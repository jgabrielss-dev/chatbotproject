"""Cotas: quem pode criar o quê, e a resposta quando o limite estourou.

Fica entre a API e o banco de propósito. As rotas só perguntam "isto cabe no
plano?" e recebem um `HTTPException` com a mensagem já pronta para o cliente; o
cálculo (qual plano, quanto já usou, quando renova) fica aqui, num lugar só. Se
cada rota refizesse a conta, uma delas ia esquecer do teste e o limite viraria
decorativo.

As funções recebem `usuario_id` e `eh_admin`, e não o `Usuario`, porque quem
chama não é sempre uma requisição web: o worker responde mensagem com o
`dono_id` que veio do agente, sem nunca ter visto um token.

Três decisões que valem explicar:

* **Admin não tem cota.** O admin cadastra canal de WhatsApp não-oficial para
  testar integração; um limite comercial quebraria a operação interna.

* **Conta expirada não perde o que já tem.** O que se bloqueia é criar agente
  novo, criar canal novo e responder mensagem nova. Apagar o bot do cliente
  porque ele deixou de pagar transforma inadimplência em perda de dado — e um
  cliente que volta e paga mereceria achar tudo no lugar.

* **A mensagem que estoura a cota não é silenciosamente perdida.** O worker
  recebe um texto pronto para mandar ao cliente, dizendo o que aconteceu e o
  que fazer. Um bot que simplesmente para de responder parece quebrado.
"""
from __future__ import annotations

import logging
import time

from fastapi import HTTPException

from app import cobranca
from app import repos_cobranca as rc
from app import repositories as repo
from app.config import settings

log = logging.getLogger("limites")

#: Tempo que o plano fica em cache. O caminho quente é o worker respondendo
#: mensagem: um SELECT a cada mensagem para ler um número que só muda uma vez
#: por renewal seria desperdício. 30 s é imperceptível para quem clica e corta
#: o custo de forma mensurável.
CACHE_SEG = 30.0

_cache: dict[str, tuple[float, cobranca.Plano]] = {}


def limpar_cache(usuario_id: str | None = None) -> None:
    """Usada pelos testes e depois de trocar o plano de alguém."""
    if usuario_id is None:
        _cache.clear()
        _isento_cache.clear()
    else:
        _cache.pop(usuario_id, None)
        _isento_cache.pop(usuario_id, None)


def sem_cota() -> bool:
    """Sem `DATABASE_URL` (dev local, suíte de teste) nada é cobrado.

    O app precisa subir numa máquina sem banco para entregar a página de login
    e o `/health`; se a cota fosse exigida aqui, o processo nem iniciaria.
    """
    return not settings.has_db


# --------------------------------------------------------------------------
# Contas isentas de pagamento
# --------------------------------------------------------------------------
# E-mails em `CONTAS_ISENTAS` (dono/operador da plataforma e contas de teste)
# nunca precisam pagar: recebem o plano máximo no `plano_atual` (o que libera
# o gerador de prompt do item 12, que pede Pro ou maior), passam por cima de
# todos os limites e veem a tela de plano sem cota — sem nunca passar por um
# checkout. A checagem por e-mail resolve na hora (rotas web); a por id
# resolve o e-mail no banco com cache curto (worker, que só tem o dono_id).

_ISENTO_CACHE_SEG = 30.0
_isento_cache: dict[str, tuple[float, bool]] = {}


def eh_isento(email: str) -> bool:
    """O e-mail está na lista de quem não paga?"""
    return (email or "").strip().lower() in settings.contas_isentas


async def eh_isento_por_id(usuario_id: str) -> bool:
    """Versão por id, para o caminho que só conhece o dono_id (worker).

    Bate no perfil uma vez e guarda 30 s — o mesmo desenho do cache do plano:
    o hot path não paga um SELECT a cada mensagem para ler uma lista que muda
    a cada deploy.
    """
    if not settings.has_db:
        return False
    agora = time.monotonic()
    guardado = _isento_cache.get(usuario_id)
    if guardado and guardado[0] > agora:
        return guardado[1]
    try:
        perfil = await repo.obter_perfil(usuario_id)
        isento = bool(perfil and eh_isento(str(perfil.get("email") or "")))
    except Exception as e:  # noqa: BLE001
        log.warning("Nao consegui checar isencao de %s: %s", usuario_id, e)
        return False
    _isento_cache[usuario_id] = (agora + _ISENTO_CACHE_SEG, isento)
    return isento


# --------------------------------------------------------------------------
# Plano corrente
# --------------------------------------------------------------------------


def _plano_admin() -> cobranca.Plano:
    return cobranca.plano(cobranca.PLANO_PRO)


def _plano_maximo() -> cobranca.Plano:
    """Plano das contas isentas: o teto do catálogo, sem cobrança."""
    return cobranca.plano(cobranca.PLANO_ESPECIALISTA)


async def plano_atual(usuario_id: str, eh_admin: bool = False) -> cobranca.Plano:
    """Plano vigente da conta, com cache curto.

    NUNCA levanta exceção: um banco instável não pode transformar "criar
    agente" em 500. Cai no plano padrão, que é o mais restritivo — errar
    bloqueando é melhor do que errar liberando, porque cota que não vale é
    cota que o cliente descobre no teste.
    """
    if sem_cota() or eh_admin:
        return _plano_admin()
    if await eh_isento_por_id(usuario_id):
        # Dono e operador não ficam trancados atrás do próprio funil.
        return _plano_maximo()
    agora = time.monotonic()
    guardado = _cache.get(usuario_id)
    if guardado and guardado[0] > agora:
        return guardado[1]
    try:
        assinatura = await rc.garantir_assinatura(usuario_id)
        p = cobranca.plano(assinatura.get("plano_id"))
    except Exception as e:  # noqa: BLE001
        log.warning("Plano de %s indisponivel, usando o padrao: %s", usuario_id, e)
        p = cobranca.plano(cobranca.PLANO_PADRAO)
    _cache[usuario_id] = (agora + CACHE_SEG, p)
    return p


async def assinatura_atual(usuario_id: str) -> dict:
    """Linha da assinatura com a virada de período já avaliada.

    `{}` para quem roda sem banco: nesses casos não há período a vigiar.
    """
    if sem_cota():
        return {}
    try:
        return await rc.garantir_assinatura(usuario_id) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("Assinatura de %s indisponivel: %s", usuario_id, e)
        return {}


# --------------------------------------------------------------------------
# Enforce
# --------------------------------------------------------------------------


async def exigir_cota_agentes(usuario_id: str) -> None:
    """Chamar antes de criar um agente. 402 quando não cabe."""
    if sem_cota():
        return
    p = await plano_atual(usuario_id)
    assinatura = await assinatura_atual(usuario_id)
    usados = await contar_agentes(usuario_id)
    if usados < p.max_agentes:
        return
    raise HTTPException(402, _msg_limite(
        assinatura,
        f"seu plano {p.nome} inclui {p.max_agentes} agente(s) e você já tem {usados}",
        f"seus {usados} agente(s)",
        "criar outro agente",
    ))


async def exigir_cota_canais(usuario_id: str, usados: int) -> None:
    """Chamar antes de criar um canal. `usados` = canais que o agente já tem."""
    if sem_cota():
        return
    p = await plano_atual(usuario_id)
    assinatura = await assinatura_atual(usuario_id)
    if usados < p.max_canais_por_agente:
        return
    raise HTTPException(402, _msg_limite(
        assinatura,
        f"o plano {p.nome} permite {p.max_canais_por_agente} canal(is) por agente "
        f"e este agente já tem {usados}",
        f"os {usados} canal(is) já ligados",
        "ligar mais um canal",
    ))


async def checar_mensagem(usuario_id: str, agente_id: int, eh_admin: bool = False) -> tuple[bool, str]:
    """O worker chama antes de gastar uma chamada de IA.

    Devolve `(pode, texto_para_o_cliente)`. O texto já é o que vai ser
    entregue no WhatsApp/Telegram: o cliente precisa saber que a cota acabou e
    o que fazer, não receber silêncio.
    """
    if sem_cota() or eh_admin:
        return True, ""
    if await eh_isento_por_id(usuario_id):
        return True, ""
    p = await plano_atual(usuario_id, eh_admin)
    assinatura = await assinatura_atual(usuario_id)
    status = assinatura.get("status") or "ativo"
    if status in ("cancelado", "expirado"):
        return False, (
            "Este bot está pausado porque o período do plano acabou. "
            "O dono pode reativar quando quiser pela tela de plano — "
            "nada do que foi configurado foi apagado."
        )
    try:
        usadas = int((await rc.consumo_do_mes(usuario_id)).get(agente_id, 0))
    except Exception as e:  # noqa: BLE001
        log.warning("Consumo de %s indisponivel, liberando: %s", usuario_id, e)
        return True, ""
    if usadas < p.max_mensagens_por_agente_mes:
        return True, ""
    return False, (
        f"Este agente chegou ao limite de {p.max_mensagens_por_agente_mes} "
        f"mensagens do mês. O dono pode aumentar o plano ou comprar mensagens "
        f"extras na tela de assinatura; até lá, o bot segue sem responder."
    )


def _msg_limite(assinatura: dict, causa: str, preservado: str, acao: str) -> str:
    """Mensagem de 402.

    Sempre diz as três coisas: o que travou, o que fazer e que nada foi
    apagado. A última é a que evita o ticket "meu bot sumiu" depois que o
    cliente deixou de pagar.
    """
    status = assinatura.get("status") or "ativo"
    if status in ("cancelado", "expirado"):
        return (
            f"O período do seu plano acabou, então não dá para {acao}. "
            f"Escolha um plano na sua conta para {acao} — {preservado} continuam "
            f"guardados e voltam a funcionar assim que a assinatura voltar."
        )
    return (
        f"Limite do plano: {causa}. Mude de plano na sua conta para {acao} — "
        f"a mudança entra no fim do período já pago e {preservado} não são "
        f"apagados."
    )


async def contar_agentes(usuario_id: str) -> int:
    try:
        return len(await rc.agentes_do_dono(usuario_id))
    except Exception as e:  # noqa: BLE001
        log.warning("Nao consegui contar agentes de %s: %s", usuario_id, e)
        return 0


# --------------------------------------------------------------------------
# Tela de plano
# --------------------------------------------------------------------------


async def contexto(usuario_id: str, eh_admin: bool = False) -> dict:
    """Plano + assinatura + consumo: é o que a tela de perfil e os agentes
    internos leem.

    Nunca levanta exceção. Uma conta sem linha em `assinaturas` (criada antes da
    migration, ou com o trigger que não rodou) cai no plano padrão: o cliente vê
    o teste e o botão de assinar, e não um 500.
    """
    if sem_cota():
        return {
            "plano": {**cobranca.plano(cobranca.PLANO_PRO).detalhe(),
                      "max_agentes": 0, "max_canais_por_agente": 0,
                      "max_mensagens_por_agente_mes": 0},
            "status": "ativo", "ciclo": "mensal",
            "inicio_periodo": None, "fim_periodo": None,
            "em_teste": False, "dias_restantes": None,
            "agentes": {"usados": 0, "limite": 0},
            "canais_por_agente": {"limite": 0},
            "consumo": [], "consumo_total": 0, "limite_total": 0,
            "troca_agendada": None, "cancelamento_agendado": None,
            "economia_anual": 0.0, "sem_cota": True,
            "catalogo": cobranca.catalogo_para_json(),
            "mensagens_excedentes": _tabela_excedentes(),
        }

    assinatura = await assinatura_atual(usuario_id)
    consumo: dict[int, int] = {}
    if not eh_admin:
        try:
            consumo = await rc.consumo_do_mes(usuario_id)
        except Exception as e:  # noqa: BLE001
            log.warning("Consumo de %s indisponivel: %s", usuario_id, e)
    assinatura = {**assinatura, "agentes_usados": await contar_agentes(usuario_id)}
    resumo = cobranca.resumo_para_usuario(assinatura, consumo)
    if eh_admin:
        # Admin não tem cota: mostrar "3 de 15 agentes" para o operador seria
        # mentira, porque nada é barrado para ele.
        resumo["plano"] = {**resumo["plano"], "max_agentes": 0,
                           "max_canais_por_agente": 0,
                           "max_mensagens_por_agente_mes": 0, "admin": True}
        resumo["agentes"] = {"usados": 0, "limite": 0, "admin": True}
    elif await eh_isento_por_id(usuario_id):
        # Conta isenta: mesmo desenho do admin, mas com o plano do teto do
        # catálogo — o gerador (plano_minimo=Pro) fica aberto sem checkout.
        resumo["status"] = "ativo"
        resumo["plano"] = {**_plano_maximo().detalhe(), "max_agentes": 0,
                           "max_canais_por_agente": 0,
                           "max_mensagens_por_agente_mes": 0, "isento": True}
        resumo["agentes"] = {"usados": 0, "limite": 0, "isento": True}
    resumo["catalogo"] = cobranca.catalogo_para_json()
    resumo["mensagens_excedentes"] = _tabela_excedentes()
    return resumo


def _tabela_excedentes() -> list[dict]:
    return [{"ate": t, "preco_por_mil": p} for t, p in cobranca.PRECO_POR_MIL_MENSAGEM]

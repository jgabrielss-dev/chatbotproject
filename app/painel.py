"""Rotas do menu do cliente (`/painel`).

Fica separado do `main.py` de propósito: estas rotas são todas de leitura de um
tenant só, enquanto as de `main.py` são as de administração (e de webhook). A
separação deixa óbvio onde a sessão é exigida e onde a posse é conferida.

Todas passam por `usuario_atual` (que o middleware já populou) e repassam o
`dono_id` para o repositório. Nada aqui aceita um `dono_id` vindo do cliente:
o dono é sempre a conta logada, e o admin — que enxerga tudo — é o único caso
em que ele é `None`.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app import repositories as repo
from app import tipos_canal
from app.auth import exigir_admin, exigir_token_aal2, limpar_cache, usuario_atual

log = logging.getLogger("painel")

router = APIRouter(prefix="/api/painel", tags=["painel"])


def _dono(usuario) -> str | None:
    """Admin vê a plataforma inteira; o resto vê só a própria conta."""
    return None if usuario.eh_admin else usuario.id


async def _agente_permitido(agente_id: int, usuario) -> dict:
    agente = await repo.obter_agente(agente_id, _dono(usuario))
    if not agente:
        # 404 em vez de 403: confirmar "existe, mas é de outro" já entrega
        # informação sobre a conta de terceiro.
        raise HTTPException(404, "Agente não encontrado.")
    return agente


@router.get("/resumo")
async def resumo(request: Request):
    """Números da home: agentes, canais, sessões, mensagens e fila."""
    usuario = usuario_atual(request)
    return {
        "estatisticas": await repo.estatisticas_do_dono(_dono(usuario)),
        "caixa": await repo.resumo_caixa(limite=5, dono_id=_dono(usuario)),
    }


@router.get("/agentes")
async def agentes(request: Request):
    return await repo.listar_agentes(_dono(usuario_atual(request)))


@router.get("/agentes/{agente_id}")
async def agente(agente_id: int, request: Request):
    """Detalhe do agente com os canais: é o que o menu do cliente monta."""
    usuario = usuario_atual(request)
    dado = await _agente_permitido(agente_id, usuario)
    canais = await repo.listar_canais(agente_id)
    dado["canais"] = [repo.canal_publico(c) for c in canais]
    return dado


@router.get("/agentes/{agente_id}/sessoes")
async def sessoes(agente_id: int, request: Request):
    """Conversas do agente, com a memória que a IA extraiu de cada cliente.

    É o dado que diferencia este SaaS de um cadastro de bot: o dono vê o que o
    bot aprendeu sobre quem falou com ele.
    """
    usuario = usuario_atual(request)
    await _agente_permitido(agente_id, usuario)
    return await repo.listar_sessoes(agente_id)


@router.get("/sessoes/{sessao_id}")
async def sessao(sessao_id: int, request: Request):
    """Uma conversa inteira."""
    usuario = usuario_atual(request)
    s = await repo.obter_sessao_do_dono(sessao_id, _dono(usuario))
    if not s:
        raise HTTPException(404, "Sessão não encontrada.")
    s["mensagens"] = await repo.mensagens_da_sessao(sessao_id)
    return s


@router.get("/canais")
async def canais(request: Request):
    """Todos os canais do tenant, para a visão de overview."""
    return [repo.canal_publico(c)
            for c in await repo.listar_canais_do_dono(_dono(usuario_atual(request)))]


@router.get("/caixa")
async def caixa(request: Request):
    """Fila de mensagens do tenant (pendências e contadores)."""
    return await repo.resumo_caixa(dono_id=_dono(usuario_atual(request)))


# --------------------------------------------------------------------------
# Item 2: canais do próprio cliente
#
# A posse já é garantida por `_agente_permitido`/`_canal_dono`, que passam o
# `dono_id` para o repositório: um id de agente ou canal de outra conta devolve
# 404, e nunca 403 com detalhe. O que falta aqui é a lista de tipos que o
# cliente pode usar, que é decisão de produto e mora em `app/tipos_canal.py`.
# Por que uma rota só de leitura: o formulário do cliente é desenhado a partir
# do que ela devolve, então cliente e servidor não podem divergir sobre quais
# campos cada canal pede.
# --------------------------------------------------------------------------


@router.get("/canais/tipos")
async def tipos_de_canal(request: Request):
    """Tipos de canal que o cliente pode criar, com os campos de cada um."""
    usuario = usuario_atual(request)
    return {
        "tipos": tipos_canal.como_json(),
        "admin": usuario.eh_admin,
    }


@router.get("/perfil")
async def perfil(request: Request):
    """Dados da conta do cliente: nome, e-mail e papel.

    Junto com o plano (`/api/plano`) é o que a aba "Conta" monta. O papel vem
    junto porque a tela usa o mesmo componente no admin.html (item 10) e não
    adianta fetchar de novo.
    """
    usuario = usuario_atual(request)
    linha = await repo.obter_perfil(usuario.id) or {}
    return {
        "id": usuario.id,
        "nome": linha.get("nome") or "",
        "email": linha.get("email") or usuario.email,
        "role": usuario.role,
        "bloqueado": usuario.bloqueado,
        "mfa_ativo": bool(linha.get("mfa_ativo")),
        "criado_em": linha.get("criado_em"),
    }


@router.put("/perfil")
async def atualizar_perfil(request: Request):
    """Atualiza o nome de exibição.

    Só o nome. O e-mail é do Supabase Auth (é a identidade de login) e mudar
    aqui deixaria as duas fontes divergentes — o Auth continuaria mandando o
    e-mail antigo em cada validação de token e o `auth.py` sobrescreveria.
    Alterar e-mail é ação de conta, e mora no próprio Supabase.
    """
    usuario = usuario_atual(request)
    body = await request.json()
    nome = str(body.get("nome") or "").strip()
    if len(nome) > 120:
        raise HTTPException(400, "Nome muito longo (máx. 120).")
    await repo.atualizar_nome_perfil(usuario.id, nome)
    limpar_cache(usuario.id)
    return await perfil(request)


@router.put("/mfa")
async def definir_segundo_fator(request: Request):
    """Liga ou desliga a exigência do segundo fator (item 5).

    A fonte da verdade do 2FA é o Supabase Auth: quem cria e apaga o fator é o
    GoTrue, e ele só aceita token de nível 2 nas duas pontas. Esta rota guarda
    apenas se *aqui* a exigência vale — porque sem isso um token de nível 1
    continuaria entrando depois de a pessoa ativar o 2FA.

    `exigir_token_aal2` é o que fecha o ciclo: quem chega aqui chegou com um
    token que o GoTrue só emite depois de conferir um código. Então o cliente
    não consegue ligar o 2FA sem ter o autenticador, nem desligar sem ele —
    não existe segredo compartilhado entre os dois lados, a prova é o token.
    """
    usuario = usuario_atual(request)
    exigir_token_aal2(usuario)
    body = await request.json()
    ativo = bool(body.get("ativo"))
    await repo.definir_mfa_ativo(usuario.id, ativo)
    limpar_cache(usuario.id)
    return {"mfa_ativo": ativo}


@router.get("/agentes/{agente_id}/canais")
async def canais_do_agente(agente_id: int, request: Request):
    """Canais de UM agente do cliente.

    Separado de `/api/painel/canais` (que lista tudo do tenant) porque o item
    2 é sobre o cliente trabalhar por agente: a tela de canal sempre é
    "escolha o agente, depois veja os canais dele", e misturar os dois deixaria
    ambíguo de qual agente um canal é antes de você editar.
    """
    usuario = usuario_atual(request)
    await _agente_permitido(agente_id, usuario)
    return [repo.canal_publico(c) for c in await repo.listar_canais(agente_id)]


@router.get("/admin/config")
async def ler_config_operacao(request: Request):
    """Configuração global da operação, para a aba de perfil do admin (item 10).

    Só o admin chega aqui. O nome é o campo de conta (vem do próprio perfil); o
    e-mail de atendimento é global de propósito — dois admins não têm duas
    respostas diferentes para "fale com a gente".
    """
    usuario = usuario_atual(request)
    exigir_admin(usuario)
    linha = await repo.obter_perfil(usuario.id) or {}
    suporte = await repo.ler_config("email_atendimento")
    return {
        "nome": linha.get("nome") or "",
        "email": linha.get("email") or usuario.email,
        "email_atendimento": suporte or "",
        "mfa_ativo": bool(linha.get("mfa_ativo")),
    }


@router.put("/admin/config")
async def salvar_config_operacao(request: Request):
    """Grava o nome e o e-mail de atendimento.

    O nome vai para `perfis` (é da conta); o e-mail vai para `app_config` (é da
    operação). Accept e-mail vazio de propósito: esconder o link de suporte é
    melhor do que apontar para um endereço que ninguém lê.
    """
    usuario = usuario_atual(request)
    exigir_admin(usuario)
    body = await request.json()

    nome = str(body.get("nome") or "").strip()
    if len(nome) > 120:
        raise HTTPException(400, "Nome muito longo (máx. 120).")
    if nome:
        await repo.atualizar_nome_perfil(usuario.id, nome)
        limpar_cache(usuario.id)

    suporte = str(body.get("email_atendimento") or "").strip()
    if suporte and not _email_valido(suporte):
        raise HTTPException(400, "Esse não parece um e-mail válido. Exemplo: suporte@empresa.com")
    await repo.gravar_config("email_atendimento", suporte)

    return await ler_config_operacao(request)


def _email_valido(valor: str) -> bool:
    """Checagem de forma, não de existência.

    Não existe "e-mail válido" sem mandar e-mail. O que dá para pegar aqui é o
    que sempre é erro de digitação: falta de arroba, espaço, dois @, sufixo
    vazio. Rejeitar `suporte@empresa.io` porque não é `.com.br` seria um filtro
    arbitrário que ninguém consegue explicar.
    """
    if valor.count("@") != 1:
        return False
    local, _, dominio = valor.partition("@")
    if not local or not dominio or " " in valor:
        return False
    if "." not in dominio or dominio.startswith(".") or dominio.endswith("."):
        return False
    return all(ch.isprintable() for ch in valor)

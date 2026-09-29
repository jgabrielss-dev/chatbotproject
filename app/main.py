from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import repositories as repo
from app.auth import (
    ADMIN_EMERGENCIA,
    exigir_admin,
    exigir_nao_bloqueado,
    resolver_usuario,
    usuario_atual,
)
from app.channels import evolution, instagram, meta_oficial, telegram
from app.config import settings
from app.database import close_pool, get_pool
from app.pipeline import processar_mensagem

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

# Id sentinela da conta de emergência (ADMIN_TOKEN). Não é um uuid, e é por isso
# que nenhuma rota tenta gravá-lo em coluna com FK para auth.users.
ADMIN_EMERGENCIA_ID = ADMIN_EMERGENCIA.id

RAIZ = Path(__file__).resolve().parent.parent
ESTATICO = RAIZ / "app" / "static"
INDEX_HTML = ESTATICO / "index.html"
ADMIN_HTML = ESTATICO / "admin.html"
PAINEL_HTML = ESTATICO / "painel.html"
MAX_CANAIS_POR_AGENTE = 5

# Janela que o webhook generico espera o worker responder antes de devolver
# "ainda na fila". O pedido NUNCA e perdido: expirado o prazo, a mensagem segue
# na caixa_entrada e e respondida assim que o worker conseguir.
WEBHOOK_GENERICO_ESPERA_SEG = 25.0

# Canais OFICIAIS (webhook direto da Meta, sem polling e sem keepalive: o Render
# pode hibernar) versus NÃO OFICIAIS (Evolution/Baileys e instagrapi, que
# precisam de processo vivo e podem banir a conta).
TIPOS_CANAL = (
    "telegram",
    "whatsapp_oficial",
    "instagram_oficial",
    "webhook",
    "whatsapp",
    "instagram",
)
TIPOS_CANAL_OFICIAL = ("whatsapp_oficial", "instagram_oficial")
AVISO_CANAL_NAO_OFICIAL = {
    "whatsapp": (
        "Integração NÃO OFICIAL (Evolution API / Baileys). Não é homologada pela "
        "Meta, usa QR e pode desconectar a qualquer momento, e o Render precisa "
        "ficar acordado (keepalive) para não perder mensagens — o que consome as "
        "horas do plano grátis. Para produção e estabilidade, prefira "
        "'WhatsApp (oficial - Cloud API)'."
    ),
    "instagram": (
        "Integração NÃO OFICIAL (instagrapi, API privada). Viola os Termos de Uso "
        "do Instagram e pode BANIR a conta; os endpoints privados mudam sem aviso. "
        "Ativa apenas por 'sessionid' e a sessão expira. Para produção, prefira "
        "'Instagram (oficial - Messaging API)'."
    ),
}


async def _exigir_identificador_unico(
    tipo: str, config: dict, *, ignorar_canal_id: int | None = None
) -> None:
    """Impede dois canais oficiais com o mesmo id de conta da Meta.

    O roteamento do webhook (na Edge Function) resolve o canal pelo
    phone_number_id / ig_user_id. Com o mesmo id em dois canais, o roteamento
    fica ambíguo e a assinatura X-Hub-Signature-256 é conferida contra o
    app_secret do canal errado — todas as mensagens seriam rejeitadas.
    """
    if tipo not in TIPOS_CANAL_OFICIAL:
        return
    campo = "ig_user_id" if tipo == "instagram_oficial" else "phone_number_id"
    identificador = str(config.get(campo) or "").strip()
    if not identificador:
        return

    for canal in await repo.listar_canais():
        if canal["id"] == ignorar_canal_id or canal["tipo"] != tipo:
            continue
        if str((canal.get("config") or {}).get(campo) or "").strip() == identificador:
            raise HTTPException(
                409,
                f"Este {campo} ({identificador}) já está em uso pelo canal "
                f"\"{canal['nome']}\". Cada conta da Meta deve ter um único canal, "
                "senão o webhook não consegue decidir para quem é a mensagem.",
            )

    # O verify_token precisa ser único entre canais, porque é por ele que o
    # handshake de verificação (GET) acha o canal — o Meta não manda o id ainda.
    token = str(config.get("verify_token") or "").strip()
    if token:
        for canal in await repo.listar_canais():
            if canal["id"] == ignorar_canal_id:
                continue
            if canal["tipo"] in TIPOS_CANAL_OFICIAL and \
                    str((canal.get("config") or {}).get("verify_token") or "").strip() == token:
                raise HTTPException(
                    409,
                    f"Este verify_token já é usado pelo canal \"{canal['nome']}\". "
                    "Use um valor diferente em cada canal oficial.",
                )


def _url_webhook_meta(tipo: str) -> str:
    """URL pública que o Meta deve chamar. Não contém segredo: o roteamento é
    feito pelo phone_number_id / ig_user_id que vem no próprio evento."""
    familia = "whatsapp" if tipo == "whatsapp_oficial" else "instagram"
    base = settings.supabase_functions_base
    return f"{base}/meta/{familia}" if base else f"(configure SUPABASE_FUNCTIONS_BASE) /meta/{familia}"


def _validar_config_canal(tipo: str, config: dict, *, agent_id: int = 0) -> dict:
    """Valida e normaliza a config conforme o tipo do canal.

    Garante que cada canal só guarde as credenciais que realmente usa — é o que
    separa de fato a integração oficial da não oficial.
    """
    config = {k: v for k, v in (config or {}).items() if v not in ("", None)}

    if tipo == "instagram":
        # Só sessionid: o login por senha foi bloqueado pelo Instagram.
        sessionid = str(config.get("sessionid") or "")
        if not sessionid:
            raise HTTPException(
                400,
                "Instagram não-oficial exige o campo 'sessionid'. Extraia o cookie "
                "'sessionid' de instagram.com com o navegador já logado "
                "(F12 > Application/Storage > Cookies > sessionid).",
            )
        return {"sessionid": sessionid, "ig_vistos": config.get("ig_vistos") or {}}

    if tipo == "instagram_oficial":
        faltando = [c for c in ("ig_user_id", "access_token", "verify_token", "app_secret")
                    if not str(config.get(c) or "").strip()]
        if faltando:
            raise HTTPException(
                400,
                "Instagram oficial exige: ig_user_id, access_token, verify_token e "
                f"app_secret (faltando: {', '.join(faltando)}). Sem o app_secret não dá "
                "para validar a assinatura X-Hub-Signature-256 e qualquer um poderia "
                "forjar mensagens no seu canal.",
            )
        return {
            "ig_user_id": str(config["ig_user_id"]).strip(),
            "access_token": str(config["access_token"]).strip(),
            "verify_token": str(config["verify_token"]).strip(),
            "app_secret": str(config["app_secret"]).strip(),
            "webhook_url": config.get("webhook_url", ""),
        }

    if tipo == "whatsapp_oficial":
        faltando = [c for c in ("phone_number_id", "access_token", "verify_token", "app_secret")
                    if not str(config.get(c) or "").strip()]
        if faltando:
            raise HTTPException(
                400,
                "WhatsApp oficial exige: phone_number_id, access_token, verify_token e "
                f"app_secret (faltando: {', '.join(faltando)}). Sem o app_secret não dá "
                "para validar a assinatura X-Hub-Signature-256 e qualquer um poderia "
                "forjar mensagens no seu canal.",
            )
        return {
            "phone_number_id": str(config["phone_number_id"]).strip(),
            "access_token": str(config["access_token"]).strip(),
            "verify_token": str(config["verify_token"]).strip(),
            "app_secret": str(config["app_secret"]).strip(),
            "webhook_url": config.get("webhook_url", ""),
        }

    if tipo == "telegram":
        if not str(config.get("token") or "").strip():
            raise HTTPException(400, "Informe o token do bot do Telegram.")
        return {"token": str(config["token"]).strip()}

    if tipo == "whatsapp":
        return {"instance_name": config.get("instance_name", ""), "status": "", "qr": ""}

    return config

_poller_task: asyncio.Task | None = None
_keepalive_task: asyncio.Task | None = None
_worker_caixa_task: asyncio.Task | None = None
_sync_task: asyncio.Task | None = None


def _gerar_secret() -> str:
    return secrets.token_urlsafe(16)


def _secret_igual(a: str, b: str) -> bool:
    """Compara segredos em tempo constante (evita vazar o valor por tempo)."""
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def _dono(usuario) -> str | None:
    """Filtro de tenant para as queries.

    `None` = admin, que enxerga todos os agentes. Qualquer outro valor = o uuid
    da conta, e o repositório restringe tudo a esse dono. Este é o único lugar
    onde se decide isso; as rotas só repassam o valor.
    """
    return None if usuario.eh_admin else usuario.id


async def _canal_dono(canal_id: int, usuario) -> dict:
    """Canal, conferindo a posse. 404 quando não existe *ou* é de outra conta.

    Todas as rotas que recebem só `canal_id` passam por aqui. Sem a conferência,
    o id sequencial do canal seria suficiente para editar, apagar, ler a URL do
    webhook ou disparar um teste no canal de outro cliente.
    """
    canal = await repo.obter_canal_do_dono(canal_id, _dono(usuario))
    if not canal:
        raise HTTPException(404, "Canal não encontrado.")
    return canal


def _canal_publico(canal: dict | None) -> dict | None:
    """Canal com a config redigida, para ir ao navegador.

    Delegado ao repositório de propósito: a redação é responsabilidade de quem
    conhece a lista de segredos, e ter uma segunda cópia aqui foi exatamente o
    que deixou o access_token da Meta escapar.
    """
    return repo.canal_publico(canal)


# --------------------------------------------------------------------------
# Pipeline de entrada (compartilhado entre os canais)
# --------------------------------------------------------------------------

async def _tratar_mensagem(canal: dict, usuario_externo: str, texto: str):
    # `None` como dono: este caminho é o worker interno, que processa a fila de
    # todos os tenants. Ele não é uma requisição de usuário, e o canal já veio
    # resolvido pela fila — não há nada a checar aqui.
    agente = await repo.obter_agente(canal["agente_id"], None)
    if not agente or not agente["ativo"]:
        return
    resposta = await processar_mensagem(agente, canal, usuario_externo, texto)
    return resposta


# --------------------------------------------------------------------------
# Poller Instagram (método não-oficial, com risco de quebra)
# --------------------------------------------------------------------------

async def _poller_instagram(intervalo: float = 25.0) -> None:
    log.info("Poller Instagram iniciado")
    while True:
        try:
            canais = [c for c in await repo.listar_canais() if c["tipo"] == "instagram" and c["ativo"]]
            for canal in canais:
                cfg = canal["config"]
                sessionid = cfg.get("sessionid", "")
                if not sessionid:
                    continue
                novos, vistos = await instagram.coletar_novas(
                    sessionid, cfg.get("ig_vistos")
                )
                if vistos != cfg.get("ig_vistos"):
                    await repo.patch_canal_config(canal["id"], "ig_vistos", vistos)
                for thread_id, msg_id, texto in novos:
                    try:
                        log.info("Instagram DM de %s (canal %s)", thread_id, canal["id"])
                        await repo.salvar_na_caixa(
                            canal["id"], thread_id, texto,
                            origem=f"ig:{thread_id}:{msg_id}", payload={"msg_id": msg_id},
                        )
                    except Exception as e:
                        log.exception("Falha ao enfileirar DM no canal %s: %s", canal["id"], e)
        except Exception as e:
            log.exception("Erro no poller Instagram: %s", e)
        await asyncio.sleep(intervalo)


# --------------------------------------------------------------------------
# Keepalive Evolution — SÓ para o canal NÃO OFICIAL
#
# O Evolution (Baileys) não é empurrado pela Meta: ele se inscreve no WhatsApp
# por conta própria e depende de um processo vivo. Se o Render hibernar, a
# Evolution perde a conexão e as mensagens param de chegar.
#
# ATENÇÃO AO CUSTO: o Render hiberna ~15 min sem tráfego. Um keepalive com
# intervalo MENOR que 15 min impede a hibernação, ou seja, o serviço fica ligado
# o mês inteiro: ~720h das 750h/mês do plano grátis. Dois serviços assim
# estouram o limite (1440h > 750h), e é exatamente por isso que o consumo
# acabava antes dos 14 dias. Com intervalo ACIMA de 15 min o serviço dorme entre
# os pings e o custo cai para algumas horas por mês.
# Os canais OFICIAIS (whatsapp_oficial / instagram_oficial) NÃO precisam
# disto: o Meta chama o webhook, a Edge Function grava a fila e acorda o Render
# sob demanda. Prefira os oficiais sempre que possível.
# --------------------------------------------------------------------------

async def _keepalive_evolution(intervalo: float | None = None) -> None:
    if not settings.has_evolution:
        return
    if intervalo is None:
        # Acima de 900s o Render hiberna entre os pings e o custo despenca.
        intervalo = max(float(settings.evolution_keepalive_seg), 60.0)
    # Só liga o keepalive se existir canal NÃO oficial usando Evolution.
    try:
        canais = [c for c in await repo.listar_canais()
                  if c["tipo"] == "whatsapp" and c["ativo"] and c["config"].get("instance_name")]
    except Exception as e:
        log.warning("Não foi possível verificar canais Evolution: %s", e)
        return
    if not canais:
        log.info(
            "Keepalive Evolution DESLIGADO: nenhum canal 'whatsapp' (Evolution) ativo. "
            "Com apenas canais oficiais, o Render pode hibernar sem perder mensagens."
        )
        return

    # O Render hiberna ~15 min sem tráfego: ping mais frequente que isso
    # impede a hibernação e consome o mês inteiro (~720h de 750h).
    JANELA_HIBERNACAO_SEG = 900
    if intervalo < JANELA_HIBERNACAO_SEG:
        horas = 24 * 30
        log.warning(
            "Keepalive Evolution LIGADO (a cada %.0fs). Isso é MAIS FREQUENTE que a "
            "janela de hibernação do Render (~%.0fs), então o serviço fica ligado o mês "
            "inteiro: ~%dh das 750h do plano grátis. Aumente EVOLUTION_KEEPALIVE_SEG para "
            ">%ds ou migre para 'WhatsApp oficial (Cloud API)', que dispensa este custo.",
            intervalo, JANELA_HIBERNACAO_SEG, horas, JANELA_HIBERNACAO_SEG,
        )
    else:
        log.info(
            "Keepalive Evolution LIGADO (a cada %.0fs): o Render hiberna entre os pings, "
            "então o custo é de algumas horas por mês.", intervalo,
        )
    url, key = settings.evolution_api_url, settings.evolution_api_key
    while True:
        try:
            canais = [c for c in await repo.listar_canais() if c["tipo"] == "whatsapp" and c["ativo"]]
            instancias = [c["config"]["instance_name"] for c in canais if c["config"].get("instance_name")]
            if not instancias:
                instancias = ["_"]
            for instancia in instancias:
                try:
                    await evolution.status_instancia(url, key, instancia)
                except Exception:
                    pass
        except Exception as e:
            log.warning("Erro no keepalive Evolution: %s", e)
        await asyncio.sleep(intervalo)


# --------------------------------------------------------------------------
# Caixa de entrada durável: o webhook só persiste a mensagem; um worker
# processa a fila com retry (backoff) para ninguém ficar sem resposta.
# --------------------------------------------------------------------------

def _prefixo_usuario(canal: dict, remetente: str) -> str:
    tipo = canal["tipo"]
    if tipo == "telegram":
        return f"tg:{remetente}"
    if tipo in ("whatsapp", "whatsapp_oficial"):
        return f"wa:{remetente}"
    if tipo in ("instagram", "instagram_oficial"):
        return f"ig:{remetente}"
    return f"web:{remetente}"


# Canais oficiais da Meta: o id do interlocutor é sempre numérico e já vem
# normalizado (wa_id / ig-scoped id) da Edge Function.
async def _enviar_resposta(canal: dict, remetente: str, resposta: str) -> None:
    cfg = canal["config"]
    tipo = canal["tipo"]
    if tipo == "telegram":
        if not cfg.get("token"):
            raise RuntimeError("canal de telegram sem token definido")
        await telegram.enviar_mensagem(cfg["token"], remetente, resposta)
    elif tipo == "whatsapp":
        if not (settings.has_evolution and cfg.get("instance_name")):
            raise RuntimeError("canal de whatsapp sem Evolution configurada")
        url, key = _evo_creds()
        await evolution.enviar_mensagem(url, key, cfg["instance_name"], remetente, resposta)
    elif tipo == "instagram":
        sessionid = cfg.get("sessionid", "")
        if not sessionid:
            raise RuntimeError("canal de instagram sem 'sessionid' definido")
        await instagram.enviar_mensagem(sessionid, remetente, resposta)
    elif tipo in ("whatsapp_oficial", "instagram_oficial"):
        await meta_oficial.enviar(cfg, tipo, remetente, resposta)


def _proxima_tentativa(tentativas: int) -> datetime:
    """Backoff para uma falha. A mensagem NUNCA sai da fila: ela volta para o
    fim dela (proxima_tentativa no futuro) e é tentada de novo em ciclo."""
    base = settings.inbox_backoff_base_seg
    teto = settings.inbox_backoff_teto_seg
    atraso = min(base * (2 ** min(tentativas - 1, 6)), teto)
    return datetime.now(timezone.utc) + timedelta(seconds=atraso)


async def _processar_caixa(limite: int | None = None) -> None:
    limite = limite or settings.inbox_lote
    devolvidas = await repo.reenfileirar_processando()
    if devolvidas:
        log.warning("%s mensagem(ns) presa(s) em 'processando' voltaram para o fim da fila", devolvidas)
    for item in await repo.listar_caixa_para_processar(limite):
        canal = await repo.obter_canal(item["canal_id"])
        if not canal:
            await repo.falhar_caixa(item["id"], _proxima_tentativa(item["tentativas"] + 1),
                                    "canal não encontrado")
            continue
        if not canal["ativo"]:
            await repo.falhar_caixa(item["id"], _proxima_tentativa(item["tentativas"] + 1),
                                    "canal está pausado")
            continue
        # O canal "webhook" tambem passa pelo worker: a rota so enfileira e
        # espera. Pular aqui marcaria sem_resposta sem nunca gerar a resposta.
        await repo.marcar_caixa_processando(item["id"])
        try:
            resposta = await _tratar_mensagem(
                canal, _prefixo_usuario(canal, item["remetente"]), item["texto"]
            )
        except Exception as e:
            log.exception("Falha ao gerar resposta (inbox %s): %s", item["id"], e)
            await repo.falhar_caixa(item["id"], _proxima_tentativa(item["tentativas"] + 1), str(e))
            continue
        if not resposta:
            await repo.concluir_caixa(item["id"], "sem_resposta")
            continue
        try:
            await _enviar_resposta(canal, item["remetente"], resposta)
        except Exception as e:
            log.exception("Falha ao enviar resposta (inbox %s): %s", item["id"], e)
            await repo.falhar_caixa(item["id"], _proxima_tentativa(item["tentativas"] + 1), str(e))
            continue
        await repo.concluir_caixa(item["id"], "respondido", resposta)
        log.info("Inbox %s respondida no canal %s (de %s)", item["id"], canal["id"], item["remetente"])


async def _sincronizar_evolution(intervalo: float = 30.0) -> None:
    """Rede de segurança do WhatsApp: se um webhook caiu (app dormindo/hibernação),
    busca as mensagens recebidas na Evolution e as enfileira para responder depois.

    Também reconfere a URL de webhook dos canais a cada ~10 minutos: se a Edge
    Function ou a BASE_URL mudarem (ou o segredo for trocado), o canal volta a
    entregar sozinho, sem ninguém precisar abrir o painel."""
    if not settings.has_evolution:
        return
    url, key = settings.evolution_api_url, settings.evolution_api_key
    volta = 0
    while True:
        try:
            if volta <= 0:
                volta = 1
                await _reconciliar_webhooks()
            volta += 1
            canais = [c for c in await repo.listar_canais() if c["tipo"] == "whatsapp"]
            for canal in canais:
                instancia = canal["config"].get("instance_name")
                if not instancia:
                    continue
                marco = float(canal["config"].get("sync_caixa_desde") or 0)
                try:
                    novas = await evolution.listar_mensagens(url, key, instancia)
                except Exception as e:
                    log.warning("Sincronização Evolution falhou (inst %s): %s", instancia, e)
                    continue
                if marco == 0:
                    await repo.patch_canal_config(canal["id"], "sync_caixa_desde", time.time())
                    continue
                maior_ts = marco
                for msg in novas:
                    if msg["ts"] > marco:
                        await repo.salvar_na_caixa(
                            canal["id"], msg["numero"], msg["texto"],
                            origem=f"wa:{msg['origem_id']}", payload=msg["dados"],
                        )
                        if msg["ts"] > maior_ts:
                            maior_ts = msg["ts"]
                if maior_ts > marco:
                    await repo.patch_canal_config(canal["id"], "sync_caixa_desde", maior_ts)
        except Exception as e:
            log.warning("Erro na sincronização Evolution: %s", e)
        if volta >= 20:  # ~10 min com intervalo de 30s
            volta = 0
        await asyncio.sleep(intervalo)


async def _worker_caixa() -> None:
    log.info("Worker da caixa de entrada iniciado")
    while True:
        try:
            if settings.has_db:
                await _processar_caixa()
        except Exception as e:
            log.exception("Erro no worker da caixa: %s", e)
        await asyncio.sleep(2)


async def _reconciliar_tarefas_de_fundo() -> None:
    """Liga/desliga as tarefas de fundo conforme os canais ativos no banco.
    Sem isso, um canal Instagram/Evolution criado pelo painel só começaria a
    funcionar depois de reiniciar o serviço, porque as tarefas nascem uma vez
    no lifespan. Como o Render hiberna, o usuário não reinicia nada e o canal
    ficava morto. Aqui cada mudança de canal é reconciliada na hora.
    """
    global _poller_task, _keepalive_task, _sync_task
    try:
        canais = await repo.listar_canais()
    except Exception as e:
        log.warning("Nao foi possivel reconciliar tarefas de fundo: %s", e)
        return

    quer_instagram = any(c["tipo"] == "instagram" and c["ativo"] for c in canais)
    quer_evolution = any(c["tipo"] == "whatsapp" and c["ativo"] for c in canais)

    def _garantir(tarefa: asyncio.Task | None, ativo: bool,
                  criar, nome: str) -> asyncio.Task | None:
        if ativo and (tarefa is None or tarefa.done()):
            log.info("Ligando tarefa de fundo: %s", nome)
            return asyncio.create_task(criar())
        if not ativo and tarefa is not None and not tarefa.done():
            log.info("Desligando tarefa de fundo: %s", nome)
            tarefa.cancel()
            return None
        return tarefa if ativo else None

    _poller_task = _garantir(_poller_task, quer_instagram, _poller_instagram,
                             "poller Instagram (nao oficial)")
    if quer_evolution:
        _keepalive_task = _garantir(_keepalive_task, True, _keepalive_evolution,
                                    "keepalive Evolution (nao oficial)")
        _sync_task = _garantir(_sync_task, True, _sincronizar_evolution,
                               "sincronizacao Evolution (nao oficial)")
    else:
        _keepalive_task = _garantir(_keepalive_task, False, _keepalive_evolution,
                                    "keepalive Evolution (nao oficial)")
        _sync_task = _garantir(_sync_task, False, _sincronizar_evolution,
                               "sincronizacao Evolution (nao oficial)")

    if not quer_instagram:
        log.info("Nenhum canal Instagram nao-oficial ativo: poller desligado.")
    if not quer_evolution:
        log.info("Nenhum canal WhatsApp (Evolution) ativo: keepalive desligado. "
                 "Canais oficiais nao precisam de keepalive.")


async def _reconciliar_webhooks() -> None:
    """Reconfere, uma vez por cold start, as URLs de webhook já registradas.

    Cobre a migração para a URL com segredo (a instância antiga continuaria
    chamando a URL velha e pararia de entregar), a troca de BASE_URL e o
    redesenho das rotas da Edge Function. Sem isto, cada deploy deixava o
    Telegram e o WhatsApp mudos até alguém clicar em "registrar webhook" no
    painel. Uma chamada por canal."""
    url, key = settings.evolution_api_url, settings.evolution_api_key
    for canal in await repo.listar_canais():
        if canal["tipo"] == "telegram":
            await _reconciliar_webhook_telegram(canal)
            continue
        if canal["tipo"] != "whatsapp" or not canal["config"].get("instance_name"):
            continue
        if not settings.has_evolution:
            log.warning("Canal WhatsApp %s sem Evolution configurada: webhook não conferido.", canal["id"])
            continue
        nome = canal["config"]["instance_name"]
        try:
            atual = (await evolution.obter_webhook(url, key, nome)).get("url", "")
            esperado = _url_de_webhook(canal)
        except Exception as e:
            log.warning("Não consegui ler o webhook de %s: %s", nome, e)
            continue
        if atual == esperado:
            continue
        try:
            await evolution.atualizar_webhook(url, key, nome, esperado)
            log.info("Webhook de %s atualizado para a URL com segredo.", nome)
        except Exception as e:
            log.warning("Falha ao atualizar o webhook de %s: %s", nome, e)


async def _reconciliar_webhook_telegram(canal: dict) -> None:
    """Se a URL registrada no Telegram não for a esperada, reconfigura sozinho.

    Sem isto, trocar a BASE_URL, o segredo do canal ou implantar uma versão nova
    da Edge Function deixava o bot mudo até alguém clicar em "registrar webhook".
    """
    token = canal["config"].get("token")
    if not token:
        return
    try:
        esperado = _url_de_webhook(canal)
    except HTTPException:
        return
    try:
        info = await telegram.info_webhook(token)
        if info.get("url") == esperado:
            return
        await telegram.definir_webhook(token, esperado)
        log.info("Webhook do Telegram do canal %s reconferido.", canal["id"])
    except Exception as e:
        log.warning("Falha ao reconferir o webhook do Telegram do canal %s: %s", canal["id"], e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.has_db:
        await get_pool()
        global _worker_caixa_task
        _worker_caixa_task = asyncio.create_task(_worker_caixa())
        # Só há tarefa de fundo nos canais NÃO OFICIAIS. Os oficiais são
        # 100% push (webhook), então nada de polling/keepalive: é isso que
        # permite ao Render passar o mês hibernando.
        try:
            await _reconciliar_webhooks()
        except Exception as e:
            log.warning("Falha ao reconciliar webhooks da Evolution: %s", e)
        await _reconciliar_tarefas_de_fundo()
    yield
    for tarefa in (_poller_task, _keepalive_task, _worker_caixa_task, _sync_task):
        if tarefa:
            tarefa.cancel()
    # Cancelar não basta: sem await a task fica órfã e o Uvicorn mata o
    # processo no meio de uma escrita no banco.
    pendentes = [t for t in (_poller_task, _keepalive_task, _worker_caixa_task, _sync_task) if t]
    if pendentes:
        await asyncio.gather(*pendentes, return_exceptions=True)
    await close_pool()


app = FastAPI(title="Chatbot Project SaaS", lifespan=lifespan)

# O menu do cliente mora em app/painel.py: sao rotas de leitura de um tenant so,
# separadas das de administracao que ficam aqui. Importar depois de criar o
# `app` evita ciclo, porque painel.py nao importa o main.
from app import painel as painel_router  # noqa: E402

app.include_router(painel_router.router)

# Paginas e assets. Montado explicitamente porque as tres telas (index/admin/
# painel) e seus .js/.css sao servidos daqui em vez do diretorio do projeto.
if ESTATICO.is_dir():
    app.mount("/static", StaticFiles(directory=ESTATICO), name="static")

# A pagina tambem pode ser hospedada no GitHub Pages, entao liberamos CORS
# para que o navegador consiga chamar esta API de outra origem.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rotas publicas: as tres paginas (sao so o shell do HTML; os dados exigem
# conta), o health check do Render, a config do front e os webhooks, que tem
# segredo proprio na propria rota. TODO o resto exige usuario logado.
_ROTAS_PUBLICAS = ("/", "/health", "/admin", "/painel", "/api/config")
_PREFIXOS_PUBLICOS = ("/static/", "/webhook/")


def _eh_publica(caminho: str) -> bool:
    return caminho in _ROTAS_PUBLICAS or caminho.startswith(_PREFIXOS_PUBLICOS)


@app.middleware("http")
async def exigir_login(request: Request, call_next):
    """Fecha a API: sem sessão válida, nenhuma rota /api/* responde.

    Antes, sem `ADMIN_TOKEN` no servidor a API ficava aberta e o painel não
    tinha login nenhum. Com multi-tenant isso deixou de ser opção: um
    `WHERE id = $1` responderia com a fila de mensagens de outro cliente. O
    padrão agora é "fechado" — quem não tem sessão leva 401, sempre.
    """
    caminho = request.url.path
    if _eh_publica(caminho) or request.method == "OPTIONS":
        return await call_next(request)

    if not settings.has_auth:
        return JSONResponse(
            {
                "detail": "Supabase Auth não configurado no servidor. Defina SUPABASE_URL e "
                          "SUPABASE_ANON_KEY (veja deploy/.env.example) para habilitar as contas."
            },
            status_code=503,
        )

    try:
        usuario = await resolver_usuario(request)
    except HTTPException as e:
        return JSONResponse({"detail": e.detail}, status_code=e.status_code)

    if usuario is None:
        return JSONResponse(
            {"detail": "Sessão ausente ou expirada. Faça login."},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    if usuario.bloqueado:
        return JSONResponse({"detail": "Conta bloqueada. Fale com o administrador."},
                            status_code=403)
    request.state.usuario = usuario
    return await call_next(request)


@app.get("/", include_in_schema=False)
async def index():
    """Home: login e cadastro. A escolha admin x cliente acontece depois, no
    navegador, conforme o papel que o servidor devolve em /api/config."""
    return FileResponse(INDEX_HTML)


@app.get("/admin", include_in_schema=False)
async def pagina_admin():
    return FileResponse(ADMIN_HTML)


@app.get("/painel", include_in_schema=False)
async def pagina_painel():
    return FileResponse(PAINEL_HTML)


@app.get("/api/config", include_in_schema=False)
async def api_config():
    """O que o navegador precisa antes de ter conta: URL do Supabase e a chave
    publicável (que existe justamente para ir no cliente). Nada sensível — a
    service_role nunca sai do servidor."""
    return {
        "supabase_url": settings.supabase_url,
        "supabase_anon_key": settings.supabase_anon_key,
        "auth_habilitado": settings.has_auth,
        "nao_oficiais_para_usuarios": settings.canais_nao_oficiais_para_usuarios,
    }


@app.get("/api/eu", include_in_schema=False)
async def api_eu(request: Request):
    """Quem sou eu. O front lê isto para saber se leva para /admin ou /painel,
    e para mostrar o nome. Também é o que faz o login parecer instantâneo."""
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)

    # Adoção dos agentes anteriores ao multi-tenant. `dono_id IS NULL` vira zero
    # linhas depois da primeira vez, então repetir a cada visita é barato — e
    # evita depender de o operador lembrar de rodar um UPDATE depois do deploy.
    # A conta de emergência (ADMIN_TOKEN) fica de fora: o id dela não é um uuid
    # e a FK de agentes.dono_id recusaria o INSERT.
    if usuario.eh_admin and usuario.id != ADMIN_EMERGENCIA_ID:
        try:
            await repo.reivindicar_agentes_sem_dono(usuario.id)
        except Exception as e:
            log.warning("Não consegui reivindicar agentes sem dono: %s", e)

    return {
        "id": usuario.id,
        "email": usuario.email,
        "role": usuario.role,
        "eh_admin": usuario.eh_admin,
    }


@app.get("/health", include_in_schema=False)
async def health():
    return {"ok": True}


# --------------------------------------------------------------------------
# Webhooks públicos (entrada de mensagens dos canais)
# --------------------------------------------------------------------------

@app.post("/webhook/telegram/{canal_id}/{secret}")
async def webhook_telegram(canal_id: int, secret: str, request: Request):
    payload = await request.json()
    canal = await repo.obter_canal(canal_id)
    if not canal or canal["tipo"] != "telegram":
        return JSONResponse({"ok": False}, status_code=404)
    if not _secret_igual(secret, canal["config"].get("secret", "")):
        return JSONResponse({"ok": False}, status_code=404)

    texto, chat_id = telegram.extrair_mensagem(payload)
    if texto and chat_id:
        await repo.salvar_na_caixa(
            canal_id, chat_id, texto,
            origem=f"tg:{payload.get('update_id')}", payload=payload,
        )
    return JSONResponse({"ok": True})


def _qr_strip(v: str | None) -> str:
    return (v or "").replace("data:image/png;base64,", "")


def _norm_evento(v: str | None) -> str:
    return (v or "").upper().replace(".", "_")


@app.post("/webhook/evolution/{canal_id}/{secret}")
async def webhook_evolution(canal_id: int, secret: str, request: Request):
    payload = await request.json()
    canal = await repo.obter_canal(canal_id)
    if not canal or canal["tipo"] != "whatsapp":
        return JSONResponse({"ok": False}, status_code=404)
    cfg = canal["config"]
    if not _secret_igual(secret, cfg.get("secret", "")):
        return JSONResponse({"ok": False}, status_code=404)

    evento = _norm_evento(payload.get("event"))

    if evento == "QRCODE_UPDATED":
        dados = payload.get("data", {})
        qr_dados = dados.get("qrcode") or dados
        qr = _qr_strip(qr_dados.get("base64") or qr_dados.get("code"))
        if qr:
            await repo.patch_canal_config(canal_id, "qr", qr)
        await repo.patch_canal_config(canal_id, "status", "scanning")
        return JSONResponse({"ok": True})

    if evento == "CONNECTION_UPDATE":
        estado = payload.get("data", {}).get("state", "")
        await repo.patch_canal_config(canal_id, "status", estado)
        if estado == "open":
            await repo.patch_canal_config(canal_id, "qr", "")
        return JSONResponse({"ok": True})

    if payload.get("instance") and payload["instance"] != cfg.get("instance_name"):
        return JSONResponse({"ok": True})

    texto, numero, dados = evolution.extrair_mensagem(payload)
    if texto and numero:
        key_id = (dados.get("key") or {}).get("id") or ""
        await repo.salvar_na_caixa(
            canal_id, numero, texto, origem=f"wa:{key_id}", payload=dados,
        )
    return JSONResponse({"ok": True})


@app.post("/webhook/generico/{canal_id}/{secret}")
async def webhook_generico(canal_id: int, secret: str, request: Request):
    """Enfileira e devolve a resposta. O pedido NUNCA e perdido: mesmo expirado
    o prazo de espera, a mensagem continua na caixa_entrada e o worker responde
    depois. Antes, uma falha do Gemini aqui custava a mensagem do cliente."""
    if not settings.has_db:
        raise HTTPException(503, "Banco de dados não configurado.")
    canal = await repo.obter_canal(canal_id)
    if not canal or canal["tipo"] != "webhook":
        return JSONResponse({"ok": False}, status_code=404)
    if not _secret_igual(secret, canal["config"].get("secret", "")):
        return JSONResponse({"ok": False}, status_code=404)

    payload = await request.json()
    # aceita text/texto e user/remetente: um payload com a outra grafia nao pode
    # receber resposta vazia em silencio, que e a perda que a fila existe pra evitar
    texto = str(payload.get("text") or payload.get("texto") or "").strip()
    usuario = str(payload.get("user") or payload.get("remetente") or "anonimo").strip() or "anonimo"
    if not texto:
        return JSONResponse({"reply": ""})

    # origem unica por requisicao: o indice unico (canal_id, origem) descartaria
    # a segunda mensagem se duas requisições viessem com a origem vazia.
    msg_id = await repo.salvar_na_caixa(
        canal_id, usuario, texto, origem=f"web:{uuid.uuid4().hex}", payload=payload,
    )
    if msg_id is None:
        return JSONResponse({"reply": "", "status": "duplicado"}, status_code=200)

    item = await repo.aguardar_caixa(msg_id, timeout_s=WEBHOOK_GENERICO_ESPERA_SEG)
    if item and item["status"] == "respondido":
        # status sempre presente: quem chama precisa saber responder bem ou esperar
        return JSONResponse({"reply": item["resposta"] or "", "status": "respondido"})
    if item and item["ultimo_erro"]:
        # Falhou agora, mas continua na fila: o worker tenta de novo.
        return JSONResponse({"reply": "", "status": "na_fila", "erro": item["ultimo_erro"]}, status_code=202)
    return JSONResponse({"reply": "", "status": "na_fila"}, status_code=202)


# --------------------------------------------------------------------------
# Gestão de agentes
#
# O mesmo conjunto de rotas serve ao admin e ao cliente. A única diferença é o
# `dono` que o repositório aplica: o admin passa None (vê tudo) e o cliente
# passa o próprio uuid (vê só o que é dele). Não existe rota paralela "de
# cliente": duplicar isso é como uma rota nova nasce sem filtro e vaza dados.
# --------------------------------------------------------------------------

@app.get("/api/agentes")
async def get_agentes(request: Request):
    usuario = usuario_atual(request)
    return await repo.listar_agentes(_dono(usuario))


@app.post("/api/agentes")
async def post_agente(request: Request):
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    body = await request.json()
    nome = (body.get("nome") or "").strip()
    prompt = (body.get("system_prompt") or "").strip()
    if not nome:
        raise HTTPException(400, "Informe o nome do agente.")
    if not prompt:
        raise HTTPException(400, "Informe o system prompt.")
    # O dono é SEMPRE a conta logada. Ignorar um "dono_id" que venha no corpo é
    # proposital: se o campo fosse respeitado, bastaria trocar o uuid no corpo
    # para criar agente na conta de outro.
    return await repo.criar_agente(nome, prompt, usuario.id)


@app.put("/api/agentes/{agente_id}")
async def put_agente(agente_id: int, request: Request):
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    body = await request.json()
    nome = (body.get("nome") or "").strip()
    prompt = (body.get("system_prompt") or "").strip()
    ativo = bool(body.get("ativo", True))
    if not nome or not prompt:
        raise HTTPException(400, "Nome e system prompt são obrigatórios.")
    r = await repo.atualizar_agente(agente_id, nome, prompt, ativo, _dono(usuario))
    if not r:
        # 404 e não 403: para quem não é dono, o agente não deve nem existir
        # como informação (a existência já é dado do outro cliente).
        raise HTTPException(404, "Agente não encontrado.")
    return r


@app.delete("/api/agentes/{agente_id}")
async def delete_agente(agente_id: int, request: Request):
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    dono = _dono(usuario)
    # Confere a posse ANTES de apagar: os canais são lidos para derrubar a
    # instância na Evolution, e essa limpeza não pode rodar para agente alheio.
    if not await repo.obter_agente(agente_id, dono):
        raise HTTPException(404, "Agente não encontrado.")
    canais = await repo.listar_canais(agente_id)
    if not await repo.excluir_agente(agente_id, dono):
        raise HTTPException(404, "Agente não encontrado.")
    if settings.has_evolution:
        for c in canais:
            if c["tipo"] == "whatsapp" and c["config"].get("instance_name"):
                try:
                    await evolution.deletar_instancia(
                        settings.evolution_api_url, settings.evolution_api_key, c["config"]["instance_name"]
                    )
                except Exception as e:
                    log.warning("Falha ao remover instância %s: %s", c["config"]["instance_name"], e)
    return {"ok": True}


# --------------------------------------------------------------------------
# Gestão de canais
# --------------------------------------------------------------------------

@app.get("/api/agentes/{agente_id}/canais")
async def get_canais(agente_id: int, request: Request):
    usuario = usuario_atual(request)
    if not await repo.obter_agente(agente_id, _dono(usuario)):
        raise HTTPException(404, "Agente não encontrado.")
    return [_canal_publico(c) for c in await repo.listar_canais(agente_id)]


def _exigir_tipo_permitido(usuario, tipo: str) -> None:
    """Canais não oficiais criam instância em infraestrutura compartilhada e
    podem banir a conta conectada. Eles ficam restritos ao admin; a variável
    CANAIS_NAO_OFICIAIS_PARA_USUARIOS abre para todos os clientes, se o
    operador decidir assumir esse risco."""
    if usuario.eh_admin or tipo not in ("whatsapp", "instagram"):
        return
    if settings.canais_nao_oficiais_para_usuarios:
        return
    raise HTTPException(
        403,
        f"O canal \"{tipo}\" não está disponível na sua conta. Use os oficiais da "
        "Meta (WhatsApp Cloud API / Instagram Messaging API) ou o Telegram: eles "
        "são estáveis e não podem banir a conta conectada. Fale com o administrador "
        "se precisar deste canal.",
    )


@app.post("/api/agentes/{agente_id}/canais")
async def post_canal(agente_id: int, request: Request):
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    if not await repo.obter_agente(agente_id, _dono(usuario)):
        raise HTTPException(404, "Agente não encontrado.")
    body = await request.json()
    tipo = body.get("tipo", "").strip()
    nome = (body.get("nome") or "").strip()
    config = dict(body.get("config") or {})
    if tipo not in TIPOS_CANAL:
        raise HTTPException(400, "Tipo de canal inválido.")
    if not nome:
        raise HTTPException(400, "Informe um nome para o canal.")
    _exigir_tipo_permitido(usuario, tipo)

    existentes = await repo.listar_canais(agente_id)
    if len(existentes) >= MAX_CANAIS_POR_AGENTE:
        raise HTTPException(400, f"Cada agente aceita no máximo {MAX_CANAIS_POR_AGENTE} canais.")

    config = _validar_config_canal(tipo, config, agent_id=agente_id)
    await _exigir_identificador_unico(tipo, config)

    config["secret"] = _gerar_secret()
    if tipo == "whatsapp":

        config["instance_name"] = f"{settings.evolution_instance_prefix}{agente_id}x{secrets.token_hex(3)}"
        canal = await repo.criar_canal(agente_id, tipo, nome, config)
        try:
            qr = await _conectar_whatsapp(canal)
            canal = await repo.obter_canal(canal["id"])
            canal["config"]["qr"] = qr
            canal["config"]["status"] = canal["config"].get("status", "scanning")
            await _reconciliar_tarefas_de_fundo()
            return _canal_publico(canal)
        except HTTPException:
            await repo.excluir_canal(canal["id"])
            raise
        except Exception as e:
            await repo.excluir_canal(canal["id"])
            raise HTTPException(400, f"Falha ao iniciar WhatsApp: {e}")
    if tipo in ("whatsapp_oficial", "instagram_oficial"):
        config["webhook_url"] = _url_webhook_meta(tipo)
    canal = await repo.criar_canal(agente_id, tipo, nome, config)
    await _reconciliar_tarefas_de_fundo()
    return _canal_publico(canal)


@app.put("/api/canais/{canal_id}")
async def put_canal(canal_id: int, request: Request):
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    body = await request.json()
    nome = (body.get("nome") or "").strip()
    ativo = bool(body.get("ativo", True))
    novos = dict(body.get("config") or {})
    if not nome:
        raise HTTPException(400, "Informe um nome para o canal.")
    atual = await _canal_dono(canal_id, usuario)
    _exigir_tipo_permitido(usuario, atual["tipo"])

    # Mescla preservando segredos: o painel reexibe '********' e não deve
    # sobrescrever o token/sessionid guardado com esse placeholder.
    tipo = atual["tipo"]
    config = repo.mesclar_config_sync(atual, novos)
    if tipo in TIPOS_CANAL:
        config = _validar_config_canal(tipo, config, agent_id=atual.get("agente_id", 0))
        await _exigir_identificador_unico(tipo, config, ignorar_canal_id=canal_id)
    if tipo == "whatsapp":
        config["instance_name"] = atual["config"].get("instance_name", "")
        config["status"] = atual["config"].get("status", "")
        config.setdefault("qr", "")
    if tipo in TIPOS_CANAL_OFICIAL:
        config["webhook_url"] = _url_webhook_meta(tipo)
    config["secret"] = atual["config"].get("secret") or _gerar_secret()
    salvo = await repo.atualizar_canal(canal_id, nome, config, ativo)
    await _reconciliar_tarefas_de_fundo()
    return _canal_publico(salvo)


@app.delete("/api/canais/{canal_id}")
async def delete_canal(canal_id: int, request: Request):
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    canal = await _canal_dono(canal_id, usuario)
    await repo.excluir_canal(canal_id)
    if canal["tipo"] == "whatsapp" and settings.has_evolution and canal["config"].get("instance_name"):
        try:
            await evolution.deletar_instancia(
                settings.evolution_api_url, settings.evolution_api_key, canal["config"]["instance_name"]
            )
        except Exception as e:
            log.warning("Falha ao remover instância Evolution do canal %s: %s", canal_id, e)
    await _reconciliar_tarefas_de_fundo()
    if canal["tipo"] == "instagram" and canal["config"].get("sessionid"):
        try:
            await repo.excluir_sessao_instagram(
                instagram._chave(canal["config"]["sessionid"])
            )
        except Exception as e:
            log.warning("Falha ao remover a sessão do Instagram do canal %s: %s", canal_id, e)
    return {"ok": True}


# --------------------------------------------------------------------------
# Ações por tipo de canal
# --------------------------------------------------------------------------

def _url_de_webhook(canal: dict) -> str:
    """URL publica do canal. Telegram e webhook generico carregam o segredo na
    propria rota; o do Evolution tambem (antes so o id, e qualquer um achava)."""
    base = settings.base_url
    inbox = settings.supabase_functions_base
    secret = canal["config"].get("secret", "")
    if canal["tipo"] == "telegram":
        if inbox:
            return f"{inbox}/telegram/{canal['id']}/{secret}"
        if not base:
            raise HTTPException(400, "Configure a variável BASE_URL (ou SUPABASE_FUNCTIONS_BASE) no .env para gerar webhooks.")
        return f"{base}/webhook/telegram/{canal['id']}/{secret}"
    if canal["tipo"] == "whatsapp":
        if inbox:
            return f"{inbox}/evolution/{canal['id']}/{secret}"
        if not base:
            raise HTTPException(400, "Configure a variável BASE_URL no .env para gerar webhooks.")
        return f"{base}/webhook/evolution/{canal['id']}/{secret}"
    if canal["tipo"] == "webhook":
        if not base:
            raise HTTPException(400, "Configure a variável BASE_URL no .env para gerar webhooks.")
        return f"{base}/webhook/generico/{canal['id']}/{secret}"
    return ""


@app.get("/api/canais/{canal_id}/webhook-url")
async def get_webhook_url(canal_id: int, request: Request):
    return {"url": _url_de_webhook(await _canal_dono(canal_id, usuario_atual(request)))}


@app.post("/api/canais/{canal_id}/telegram/set-webhook")
async def set_telegram_webhook(canal_id: int, request: Request):
    canal = await _canal_dono(canal_id, usuario_atual(request))
    if canal["tipo"] != "telegram":
        raise HTTPException(404, "Canal de Telegram não encontrado.")
    token = canal["config"].get("token")
    if not token:
        raise HTTPException(400, "Defina o token do bot primeiro.")
    try:
        info = await telegram.info_bot(token)
        await telegram.definir_webhook(token, _url_de_webhook(canal))
        return {"ok": True, "bot": info.get("username")}
    except Exception as e:
        raise HTTPException(400, f"Falha ao configurar webhook: {e}")


def _evo_creds() -> tuple[str, str]:
    if not settings.has_evolution:
        raise HTTPException(400, "Evolution não configurada (defina EVOLUTION_API_URL e EVOLUTION_API_KEY no .env).")
    return settings.evolution_api_url, settings.evolution_api_key


async def _conectar_whatsapp(canal: dict) -> str:
    """Garante a instância na Evolution (criando ou reapontando o webhook) e devolve o QR."""
    url, key = _evo_creds()
    nome = canal["config"].get("instance_name", "")
    if not nome:
        raise HTTPException(400, "Instância não definida para este canal.")
    webhook_url = _url_de_webhook(canal)
    try:
        await evolution.criar_instancia(url, key, nome, webhook_url)
    except Exception as e:
        log.info("Instância %s já existe (%s); reapontando o webhook.", nome, str(e)[:120])
    # Sempre reaponta: é o que coloca o segredo do canal na URL. Sem isso, uma
    # instância já criada continuaria chamando a URL antiga e pararia de entregar.
    try:
        await evolution.atualizar_webhook(url, key, nome, webhook_url)
    except Exception as e:
        log.warning("Não consegui reapontar o webhook de %s: %s", nome, e)
    await repo.patch_canal_config(canal["id"], "status", "scanning")
    return await _buscar_qr(canal)


@app.post("/api/canais/{canal_id}/whatsapp/conectar")
async def connect_whatsapp(canal_id: int, request: Request):
    canal = await _canal_dono(canal_id, usuario_atual(request))
    if canal["tipo"] != "whatsapp":
        raise HTTPException(404, "Canal de WhatsApp não encontrado.")
    qr = await _conectar_whatsapp(canal)
    return {"ok": True, "status": "scanning", "qr": qr}


async def _buscar_qr(canal: dict) -> str:
    cfg = canal["config"]
    url, key = _evo_creds()
    try:
        data = await evolution.obter_qrcode(
            url, key, cfg["instance_name"]
        )
        qr = _qr_strip(data.get("base64") or data.get("code"))
    except Exception:
        qr = ""
    if qr:
        await repo.patch_canal_config(canal["id"], "qr", qr)
    return qr


@app.get("/api/canais/{canal_id}/whatsapp/qr")
async def get_whatsapp_qr(canal_id: int, request: Request):
    canal = await _canal_dono(canal_id, usuario_atual(request))
    if canal["tipo"] != "whatsapp":
        raise HTTPException(404, "Canal de WhatsApp não encontrado.")
    status = canal["config"].get("status", "")
    if status == "open":
        return {"status": "open", "qr": ""}
    qr = await _buscar_qr(canal)
    if qr:
        status = "scanning"
        await repo.patch_canal_config(canal_id, "status", status)
    else:
        status = canal["config"].get("status", "conectando")
    return {"status": status, "qr": qr}


@app.get("/api/caixa")
async def get_caixa(request: Request):
    if not settings.has_db:
        raise HTTPException(503, "Banco de dados não configurado.")
    # Escopada ao dono: o texto das mensagens e o remetente são conversa de
    # cliente, então a fila global só sai para o admin.
    return await repo.resumo_caixa(dono_id=_dono(usuario_atual(request)))


@app.post("/api/canais/{canal_id}/whatsapp/desconectar")
async def disconnect_whatsapp(canal_id: int, request: Request):
    canal = await _canal_dono(canal_id, usuario_atual(request))
    if canal["tipo"] != "whatsapp":
        raise HTTPException(404, "Canal de WhatsApp não encontrado.")
    if settings.has_evolution:
        url, key = _evo_creds()
        try:
            await evolution.desconectar(url, key, canal["config"].get("instance_name", ""))
        except Exception as e:
            log.warning("Falha ao desconectar instância: %s", e)
    await repo.patch_canal_config(canal_id, "status", "")
    await repo.patch_canal_config(canal_id, "qr", "")
    return {"ok": True}


@app.post("/api/canais/{canal_id}/testar")
async def testar_canal(canal_id: int, request: Request):
    canal = await _canal_dono(canal_id, usuario_atual(request))
    cfg = canal["config"]
    try:
        if canal["tipo"] == "telegram":
            info = await telegram.info_bot(cfg.get("token", ""))
            return {"ok": True, "info": f"@{(info or {}).get('username', '?')}"}
        if canal["tipo"] == "whatsapp":
            url, key = _evo_creds()
            st = await evolution.status_instancia(url, key, cfg.get("instance_name", ""))
            return {"ok": True, "info": st.get("instance", {}).get("state", "")}
        if canal["tipo"] == "instagram":
            client = await instagram.obter_cliente(cfg.get("sessionid", ""))
            return {"ok": True, "info": f"@{client.username} (não-oficial)"}
        if canal["tipo"] in TIPOS_CANAL_OFICIAL:
            info = await meta_oficial.verificar(cfg, canal["tipo"])
            return {"ok": True, "info": f"{info} (oficial)"}
    except Exception as e:
        raise HTTPException(400, f"Falha no teste: {e}")


# --------------------------------------------------------------------------
# Administração de contas
#
# Só o admin chega aqui. É a parte que "separa as instâncias": quem é cliente,
# quem é admin, e de quem é cada agente.
# --------------------------------------------------------------------------

@app.get("/api/admin/contas")
async def admin_listar_contas(request: Request):
    exigir_admin(usuario_atual(request))
    return await repo.listar_perfis()


@app.get("/api/admin/contas/{conta_id}")
async def admin_detalhe_conta(conta_id: str, request: Request):
    """Uma conta e os agentes dela. O admin precisa disso para conferir a
    separação sem precisar sair do painel."""
    exigir_admin(usuario_atual(request))
    perfil = await repo.obter_perfil(conta_id)
    if not perfil:
        raise HTTPException(404, "Conta não encontrada.")
    return {
        "perfil": perfil,
        "agentes": await repo.listar_agentes(conta_id),
    }


@app.put("/api/admin/contas/{conta_id}")
async def admin_atualizar_conta(conta_id: str, request: Request):
    """Promove/rebaixa e/ou bloqueia uma conta."""
    admin = exigir_admin(usuario_atual(request))
    body = await request.json()
    role = (body.get("role") or "usuario").strip()
    if role not in ("admin", "usuario"):
        raise HTTPException(400, "Papel inválido: use 'admin' ou 'usuario'.")
    bloqueado = body.get("bloqueado")
    if bloqueado is not None:
        bloqueado = bool(bloqueado)

    alvo = await repo.obter_perfil(conta_id)
    if not alvo:
        raise HTTPException(404, "Conta não encontrada.")

    # Rebaixar ou bloquear o último admin tranca todo mundo fora do painel
    # admin (o cliente não consegue trocar senha de papel, por diseño). Vale a
    # pena barrar na API em vez de explicar depois que a conta ficou órfã.
    perde_admin = alvo["role"] == "admin" and (role != "admin" or bloqueado is True)
    if perde_admin and await repo.contar_admins() <= 1:
        raise HTTPException(
            400,
            "Esta é a última conta de administrador ativa. Promova outra conta "
            "para admin antes de rebaixar ou bloquear esta.",
        )
    if conta_id == admin.id and perde_admin:
        raise HTTPException(400, "Você não pode rebaixar nem bloquear a própria conta de admin.")

    atualizado = await repo.definir_papel(conta_id, role, bloqueado)
    # O cache de 60 s do auth guardaria o papel antigo, então esta conta
    # continuaria admin (ou desbloqueada) por até um minuto sem isso.
    from app.auth import limpar_cache
    limpar_cache(conta_id)
    return atualizado


@app.post("/api/admin/agentes/{agente_id}/transferir")
async def admin_transferir_agente(agente_id: int, request: Request):
    """Move um agente para outra conta. É como se resolve um agente criado sem
    dono ou deixado órfão quando o cadastro do cliente é refeito."""
    exigir_admin(usuario_atual(request))
    body = await request.json()
    dono = (body.get("dono_id") or "").strip() or None
    if dono is not None and not await repo.obter_perfil(dono):
        raise HTTPException(400, "Conta de destino não existe.")
    if dono is not None:
        existentes = await repo.listar_agentes(dono)
        if len(existentes) >= MAX_CANAIS_POR_AGENTE:
            raise HTTPException(400, "A conta de destino já tem o máximo de agentes.")
    r = await repo.definir_dono_agente(agente_id, dono)
    if not r:
        raise HTTPException(404, "Agente não encontrado.")
    return r

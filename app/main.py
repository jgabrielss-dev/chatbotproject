from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app import repositories as repo
from app.channels import evolution, instagram, meta_oficial, telegram
from app.config import settings
from app.database import close_pool, get_pool
from app.pipeline import processar_mensagem

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

RAIZ = Path(__file__).resolve().parent.parent
INDEX_HTML = RAIZ / "index.html"
MAX_CANAIS_POR_AGENTE = 5
MAX_TENTATIVAS_INBOX = 8
INBOX_BACKOFF_BASE_SEG = 5

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


def _publico(canal: dict | None) -> dict | None:
    """Redige os segredos do canal antes de devolver na API."""
    return repo._sem_secrets(canal) if canal else canal


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


# --------------------------------------------------------------------------
# Pipeline de entrada (compartilhado entre os canais)
# --------------------------------------------------------------------------

async def _tratar_mensagem(canal: dict, usuario_externo: str, texto: str):
    agente = await repo.obter_agente(canal["agente_id"])
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
        await telegram.enviar_mensagem(cfg["token"], remetente, resposta)
    elif tipo == "whatsapp":
        if settings.has_evolution and cfg.get("instance_name"):
            url, key = _evo_creds()
            await evolution.enviar_mensagem(url, key, cfg["instance_name"], remetente, resposta)
    elif tipo == "instagram":
        sessionid = cfg.get("sessionid", "")
        if sessionid:
            await instagram.enviar_mensagem(sessionid, remetente, resposta)
    elif tipo in ("whatsapp_oficial", "instagram_oficial"):
        await meta_oficial.enviar(cfg, tipo, remetente, resposta)


def _proxima_tentativa(tentativas: int) -> datetime:
    atraso = min(INBOX_BACKOFF_BASE_SEG * (2 ** min(tentativas - 1, 6)), 900)
    return datetime.now(timezone.utc) + timedelta(seconds=atraso)


async def _processar_caixa(limite: int = 8) -> None:
    await repo.reenfileirar_processando()
    for item in await repo.listar_caixa_para_processar(limite):
        canal = await repo.obter_canal(item["canal_id"])
        if not canal:
            await repo.falhar_caixa(item["id"], item["tentativas"] + 1,
                                    _proxima_tentativa(item["tentativas"] + 1),
                                    "canal não encontrado")
            continue
        if canal["tipo"] == "webhook":
            await repo.concluir_caixa(item["id"], "sem_resposta")
            continue
        await repo.marcar_caixa_processando(item["id"])
        try:
            resposta = await _tratar_mensagem(
                canal, _prefixo_usuario(canal, item["remetente"]), item["texto"]
            )
        except Exception as e:
            log.exception("Falha ao gerar resposta (inbox %s): %s", item["id"], e)
            await repo.falhar_caixa(item["id"], item["tentativas"] + 1,
                                    _proxima_tentativa(item["tentativas"] + 1), str(e))
            continue
        if not resposta:
            await repo.concluir_caixa(item["id"], "sem_resposta")
            continue
        try:
            await _enviar_resposta(canal, item["remetente"], resposta)
        except Exception as e:
            log.exception("Falha ao enviar resposta (inbox %s): %s", item["id"], e)
            await repo.falhar_caixa(item["id"], item["tentativas"] + 1,
                                    _proxima_tentativa(item["tentativas"] + 1), str(e))
            continue
        await repo.concluir_caixa(item["id"], "respondido", resposta)
        log.info("Inbox %s respondida no canal %s (de %s)", item["id"], canal["id"], item["remetente"])


async def _sincronizar_evolution(intervalo: float = 30.0) -> None:
    """Rede de segurança do WhatsApp: se um webhook caiu (app dormindo/hibernação),
    busca as mensagens recebidas na Evolution e as enfileira para responder depois."""
    if not settings.has_evolution:
        return
    url, key = settings.evolution_api_url, settings.evolution_api_key
    while True:
        try:
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.has_db:
        await get_pool()
        global _worker_caixa_task
        _worker_caixa_task = asyncio.create_task(_worker_caixa())
        # Só há tarefa de fundo nos canais NÃO OFICIAIS. Os oficiais são
        # 100% push (webhook), então nada de polling/keepalive: é isso que
        # permite ao Render passar o mês hibernando.
        await _reconciliar_tarefas_de_fundo()
    yield
    for tarefa in (_poller_task, _keepalive_task, _worker_caixa_task, _sync_task):
        if tarefa:
            tarefa.cancel()
    await close_pool()


app = FastAPI(title="Chatbot Project SaaS", lifespan=lifespan)

# A pagina tambem pode ser hospedada no GitHub Pages, entao liberamos CORS
# para que o navegador consiga chamar esta API de outra origem.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(INDEX_HTML)


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
    if not canal or canal["tipo"] != "telegram" or canal["config"].get("secret") != secret:
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


@app.post("/webhook/evolution/{canal_id}")
async def webhook_evolution(canal_id: int, request: Request):
    payload = await request.json()
    canal = await repo.obter_canal(canal_id)
    if not canal or canal["tipo"] != "whatsapp":
        return JSONResponse({"ok": False}, status_code=404)

    cfg = canal["config"]
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
    if not settings.has_db:
        raise HTTPException(503, "Banco de dados não configurado.")
    canal = await repo.obter_canal(canal_id)
    if not canal or canal["tipo"] != "webhook" or canal["config"].get("secret") != secret:
        return JSONResponse({"ok": False}, status_code=404)

    payload = await request.json()
    texto = str(payload.get("text", "")).strip()
    usuario = str(payload.get("user", "anonimo") or "anonimo")
    if texto:
        resposta = await _tratar_mensagem(canal, f"web:{usuario}", texto)
        await repo.salvar_na_caixa(
            canal_id, usuario, texto, origem="",
            payload=payload, status="respondido", resposta=resposta or "",
        )
        return JSONResponse({"reply": resposta or ""})
    return JSONResponse({"reply": ""})


# --------------------------------------------------------------------------
# Gestão de agentes
# --------------------------------------------------------------------------

@app.get("/api/agentes")
async def get_agentes():
    return await repo.listar_agentes()


@app.post("/api/agentes")
async def post_agente(request: Request):
    body = await request.json()
    nome = (body.get("nome") or "").strip()
    prompt = (body.get("system_prompt") or "").strip()
    if not nome:
        raise HTTPException(400, "Informe o nome do agente.")
    if not prompt:
        raise HTTPException(400, "Informe o system prompt.")
    return await repo.criar_agente(nome, prompt)


@app.put("/api/agentes/{agente_id}")
async def put_agente(agente_id: int, request: Request):
    body = await request.json()
    nome = (body.get("nome") or "").strip()
    prompt = (body.get("system_prompt") or "").strip()
    ativo = bool(body.get("ativo", True))
    if not nome or not prompt:
        raise HTTPException(400, "Nome e system prompt são obrigatórios.")
    r = await repo.atualizar_agente(agente_id, nome, prompt, ativo)
    if not r:
        raise HTTPException(404, "Agente não encontrado.")
    return r


@app.delete("/api/agentes/{agente_id}")
async def delete_agente(agente_id: int):
    canais = await repo.listar_canais(agente_id)
    if not await repo.excluir_agente(agente_id):
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
async def get_canais(agente_id: int):
    return await repo.listar_canais(agente_id, redigir=True)


@app.post("/api/agentes/{agente_id}/canais")
async def post_canal(agente_id: int, request: Request):
    if not await repo.obter_agente(agente_id):
        raise HTTPException(404, "Agente não encontrado.")
    body = await request.json()
    tipo = body.get("tipo", "").strip()
    nome = (body.get("nome") or "").strip()
    config = dict(body.get("config") or {})
    if tipo not in TIPOS_CANAL:
        raise HTTPException(400, "Tipo de canal inválido.")
    if not nome:
        raise HTTPException(400, "Informe um nome para o canal.")

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
            return _publico(canal)
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
    return _publico(canal)


@app.put("/api/canais/{canal_id}")
async def put_canal(canal_id: int, request: Request):
    body = await request.json()
    nome = (body.get("nome") or "").strip()
    ativo = bool(body.get("ativo", True))
    novos = dict(body.get("config") or {})
    if not nome:
        raise HTTPException(400, "Informe um nome para o canal.")
    atual = await repo.obter_canal(canal_id)
    if not atual:
        raise HTTPException(404, "Canal não encontrado.")

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
    config["secret"] = atual["config"].get("secret", _gerar_secret())
    salvo = await repo.atualizar_canal(canal_id, nome, config, ativo)
    await _reconciliar_tarefas_de_fundo()
    return _publico(salvo)


@app.delete("/api/canais/{canal_id}")
async def delete_canal(canal_id: int):
    canal = await repo.obter_canal(canal_id)
    if not canal:
        raise HTTPException(404, "Canal não encontrado.")
    await repo.excluir_canal(canal_id)
    if canal["tipo"] == "whatsapp" and settings.has_evolution and canal["config"].get("instance_name"):
        try:
            await evolution.deletar_instancia(
                settings.evolution_api_url, settings.evolution_api_key, canal["config"]["instance_name"]
            )
        except Exception as e:
            log.warning("Falha ao remover instância Evolution do canal %s: %s", canal_id, e)
    await _reconciliar_tarefas_de_fundo()
    return {"ok": True}


# --------------------------------------------------------------------------
# Ações por tipo de canal
# --------------------------------------------------------------------------

def _url_de_webhook(canal: dict) -> str:
    base = settings.base_url
    inbox = settings.supabase_functions_base
    if canal["tipo"] == "telegram":
        if inbox:
            return f"{inbox}/telegram/{canal['id']}/{canal['config'].get('secret')}"
        if not base:
            raise HTTPException(400, "Configure a variável BASE_URL (ou SUPABASE_FUNCTIONS_BASE) no .env para gerar webhooks.")
        return f"{base}/webhook/telegram/{canal['id']}/{canal['config'].get('secret')}"
    if canal["tipo"] == "whatsapp":
        if inbox:
            return f"{inbox}/evolution/{canal['id']}"
        return f"{base}/webhook/evolution/{canal['id']}"
    if canal["tipo"] == "webhook":
        if not base:
            raise HTTPException(400, "Configure a variável BASE_URL no .env para gerar webhooks.")
        return f"{base}/webhook/generico/{canal['id']}/{canal['config'].get('secret')}"
    return ""


@app.get("/api/canais/{canal_id}/webhook-url")
async def get_webhook_url(canal_id: int):
    canal = await repo.obter_canal(canal_id)
    if not canal:
        raise HTTPException(404, "Canal não encontrado.")
    return {"url": _url_de_webhook(canal)}


@app.post("/api/canais/{canal_id}/telegram/set-webhook")
async def set_telegram_webhook(canal_id: int):
    canal = await repo.obter_canal(canal_id)
    if not canal or canal["tipo"] != "telegram":
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
    """Cria a instância na Evolution (ou usa a existente) e devolve o QR."""
    url, key = _evo_creds()
    nome = canal["config"].get("instance_name", "")
    if not nome:
        raise HTTPException(400, "Instância não definida para este canal.")
    try:
        await evolution.criar_instancia(url, key, nome, _url_de_webhook(canal))
    except Exception as e:
        log.info("Criar instância %s: %s", nome, e)
    await repo.patch_canal_config(canal["id"], "status", "scanning")
    return await _buscar_qr(canal)


@app.post("/api/canais/{canal_id}/whatsapp/conectar")
async def connect_whatsapp(canal_id: int):
    canal = await repo.obter_canal(canal_id)
    if not canal or canal["tipo"] != "whatsapp":
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
async def get_whatsapp_qr(canal_id: int):
    canal = await repo.obter_canal(canal_id)
    if not canal or canal["tipo"] != "whatsapp":
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
async def get_caixa():
    if not settings.has_db:
        raise HTTPException(503, "Banco de dados não configurado.")
    return await repo.resumo_caixa()


@app.post("/api/canais/{canal_id}/whatsapp/desconectar")
async def disconnect_whatsapp(canal_id: int):
    canal = await repo.obter_canal(canal_id)
    if not canal or canal["tipo"] != "whatsapp":
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
async def testar_canal(canal_id: int):
    canal = await repo.obter_canal(canal_id)
    if not canal:
        raise HTTPException(404, "Canal não encontrado.")
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
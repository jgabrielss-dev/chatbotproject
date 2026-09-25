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

from app import repositories as repo
from app.channels import evolution, instagram, telegram
from app.config import settings
from app.database import close_pool, get_pool
from app.pipeline import processar_mensagem

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

RAIZ = Path(__file__).resolve().parent.parent
INDEX_HTML = RAIZ / "index.html"
MAX_CANAIS_POR_AGENTE = 5

# Janela que o webhook generico espera o worker responder antes de devolver
# "ainda na fila". O pedido NUNCA e perdido: expirado o prazo, a mensagem segue
# na caixa_entrada e e respondida assim que o worker conseguir.
WEBHOOK_GENERICO_ESPERA_SEG = 25.0

_poller_task: asyncio.Task | None = None
_keepalive_task: asyncio.Task | None = None
_worker_caixa_task: asyncio.Task | None = None
_sync_task: asyncio.Task | None = None


def _gerar_secret() -> str:
    return secrets.token_urlsafe(16)


def _secret_igual(a: str, b: str) -> bool:
    """Compara segredos em tempo constante (evita vazar o valor por tempo)."""
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def _canal_publico(canal: dict | None) -> dict | None:
    """Copia do canal com os segredos mascarados, para ir ao navegador."""
    if not canal:
        return canal
    saida = dict(canal)
    saida["config"] = repo.redigir_config(canal.get("config") or {})
    return saida


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
                usuario = cfg.get("usuario", "")
                senha = cfg.get("senha", "")
                sessionid = cfg.get("sessionid", "")
                if not usuario or not (senha or sessionid):
                    continue
                novos, vistos = await instagram.coletar_novas(
                    usuario, senha, sessionid, cfg.get("ig_vistos")
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
# Keepalive Evolution (free tier hiberna apos ~15 min sem trafego)
# --------------------------------------------------------------------------

async def _keepalive_evolution(intervalo: float = 240.0) -> None:
    if not settings.has_evolution:
        return
    log.info("Keepalive Evolution iniciado")
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
    if canal["tipo"] == "telegram":
        return f"tg:{remetente}"
    if canal["tipo"] == "whatsapp":
        return f"wa:{remetente}"
    if canal["tipo"] == "instagram":
        return f"ig:{remetente}"
    return f"web:{remetente}"


async def _enviar_resposta(canal: dict, remetente: str, resposta: str) -> None:
    cfg = canal["config"]
    if canal["tipo"] == "telegram":
        if not cfg.get("token"):
            raise RuntimeError("canal de telegram sem token definido")
        await telegram.enviar_mensagem(cfg["token"], remetente, resposta)
    elif canal["tipo"] == "whatsapp":
        if not (settings.has_evolution and cfg.get("instance_name")):
            raise RuntimeError("canal de whatsapp sem Evolution configurada")
        url, key = _evo_creds()
        await evolution.enviar_mensagem(url, key, cfg["instance_name"], remetente, resposta)
    elif canal["tipo"] == "instagram":
        usuario, senha = cfg.get("usuario", ""), cfg.get("senha", "")
        sessionid = cfg.get("sessionid", "")
        if not (usuario and (senha or sessionid)):
            raise RuntimeError("canal de instagram sem usuario/senha/sessionid")
        await instagram.enviar_mensagem(usuario, senha, sessionid, remetente, resposta)


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


async def _reconciliar_webhooks_evolution() -> None:
    """Confere, uma vez por cold start, se a URL de webhook registrada na
    Evolution é a que o app espera hoje. Cobre a migração para a URL com
    segredo (a instância antiga continuaria chamando a URL velha e pararia de
    entregar) e qualquer reconfiguração de BASE_URL. Uma chamada por canal."""
    if not settings.has_evolution:
        return
    url, key = settings.evolution_api_url, settings.evolution_api_key
    for canal in await repo.listar_canais():
        if canal["tipo"] != "whatsapp" or not canal["config"].get("instance_name"):
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.has_db:
        await get_pool()
        global _poller_task, _keepalive_task, _worker_caixa_task, _sync_task
        try:
            await _reconciliar_webhooks_evolution()
        except Exception as e:
            log.warning("Falha ao reconciliar webhooks da Evolution: %s", e)
        _poller_task = asyncio.create_task(_poller_instagram())
        _keepalive_task = asyncio.create_task(_keepalive_evolution())
        _worker_caixa_task = asyncio.create_task(_worker_caixa())
        _sync_task = asyncio.create_task(_sincronizar_evolution())
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

# Rotas publicas: a pagina (o shell vazio), o health check usado pelo Render e
# pela edge function para acordar o app, e os webhooks (que tem segredo proprio
# na propria rota, checked la dentro).
_ROTAS_PUBLICAS = ("/", "/health")


@app.middleware("http")
async def exigir_admin(request: Request, call_next):
    """Exige X-Admin-Token em tudo que nao for rota publica/webhook.

    Sem isto, qualquer pessoa na internet lia o token do bot de Telegram em
    /api/agentes/{id}/canais e ainda podia apagar agentes e canais.
    Falha fechada: sem ADMIN_TOKEN configurado, /api/* nao responde.
    """
    caminho = request.url.path
    if caminho in _ROTAS_PUBLICAS or caminho.startswith("/webhook/"):
        return await call_next(request)
    # O preflight do navegador (GitHub Pages -> API) nao leva o X-Admin-Token.
    # Se fosse barrado aqui, o CORS nem responderia e a pagina ficaria muda.
    if request.method == "OPTIONS":
        return await call_next(request)
    if not settings.has_admin:
        return JSONResponse(
            {"detail": "ADMIN_TOKEN não configurado no servidor: a API está fechada."},
            status_code=503,
        )
    if not _secret_igual(request.headers.get("x-admin-token", ""), settings.admin_token):
        return JSONResponse({"detail": "Token de acesso inválido ou ausente."}, status_code=401)
    return await call_next(request)


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
    return [_canal_publico(c) for c in await repo.listar_canais(agente_id)]


@app.post("/api/agentes/{agente_id}/canais")
async def post_canal(agente_id: int, request: Request):
    if not await repo.obter_agente(agente_id):
        raise HTTPException(404, "Agente não encontrado.")
    body = await request.json()
    tipo = body.get("tipo", "").strip()
    nome = (body.get("nome") or "").strip()
    config = dict(body.get("config") or {})
    if tipo not in ("telegram", "whatsapp", "instagram", "webhook"):
        raise HTTPException(400, "Tipo de canal inválido.")
    if not nome:
        raise HTTPException(400, "Informe um nome para o canal.")

    existentes = await repo.listar_canais(agente_id)
    if len(existentes) >= MAX_CANAIS_POR_AGENTE:
        raise HTTPException(400, f"Cada agente aceita no máximo {MAX_CANAIS_POR_AGENTE} canais.")

    config["secret"] = _gerar_secret()
    if tipo == "whatsapp":
        config["instance_name"] = f"{settings.evolution_instance_prefix}{agente_id}x{secrets.token_hex(3)}"
        canal = await repo.criar_canal(agente_id, tipo, nome, config)
        try:
            qr = await _conectar_whatsapp(canal)
            canal = await repo.obter_canal(canal["id"])
            saida = _canal_publico(canal)
            saida["config"]["qr"] = qr
            saida["config"]["status"] = saida["config"].get("status", "scanning")
            return saida
        except HTTPException:
            await repo.excluir_canal(canal["id"])
            raise
        except Exception as e:
            await repo.excluir_canal(canal["id"])
            raise HTTPException(400, f"Falha ao iniciar WhatsApp: {e}")
    return _canal_publico(await repo.criar_canal(agente_id, tipo, nome, config))


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

    config = dict(atual["config"])
    for chave, valor in novos.items():
        # O painel reenvia o valor mascarado de um segredo que ele não editou;
        # ignorá-lo evita sobrescrever o token/senha guardado com "********".
        if valor == repo.MASCARA:
            continue
        if valor is None or (isinstance(valor, str) and not valor.strip()):
            continue
        config[chave] = valor.strip() if isinstance(valor, str) else valor
    config["secret"] = atual["config"].get("secret", _gerar_secret())
    config["instance_name"] = atual["config"].get("instance_name", config.get("instance_name", ""))
    return _canal_publico(await repo.atualizar_canal(canal_id, nome, config, ativo))


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
    if canal["tipo"] == "instagram" and canal["config"].get("usuario"):
        try:
            await repo.excluir_sessao_instagram(canal["config"]["usuario"])
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
            await instagram.obter_cliente(
                cfg.get("usuario", ""), cfg.get("senha", ""), cfg.get("sessionid", "")
            )
            return {"ok": True, "info": "logado"}
        return {"ok": True, "info": "webhook pronto"}
    except Exception as e:
        raise HTTPException(400, f"Falha no teste: {e}")
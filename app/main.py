from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app import repositories as repo
from app.channels import evolution, instagram, telegram
from app.config import settings
from app.database import close_pool, get_pool
from app.pipeline import processar_mensagem

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

STATIC_DIR = Path(__file__).parent / "web" / "admin"
MAX_CANAIS_POR_AGENTE = 5

_poller_task: asyncio.Task | None = None


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
                usuario = cfg.get("usuario", "")
                senha = cfg.get("senha", "")
                if not usuario or not senha:
                    continue
                novos, vistos = await instagram.coletar_novas(usuario, senha, cfg.get("ig_vistos"))
                if vistos != cfg.get("ig_vistos"):
                    await repo.patch_canal_config(canal["id"], "ig_vistos", vistos)
                for thread_id, msg_id, texto in novos:
                    try:
                        log.info("Instagram DM de %s (canal %s)", thread_id, canal["id"])
                        resposta = await _tratar_mensagem(canal, f"ig:{thread_id}", texto)
                        if resposta:
                            await instagram.enviar_mensagem(usuario, senha, thread_id, resposta)
                    except Exception as e:
                        log.exception("Falha ao responder DM no canal %s: %s", canal["id"], e)
        except Exception as e:
            log.exception("Erro no poller Instagram: %s", e)
        await asyncio.sleep(intervalo)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.has_db:
        await get_pool()
        global _poller_task
        _poller_task = asyncio.create_task(_poller_instagram())
    yield
    if _poller_task:
        _poller_task.cancel()
    await close_pool()


app = FastAPI(title="Chatbot Project SaaS", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


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
        try:
            resposta = await _tratar_mensagem(canal, f"tg:{chat_id}", texto)
            if resposta:
                await telegram.enviar_mensagem(canal["config"]["token"], chat_id, resposta)
        except Exception as e:
            log.exception("Erro no webhook Telegram (canal %s): %s", canal_id, e)
    return JSONResponse({"ok": True})


@app.post("/webhook/evolution/{canal_id}")
async def webhook_evolution(canal_id: int, request: Request):
    payload = await request.json()
    canal = await repo.obter_canal(canal_id)
    if not canal or canal["tipo"] != "whatsapp":
        return JSONResponse({"ok": False}, status_code=404)

    cfg = canal["config"]
    evento = payload.get("event")

    if evento == "QRCODE_UPDATED":
        qr = payload.get("data", {}).get("base64") or payload.get("data", {}).get("code")
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

    texto, numero, _ = evolution.extrair_mensagem(payload)
    if texto and numero and cfg.get("server_url") and cfg.get("apikey"):
        try:
            resposta = await _tratar_mensagem(canal, f"wa:{numero}", texto)
            if resposta:
                await evolution.enviar_mensagem(
                    cfg["server_url"], cfg["apikey"], cfg["instance_name"], numero, resposta
                )
        except Exception as e:
            log.exception("Erro no webhook Evolution (canal %s): %s", canal_id, e)
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
    if not await repo.excluir_agente(agente_id):
        raise HTTPException(404, "Agente não encontrado.")
    return {"ok": True}


# --------------------------------------------------------------------------
# Gestão de canais
# --------------------------------------------------------------------------

@app.get("/api/agentes/{agente_id}/canais")
async def get_canais(agente_id: int):
    return await repo.listar_canais(agente_id)


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
    return await repo.criar_canal(agente_id, tipo, nome, config)


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
        if valor is None or (isinstance(valor, str) and not valor.strip()):
            continue
        config[chave] = valor.strip() if isinstance(valor, str) else valor
    config["secret"] = atual["config"].get("secret", _gerar_secret())
    config["instance_name"] = atual["config"].get("instance_name", config.get("instance_name", ""))
    return await repo.atualizar_canal(canal_id, nome, config, ativo)


@app.delete("/api/canais/{canal_id}")
async def delete_canal(canal_id: int):
    if not await repo.excluir_canal(canal_id):
        raise HTTPException(404, "Canal não encontrado.")
    return {"ok": True}


# --------------------------------------------------------------------------
# Ações por tipo de canal
# --------------------------------------------------------------------------

def _url_de_webhook(canal: dict) -> str:
    base = settings.base_url
    if not base:
        raise HTTPException(400, "Configure a variável BASE_URL no .env para gerar webhooks.")
    if canal["tipo"] == "telegram":
        return f"{base}/webhook/telegram/{canal['id']}/{canal['config'].get('secret')}"
    if canal["tipo"] == "whatsapp":
        return f"{base}/webhook/evolution/{canal['id']}"
    if canal["tipo"] == "webhook":
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


@app.post("/api/canais/{canal_id}/whatsapp/conectar")
async def connect_whatsapp(canal_id: int):
    canal = await repo.obter_canal(canal_id)
    if not canal or canal["tipo"] != "whatsapp":
        raise HTTPException(404, "Canal de WhatsApp não encontrado.")
    cfg = canal["config"]
    if not cfg.get("server_url") or not cfg.get("apikey"):
        raise HTTPException(400, "Defina a URL do servidor Evolution e a API key first.")
    try:
        await evolution.criar_instancia(
            cfg["server_url"], cfg["apikey"], cfg["instance_name"], _url_de_webhook(canal)
        )
    except Exception as e:
        raise HTTPException(400, f"Falha ao criar instância: {e}")
    qr = await _buscar_qr(canal)
    return {"ok": True, "status": "scanning", "qr": qr}


async def _buscar_qr(canal: dict) -> str:
    cfg = canal["config"]
    try:
        data = await evolution.obter_qrcode(
            cfg["server_url"], cfg["apikey"], cfg["instance_name"]
        )
        qr = data.get("base64") or (data.get("code") or "").replace("data:image/png;base64,", "")
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
    if not status:
        qr = await _buscar_qr(canal)
        status = canal["config"].get("status", "conectando")
    return {"status": status, "qr": canal["config"].get("qr", "")}


@app.post("/api/canais/{canal_id}/whatsapp/desconectar")
async def disconnect_whatsapp(canal_id: int):
    canal = await repo.obter_canal(canal_id)
    if not canal or canal["tipo"] != "whatsapp":
        raise HTTPException(404, "Canal de WhatsApp não encontrado.")
    cfg = canal["config"]
    if cfg.get("server_url") and cfg.get("apikey"):
        try:
            await evolution.desconectar(cfg["server_url"], cfg["apikey"], cfg["instance_name"])
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
            st = await evolution.status_instancia(cfg["server_url"], cfg["apikey"], cfg["instance_name"])
            return {"ok": True, "info": st.get("instance", {}).get("state", "")}
        if canal["tipo"] == "instagram":
            await instagram.obter_cliente(cfg["usuario"], cfg["senha"])
            return {"ok": True, "info": "logado"}
        return {"ok": True, "info": "webhook pronto"}
    except Exception as e:
        raise HTTPException(400, f"Falha no teste: {e}")
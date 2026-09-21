from __future__ import annotations

import httpx

TELEGRAM_API = "https://api.telegram.org"


def _url(token: str, metodo: str) -> str:
    return f"{TELEGRAM_API}/bot{token}/{metodo}"


async def enviar_mensagem(token: str, chat_id: int | str, texto: str) -> None:
    """Envia mensagem para um chat do Telegram."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            _url(token, "sendMessage"),
            json={"chat_id": chat_id, "text": texto},
        )
        resp.raise_for_status()


async def definir_webhook(token: str, url: str) -> dict:
    """Registra o webhook do bot (o Telegram passa a enviar updates para url)."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            _url(token, "setWebhook"),
            json={"url": url, "allowed_updates": ["message"]},
        )
        resp.raise_for_status()
        return resp.json()


async def info_bot(token: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(_url(token, "getMe"))
        resp.raise_for_status()
        return resp.json().get("result", {})


def extrair_mensagem(payload: dict) -> tuple[str | None, str | None]:
    """Retorna (conversa, chat_id) a partir do update do Telegram."""
    msg = payload.get("message") or {}
    chat = msg.get("chat") or {}
    texto = msg.get("text")
    if texto in ("/start", "/begin"):
        texto = "/start"
    return texto, str(chat.get("id")) if chat else None
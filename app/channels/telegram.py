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


async def info_webhook(token: str) -> dict:
    """Webhook atualmente registrado no Telegram (inclui a url)."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(_url(token, "getWebhookInfo"))
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


# --------------------------------------------------------------------------
# Anexos (item 15)
# --------------------------------------------------------------------------

#: Ordem importa: `video_note` e `voice` também podem vir com `video`/`audio` no
#: mesmo update em updates antigos, e o Telegram manda o maior primeiro em
#: `photo`. Pegar o primeiro de cada lista é o que a API de documented diz.
_CAMPO_ANEXO = ("video_note", "video", "audio", "voice", "document", "photo")

#: Tipos que o Telegram entrega, traduzidos para os quatro do item 15.
#: `voice` e `video_note` viram áudio/vídeo: os dois são formatosproprio do
#: Telegram (ogg opus, mp4 sem som) que o Gemini entende pelo mime.
def _tipo_do_telegram(campo: str, mime: str) -> str:
    if campo in ("voice", "audio"):
        return "audio"
    if campo in ("video", "video_note"):
        return "video"
    if campo == "photo":
        return "foto"
    from app.midia import classificar
    return classificar(mime) or "arquivo"


def extrair_anexo(payload: dict) -> dict | None:
    """Referência do anexo de um update do Telegram, ou None se é só texto.

    Devolve a forma que `app/midia.py` entende: `{tipo, mime, nome, fonte}` com
    `fonte = {"ref": file_id}`. Quem baixa é `baixar_anexo`, porque o token do
    bot fica no `canais.config` e não no update.
    """
    msg = payload.get("message") or payload.get("edited_message") or {}
    for campo in _CAMPO_ANEXO:
        bruto = msg.get(campo)
        if not bruto:
            continue
        if campo == "photo":
            # O Telegram manda a mesma foto em vários tamanhos, do menor pro
            # maior. O último é o original.
            bruto = bruto[-1] if isinstance(bruto, list) else bruto
        if not isinstance(bruto, dict) or not bruto.get("file_id"):
            continue
        mime = bruto.get("mime_type") or ""
        if not mime:
            mime = {"photo": "image/jpeg", "voice": "audio/ogg",
                    "video_note": "video/mp4"}.get(campo, "")
        tipo = _tipo_do_telegram(campo, mime)
        nome = (bruto.get("file_name")
                or f"{campo}_{bruto.get('file_unique_id', '') or bruto['file_id'][-12:]}")
        return {"tipo": tipo, "mime": mime, "nome": nome, "fonte": {"ref": bruto["file_id"]}}
    return None


async def baixar_anexo(token: str, file_id: str) -> tuple[str, bytes]:
    """Baixa um arquivo do Telegram. Devolve (mime, bytes).

    Duas chamadas, e a ordem importa: `getFile` devolve só o caminho, e esse
    caminho vale por uma hora. Se o download falhar, o erro sobe — quem chama
    (`app/pipeline.py`) trata e diz ao usuário em vez de engolir.
    """
    if not token:
        raise RuntimeError("canal de Telegram sem token")
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        resp = await client.post(_url(token, "getFile"), json={"file_id": file_id})
        resp.raise_for_status()
        arquivo = (resp.json() or {}).get("result") or {}
        caminho = arquivo.get("file_path") or ""
        mime = arquivo.get("mime_type") or "application/octet-stream"
        if not caminho:
            raise RuntimeError("Telegram nao devolveu file_path (arquivo grande demais?)")
        baixa = await client.get(f"{TELEGRAM_API}/file/bot{token}/{caminho}")
        baixa.raise_for_status()
        return str(mime).split(";")[0], baixa.content

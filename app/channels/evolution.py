from __future__ import annotations

import httpx


async def criar_instancia(
    server_url: str, apikey: str, instance_name: str, webhook_url: str
) -> dict:
    """Cria a instância WhatsApp na Evolution API com webhook apontando pro nosso app."""
    url = f"{server_url.rstrip('/')}/instance/create"
    payload = {
        "instanceName": instance_name,
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS",
        "webhook": {
            "enabled": True,
            "url": webhook_url,
            "byEvents": False,
            "base64": False,
            "events": ["MESSAGES_UPSERT", "QRCODE_UPDATED", "CONNECTION_UPDATE"],
        },
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers={"apikey": apikey})
        resp.raise_for_status()
        return resp.json()


async def obter_qrcode(server_url: str, apikey: str, instance_name: str) -> dict:
    """Retorna {base64, code, status} do QR atual da instância."""
    url = f"{server_url.rstrip('/')}/instance/connect/{instance_name}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers={"apikey": apikey})
        resp.raise_for_status()
        return resp.json()


async def status_instancia(server_url: str, apikey: str, instance_name: str) -> dict:
    url = f"{server_url.rstrip('/')}/instance/connectionState/{instance_name}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers={"apikey": apikey})
        resp.raise_for_status()
        return resp.json()


async def desconectar(server_url: str, apikey: str, instance_name: str) -> None:
    url = f"{server_url.rstrip('/')}/instance/logout/{instance_name}"
    async with httpx.AsyncClient(timeout=20) as client:
        await client.delete(url, headers={"apikey": apikey})


async def deletar_instancia(server_url: str, apikey: str, instance_name: str) -> None:
    url = f"{server_url.rstrip('/')}/instance/delete/{instance_name}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.delete(url, headers={"apikey": apikey})
        resp.raise_for_status()


async def enviar_mensagem(
    server_url: str, apikey: str, instance_name: str, numero: str, texto: str
) -> None:
    url = f"{server_url.rstrip('/')}/message/sendText/{instance_name}"
    payload = {"number": numero, "text": texto}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers={"apikey": apikey})
        resp.raise_for_status()


async def listar_mensagens(
    server_url: str, apikey: str, instance_name: str, limite: int = 50
) -> list[dict]:
    """Retorna mensagens RECEBIDAS (fromMe=false) recentes da instância.
    Cada item: {numero, texto, origem_id, ts, dados} - usado como rede de segurança
    para nunca perder mensagem cujo webhook caiu (ex.: app dormindo)."""
    url = f"{server_url.rstrip('/')}/chat/findMessages/{instance_name}"
    payload = {"take": limite, "orderBy": {"messageTimestamp": "desc"}}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers={"apikey": apikey})
        resp.raise_for_status()
        records = (resp.json().get("messages") or {}).get("records") or []
    saidas = []
    for rec in records:
        key = rec.get("key") or {}
        if key.get("fromMe"):
            continue
        msg = rec.get("message") or {}
        texto = msg.get("conversation") or (msg.get("extendedTextMessage") or {}).get("text")
        if not texto:
            continue
        remote = str(key.get("remoteJid", "")).split("@")[0]
        saidas.append(
            {
                "numero": remote,
                "texto": texto,
                "origem_id": str(key.get("id", "") or ""),
                "ts": float(rec.get("messageTimestamp") or 0),
                "dados": rec,
            }
        )
    return saidas


def extrair_mensagem(payload: dict) -> tuple[str | str | None, str | None, dict | None]:
    """Retorna (texto, remoteJid, dados) de um evento MESSAGES_UPSERT."""
    evento = (payload.get("event") or "").upper().replace(".", "_")
    if evento != "MESSAGES_UPSERT":
        return None, None, None
    data = payload.get("data") or {}
    key = data.get("key") or {}
    if key.get("fromMe"):
        return None, None, None
    msg = data.get("message") or {}
    ext = msg.get("extendedTextMessage") or {}
    texto = msg.get("conversation") or ext.get("text")
    remote = key.get("remoteJid")
    if remote:
        remote = str(remote).split("@")[0]
    return texto, remote, data
from __future__ import annotations

import httpx

#: teto de um anexo, o mesmo que o `midia` aplica aos outros canais.
_LIMITE_ARQUIVO = 15 * 1024 * 1024


WEBHOOK_EVENTOS = ["MESSAGES_UPSERT", "QRCODE_UPDATED", "CONNECTION_UPDATE"]


def _webhook(url: str) -> dict:
    """Formato de /instance/create."""
    return {
        "enabled": True,
        "url": url,
        "byEvents": False,
        "base64": False,
        "events": WEBHOOK_EVENTOS,
    }


def _webhook_set(url: str) -> dict:
    """Formato de /webhook/set: as chaves de byEvents/base64 ganham o prefixo
    'webhook' (verificado na Evolution v2.3.7)."""
    return {
        "enabled": True,
        "url": url,
        "webhookByEvents": False,
        "webhookBase64": False,
        "events": WEBHOOK_EVENTOS,
    }


async def criar_instancia(
    server_url: str, apikey: str, instance_name: str, webhook_url: str
) -> dict:
    """Cria a instância WhatsApp na Evolution API com webhook apontando pro nosso app."""
    url = f"{server_url.rstrip('/')}/instance/create"
    payload = {
        "instanceName": instance_name,
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS",
        "webhook": _webhook(webhook_url),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers={"apikey": apikey})
        resp.raise_for_status()
        return resp.json()


async def atualizar_webhook(
    server_url: str, apikey: str, instance_name: str, webhook_url: str
) -> dict:
    """Reaponta o webhook de uma instância que já existe (Evolution v2).

    Necessário porque a URL agora carrega o segredo do canal: uma instância
    criada antes disso continua chamando a URL antiga (sem segredo) e seria
    rejeitada. `criar_instancia` não serve aqui: ela falha em instância
    existente e o erro era engolido.
    """
    url = f"{server_url.rstrip('/')}/webhook/set/{instance_name}"
    payload = {"instance": instance_name, "webhook": _webhook_set(webhook_url)}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers={"apikey": apikey})
        resp.raise_for_status()
        return resp.json()


async def obter_webhook(server_url: str, apikey: str, instance_name: str) -> dict:
    """Configuração de webhook registrada na Evolution (para conferência)."""
    url = f"{server_url.rstrip('/')}/webhook/find/{instance_name}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers={"apikey": apikey})
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


def extrair_mensagem(payload: dict) -> tuple[str | None, str | None, dict | None]:
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


# --------------------------------------------------------------------------
# Anexos (item 15)
# --------------------------------------------------------------------------

#: Chave de cada tipo no corpo da Evolution -> tipo do item 15. A Evolution
#: manda `stickerMessage` e `contactMessage` também; os dois caem em `arquivo`
#: (o sticker chega como webp) e o contato é texto, tratado antes daqui.
_CAMPO_EVOLUTION = (
    ("imageMessage", "foto"),
    ("audioMessage", "audio"),
    ("videoMessage", "video"),
    ("documentMessage", "arquivo"),
    ("stickerMessage", "foto"),
)


def extrair_anexo(dados: dict) -> dict | None:
    """Referência do anexo de um MESSAGES_UPSERT, ou None se é só texto.

    A Evolution pode mandar o arquivo de duas formas, e as duas continuam
    válidas: `mediaUrl` (link público, o padrão) e `base64`/`media` (a
    configuração `ALLOW_BASE64`/`BASE64_MEDIA` do Baileys). A segunda é
    guardada inteira no `payload_json` de qualquer forma, então não há custo
    novo em referenciá-la aqui.

    `legenda` é o texto que o cliente escreveu junto do arquivo ("isso aqui
    quebrou"), e é o que a pessoa quer que o agente leia: sem ela, a única coisa
    que sobra é "[foto] IMG.jpg", e o agente responde sobre a imagem em vez de
    sobre o problema. `Midia.de_dict` ignora a chave — ela serve só ao webhook,
    que a usa como texto da mensagem.
    """
    msg = (dados or {}).get("message") or {}
    for campo, tipo in _CAMPO_EVOLUTION:
        corpo = msg.get(campo)
        if not isinstance(corpo, dict):
            continue
        nome = (corpo.get("fileName") or corpo.get("fileEncSha256")
                or corpo.get("mimetype") or f"{campo}")
        mime = corpo.get("mimetype") or ""
        legenda = corpo.get("caption") if isinstance(corpo.get("caption"), str) else ""
        base64_ = (corpo.get("media") or corpo.get("base64") or "")
        url = corpo.get("mediaUrl") or corpo.get("url") or corpo.get("link") or ""
        if base64_:
            return {"tipo": tipo, "mime": mime, "nome": nome,
                    "fonte": {"base64": base64_}, "legenda": legenda or ""}
        if url:
            return {"tipo": tipo, "mime": mime, "nome": nome,
                    "fonte": {"url": url}, "legenda": legenda or ""}
    return None


async def baixar_anexo(instancia: str, ref: str) -> tuple[str, bytes]:
    """Baixa o arquivo pela Evolution, usando a URL que a instância guarda.

    A Evolution não tem "download por id": o que ela guarda é a URL (que é
    pública e não expira) e o `mediaUrl` que veio no webhook. Aqui o `ref` é a
    própria URL — quem a entrega é o `extrair_anexo` acima, via `fonte["url"]`,
    e esta função existe para quando o Evolution manda o id no lugar da URL.

    A URL vem do webhook, ou seja, de QUEM CHAMA o canal, e por isso passa pela
    mesma prova de `pipeline._url_publica` que já protege o caminho de `url`:
    resolver o host e recusar rede privada antes do GET. Sem isso, o Evolution
    podia ser instruído a devolver `mediaUrl` apontando para `169.254.169.254` e
    o Render entregaria os metadados da máquina como se fosse uma foto. Cada
    redirect é conferido de novo, e o download é cortado em
    `midia.LIMITE_POR_ARQUIVO` para não transformar o plano grátis em RAM
    pública.
    """
    # Só a prova de URL: o download em si é o daqui, porque a Evolution devolve
    # um arquivo só e a leitura precisa acontecer já. O import é tardio porque
    # `pipeline` importa este módulo.
    from app.pipeline import _url_publica

    if not instancia:
        raise RuntimeError("canal de WhatsApp sem instance_name")
    url = ref if ref.startswith("http") else ""
    if not url:
        raise RuntimeError("Evolution nao devolveu mediaUrl para o arquivo")
    destino = await _url_publica(url)
    async with httpx.AsyncClient(timeout=90, follow_redirects=False) as http:
        for _ in range(4):
            async with http.stream("GET", destino) as resposta:
                if resposta.is_redirect:
                    lugar = resposta.headers.get("location") or ""
                    if not lugar:
                        raise RuntimeError("redirect sem destino")
                    destino = await _url_publica(str(httpx.URL(destino).join(lugar)))
                    continue
                resposta.raise_for_status()
                mime = str(resposta.headers.get("content-type") or "application/octet-stream")
                dados = bytearray()
                async for pedaco in resposta.aiter_bytes():
                    dados.extend(pedaco)
                    if len(dados) > _LIMITE_ARQUIVO:
                        raise RuntimeError("arquivo grande demais para o plano")
                return mime.split(";")[0], bytes(dados)
    raise RuntimeError("redirect demais ao baixar o arquivo")

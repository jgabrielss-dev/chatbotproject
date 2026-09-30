"""Canais OFICIAIS da Meta (WhatsApp Cloud API e Instagram Messaging API).

Por que isto resolve o problema do Render:
    O Meta faz POST no nosso webhook sempre que chega mensagem. O webhook é a
    Edge Function do Supabase, que grava na fila durável e só então acorda o
    Render. Não há polling e não há keepalive, então o serviço do Render pode
    hibernar (~15 min sem tráfego) e passar o mês inteiro dormindo. Só os
    minutos com mensagem real consomem hora do free tier.

    Contraste com os canais NÃO OFICIAIS (Evolution/Baileys e instagrapi), que
    precisam de um processo vivo para fazer polling ou para receber o webhook
    antes de repassar — e por isso exigem keepalive.

Segurança:
    - `X-Hub-Signature-256` é validado contra o `app_secret` do app Meta
      (HMAC-SHA256 do corpo cru), quando o app_secret está configurado.
    - O `access_token` e o `app_secret` nunca voltam ao navegador; ver
      `app/repositories.py` (redige a config na saída da API).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

log = logging.getLogger("meta_oficial")

GRAPH_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"


class MetaError(RuntimeError):
    """Erro retornado pela API Graph da Meta."""

    def __init__(self, mensagem: str, *, status: int = 0, codigo: int = 0, subcodigo: int = 0):
        super().__init__(mensagem)
        self.status = status
        self.codigo = codigo
        self.subcodigo = subcodigo


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

def _campo(cfg: dict[str, Any], *nomes: str) -> str:
    for n in nomes:
        v = cfg.get(n)
        if v:
            return str(v).strip()
    return ""


def token_acesso(cfg: dict[str, Any]) -> str:
    return _campo(cfg, "access_token", "token", "meta_access_token")


def app_secret(cfg: dict[str, Any]) -> str:
    return _campo(cfg, "app_secret", "meta_app_secret")


def verify_token(cfg: dict[str, Any]) -> str:
    return _campo(cfg, "verify_token", "meta_verify_token")


def telefone_id(cfg: dict[str, Any]) -> str:
    return _campo(cfg, "phone_number_id", "phone_id", "telefone_id")


def ig_user_id(cfg: dict[str, Any]) -> str:
    return _campo(cfg, "ig_user_id", "ig_id", "instagram_id")


# ---------------------------------------------------------------------------
# Webhook: verificação e assinatura
# ---------------------------------------------------------------------------

def conferir_verificacao(cfg: dict[str, Any], modo: str, token: str, desafio: str) -> bool:
    """Confere o handshake de verificação de webhook que o Meta faz via GET."""
    esperado = verify_token(cfg)
    if not esperado:
        return False
    return hmac.compare_digest(modo or "", "subscribe") and hmac.compare_digest(
        token or "", esperado
    )


def assinatura_valida(corpo: bytes, cabecalho: str | None, segredo: str) -> bool:
    """Valida `X-Hub-Signature-256: sha256=<hex>` com o app_secret da Meta.

    Fecha por padrão: sem `app_secret` a assinatura NÃO pode ser conferida, e
    aceitar o evento assim permitiria forjar mensagens no canal. Como a
    validação de canal oficial já exige `app_secret`, chegar aqui sem ele é um
    erro de configuração — não um caso "inseguro porém tolerado".
    """
    if not segredo:
        return False
    if not cabecalho or not cabecalho.startswith("sha256="):
        return False
    esperado = hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={esperado}", cabecalho)


# ---------------------------------------------------------------------------
# Envio de mensagens
# ---------------------------------------------------------------------------

def _extrair_erro(resposta: httpx.Response) -> MetaError:
    try:
        corpo = resposta.json()
    except Exception:
        return MetaError(f"HTTP {resposta.status_code} sem JSON", status=resposta.status_code)
    erro = (corpo.get("error") or {}) if isinstance(corpo, dict) else {}
    detalhe = erro.get("error_user_msg") or erro.get("message") or "erro desconhecido"
    sub = erro.get("error_data") or {}
    if isinstance(sub, dict) and sub.get("details"):
        detalhe = f"{detalhe}: {sub['details']}"
    return MetaError(
        str(detalhe),
        status=resposta.status_code,
        codigo=int(erro.get("code") or 0),
        subcodigo=int(erro.get("error_subcode") or 0),
    )


async def _posar(url: str, token: str, json_body: dict[str, Any] | None = None,
                 data: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30) as http:
        resposta = await http.post(url, json=json_body, data=data, headers=headers)
    if resposta.status_code >= 400:
        raise _extrair_erro(resposta)
    try:
        return resposta.json()
    except Exception:
        return {}


async def enviar_whatsapp(cfg: dict[str, Any], destinatario: str, texto: str) -> None:
    """Envia texto pelo WhatsApp Cloud API."""
    telefone = telefone_id(cfg)
    token = token_acesso(cfg)
    if not telefone or not token:
        raise MetaError("Canal WhatsApp oficial sem phone_number_id ou access_token.")
    corpo = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": destinatario,
        "type": "text",
        "text": {"preview_url": False, "body": texto},
    }
    await _posar(f"{GRAPH_BASE}/{telefone}/messages", token, json_body=corpo)
    log.info("WhatsApp Cloud API: mensagem enviada para %s", destinatario)


async def enviar_instagram(cfg: dict[str, Any], destinatario: str, texto: str) -> None:
    """Envia texto pela Instagram Messaging API.

    O corpo é o da Instagram Messaging API (não o JSON:API da Messenger Platform):
    o `destinatario` é o IGSID do cliente, que vem em `sender.id` do webhook.
    `messaging_type=RESPONSE` só é aceito dentro da janela de 24h da mensagem
    recebida — que é o caso do chatbot, que responde na hora.
    """
    conta = ig_user_id(cfg)
    token = token_acesso(cfg)
    if not conta or not token:
        raise MetaError("Canal Instagram oficial sem ig_user_id ou access_token.")
    corpo = {
        "recipient": {"id": str(destinatario)},
        "messaging_type": "RESPONSE",
        "message": {"text": texto},
    }
    await _posar(f"{GRAPH_BASE}/{conta}/messages", token, json_body=corpo)
    log.info("Instagram Messaging API: mensagem enviada para %s", destinatario)


async def enviar(cfg: dict[str, Any], tipo_canal: str, destinatario: str, texto: str) -> None:
    if tipo_canal == "whatsapp_oficial":
        await enviar_whatsapp(cfg, destinatario, texto)
    elif tipo_canal == "instagram_oficial":
        await enviar_instagram(cfg, destinatario, texto)
    else:
        raise MetaError(f"tipo de canal não suportado: {tipo_canal}")


# ---------------------------------------------------------------------------
# Diagnóstico (botão "Testar" no painel)
# ---------------------------------------------------------------------------

async def _get_graph(caminho: str, token: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as http:
        resposta = await http.get(
            f"{GRAPH_BASE}/{caminho}",
            params={**(params or {}), "access_token": token},
        )
    if resposta.status_code >= 400:
        raise _extrair_erro(resposta)
    return resposta.json()


async def verificar_whatsapp(cfg: dict[str, Any]) -> str:
    telefone = telefone_id(cfg)
    dados = await _get_graph(telefone, token_acesso(cfg), {"fields": "display_phone_number,verified_name"})
    return f"{dados.get('verified_name', '?')} ({dados.get('display_phone_number', telefone)})"


async def verificar_instagram(cfg: dict[str, Any]) -> str:
    conta = ig_user_id(cfg)
    dados = await _get_graph(conta, token_acesso(cfg), {"fields": "username,name"})
    return f"@{dados.get('username', '?')} ({dados.get('name', '')})".strip()


async def verificar(cfg: dict[str, Any], tipo_canal: str) -> str:
    if tipo_canal == "whatsapp_oficial":
        return await verificar_whatsapp(cfg)
    return await verificar_instagram(cfg)


# --------------------------------------------------------------------------
# Anexos (item 15)
# --------------------------------------------------------------------------

#: `message.<chave>` do webhook da Meta -> tipo do item 15.
_CAMPO_META = (
    ("image", "foto"),
    ("audio", "audio"),
    ("video", "video"),
    ("document", "arquivo"),
    ("sticker", "foto"),
)


def extrair_anexo(mensagem: dict) -> dict | None:
    """Referência do anexo de uma mensagem da Meta, ou None se é só texto.

    O webhook traz `id` e `mime_type`, e a URL de download SÓ existe depois de
    um GET no id (a Graph responde `{"url": ...}`). Guardar a URL no
    `payload_json` seria inútil: ela expira em 5 minutos, e a fila pode levar
    mais tempo que isso para ser processada. O que fica guardado é o id.
    """
    for chave, tipo in _CAMPO_META:
        corpo = (mensagem or {}).get(chave)
        if isinstance(corpo, dict) and corpo.get("id"):
            return {
                "tipo": tipo,
                "mime": corpo.get("mime_type") or "",
                "nome": (corpo.get("filename") or corpo.get("id")),
                "fonte": {"ref": str(corpo["id"])},
            }
    return None


async def baixar_anexo(cfg: dict[str, Any], tipo_canal: str, midia_id: str) -> tuple[str, bytes]:
    """Baixa um anexo da Meta. Devolve (mime, bytes).

    Duas chamadas, e a segunda é a que importa: a URL que a Graph devolve é de
    uso único e de 5 minutos, e ela SÓ aceita o token como query param — não
    como header. Mandar no header devolve 403 sem mensagem útil, então o
    `headers` fica de fora aqui de propósito.
    """
    token = token_acesso(cfg)
    if not token:
        raise MetaError("canal oficial sem access_token")
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as http:
        passo1 = await http.get(f"{GRAPH_BASE}/{midia_id}", params={"access_token": token})
        if passo1.status_code >= 400:
            raise _extrair_erro(passo1)
        dados = passo1.json()
        url = dados.get("url") or ""
        mime = str(dados.get("mime_type") or "application/octet-stream")
        if not url:
            raise MetaError("Graph nao devolveu url de download para o anexo")
        passo2 = await http.get(url, params={"access_token": token})
    if passo2.status_code >= 400:
        raise _extrair_erro(passo2)
    return mime.split(";")[0], passo2.content

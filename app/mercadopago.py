"""Gateway de pagamento real: Mercado Pago via PIX.

Este módulo é a ÚNICA ponte com a API do Mercado Pago. As regras de negócio
(quando cobrar, quanto, o que abre o pagamento) continuam em `app/cobranca.py`
e `app/rotas_cobranca.py` — aqui só se fala HTTP com o provedor.

Três decisões que valem explicar:

* **A fonte da verdade é a consulta reversa, não o webhook.** O webhook do MP
  é uma notificação que qualquer um pode tentar forjar endereçando o endpoint
  errado. Por isso o receptor SEMPRE reconfere o pagamento na API do MP
  (`GET /v1/payments/{id}`) antes de marcar qualquer coisa como pago. O
  `x-signature`, quando o operador configura o segredo, só corta o ruído
  antes dessa conferência.

* **Cada checkout cria um PIX com `external_reference` = o nosso id de
  `pagamentos`.** É o elo entre o mundo do MP e o nosso banco: o webhook chega
  com `data.id` (o id do MP), e o `external_reference` devolvido confirma que
  aquele pagamento é da nossa tabela.

* **O QR fica em dois formatos** porque o cliente de PIX paga de dois modos:
  `qr_code_base64` (imagem PNG para a câmera do banco) e `qr_code` (o texto
  "copia e cola" para quem paga pelo app). Guardamos o texto no banco para
  reexibição; a imagem é pesada e é regenerada a cada checkout.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import time
import uuid

import httpx

from app.config import settings

log = logging.getLogger("mercadopago")

_API_PAGAMENTOS = "https://api.mercadopago.com/v1/payments"


class MercadoPagoError(RuntimeError):
    """Falha ao falar com a API do Mercado Pago. A mensagem é segura para o
    cliente ver (não vaza token nem corpo da resposta).

    `status` e `url` carregam o que a resposta tinha a dizer: o webhook precisa
    distinguir "este pagamento não existe (ou não é visível para a nossa chave)"
    de "a API está fora do ar". São decisões opostas -- a primeira quer 200
    (não há nada a confirmar e repetir não vai mudar nada), a segunda quer 503
    (o MP volta em minutos, e é o dinheiro que ainda não abriu o período).
    """

    def __init__(self, mensagem: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(mensagem)
        self.status = status
        self.url = url


def _cabecalhos(idempotencia: str = "") -> dict:
    h = {
        "Authorization": f"Bearer {settings.mercadopago_pat}",
        "Content-Type": "application/json",
    }
    if idempotencia:
        h["X-Idempotency-Key"] = idempotencia
    return h


def _tratar(aceno: httpx.Response) -> None:
    """Transforma resposta não-2xx do MP numa exceção com mensagem limpa."""
    if aceno.status_code < 400:
        return
    detalhe = ""
    try:
        corpo = aceno.json()
        detalhe = str(corpo.get("message") or corpo.get("error") or "")
    except ValueError:
        detalhe = ""
    log.warning("Mercado Pago respondeu HTTP %s: %s", aceno.status_code, detalhe[:200])
    raise MercadoPagoError(
        (detalhe or "sem detalhe").strip()[:220] or "erro desconhecido do provedor",
        status=aceno.status_code,
        url=str(aceno.request.url) if aceno.request is not None else "",
    )


async def criar_pagamento_pix(
    valor: float,
    email: str,
    referencia_externa: str,
    descricao: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Cria um PIX no Mercado Pago e devolve o que o navegador precisa.

    Devolve: `id` (id do pagamento no MP), `status`, `qr_code` (copia e cola),
    `qr_code_base64` (imagem PNG) e `expira_em` (ISO). O `transport` é só para
    os testes injetarem um mock.
    """
    agora = dt.datetime.now(dt.timezone.utc)
    corpo: dict = {
        "transaction_amount": round(float(valor), 2),
        "description": (descricao or "Chatbot Project")[:120],
        "payment_method_id": "pix",
        "payer": {"email": (email or "").strip() or "cliente@chatbotproject.app"},
        "external_reference": str(referencia_externa),
        "date_of_expiration": (agora + dt.timedelta(minutes=30)).isoformat(),
    }
    if settings.base_url:
        corpo["notification_url"] = f"{settings.base_url}/api/webhooks/mercadopago"
    try:
        async with httpx.AsyncClient(transport=transport, timeout=25) as http:
            aceno = await http.post(
                _API_PAGAMENTOS,
                headers=_cabecalhos(str(uuid.uuid4())),
                json=corpo,
            )
    except httpx.HTTPError as e:
        log.warning("Mercado Pago inacessível ao criar PIX: %s", e)
        raise MercadoPagoError("provedor de pagamento inacessível no momento") from e

    _tratar(aceno)
    dados = aceno.json()
    transacao = (dados.get("point_of_interaction") or {}).get("transaction_data") or {}
    return {
        "id": dados.get("id"),
        "status": dados.get("status", "pending"),
        "qr_code": transacao.get("qr_code") or "",
        "qr_code_base64": transacao.get("qr_code_base64") or "",
        "expira_em": dados.get("date_of_expiration")
        or (agora + dt.timedelta(minutes=30)).isoformat(),
    }


async def consultar_pagamento(
    payment_id: int, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict:
    """Estado atual do pagamento no Mercado Pago.

    É a fonte da verdade do webhook e do polling do checkout: só um
    `status='approved'` aqui abre o período do cliente.
    """
    try:
        async with httpx.AsyncClient(transport=transport, timeout=25) as http:
            aceno = await http.get(
                f"{_API_PAGAMENTOS}/{int(payment_id)}", headers=_cabecalhos()
            )
    except httpx.HTTPError as e:
        log.warning("Mercado Pago inacessível ao consultar %s: %s", payment_id, e)
        raise MercadoPagoError("provedor de pagamento inacessível no momento") from e

    _tratar(aceno)
    dados = aceno.json()
    return {
        "id": dados.get("id"),
        "status": dados.get("status"),
        "status_detail": dados.get("status_detail"),
        "external_reference": dados.get("external_reference"),
        "transaction_amount": dados.get("transaction_amount"),
    }


def conferir_assinatura_webhook(cabecalhos: dict, corpo_bruto: bytes) -> bool:
    """Valida `x-signature` do webhook do MP quando o segredo está configurado.

    Formato do MP: `x-signature: ts=<unix>,v1=<hmac>` e `x-request-id`. O v1 é
    HMAC-SHA256 do segredo sobre `id:<id>;request-id:<rid>;ts:<ts>;`, com o id
    vindo do corpo (`data.id`). Sem segredo configurado aceita e deixa a
    consulta reversa (que é a segurança de verdade) decidir.
    """
    segredo = settings.mercadopago_webhook_secret
    if not segredo:
        return True
    assinatura = cabecalhos.get("x-signature") or ""
    request_id = cabecalhos.get("x-request-id") or ""
    partes: dict[str, str] = {}
    for item in assinatura.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            partes[k.strip()] = v.strip()
    ts, v1 = partes.get("ts", ""), partes.get("v1", "")
    if not ts or not v1:
        return False
    try:
        # Janela antirrepetição: assinatura com mais de 5 min é lixo velho.
        if abs(int(ts) - time.time()) > 300:
            return False
    except ValueError:
        return False
    try:
        dados = json.loads(corpo_bruto or b"{}")
        data_id = str((dados.get("data") or {}).get("id") or "")
    except ValueError:
        return False
    mensagem = f"id:{data_id};request-id:{request_id};ts:{ts};"
    esperado = hmac.new(segredo.encode(), mensagem.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, v1)
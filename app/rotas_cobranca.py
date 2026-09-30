"""Rotas de plano e assinatura.

Duas famílias, deliberadamente separadas:

  * `/api/planos` é PÚBLICA. A home precisa mostrar a tabela de preços para
    quem ainda não tem conta — é onde a venda acontece. Não devolve nada
    sensível: só o catálogo, e só os planos pagos (teste na vitrine vira trial
    para gente que não ia converter).

  * `/api/plano/*` é da conta logada. Mostra onde o cliente está, em que dia
    termina o período, quanto já gastou, e aceita a troca de plano.

A regra do item 9 aparece em `POST /api/plano/troca`: nada é aplicado na hora.
O pedido fica agendado e só entra no fim do período já pago. Isso vale para
upgrade e para downgrade — o cliente que se arrependeu tem direito a voltar
antes de pagar o próximo mês, e um sistema que só permite subir aliena o
cliente.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app import cobranca
from app import limites
from app import mercadopago as mp
from app import repos_cobranca as rc
from app.auth import exigir_nao_bloqueado, usuario_atual
from app.config import settings

log = logging.getLogger("rotas_cobranca")

router = APIRouter(tags=["cobranca"])


def _isento(usuario) -> bool:
    """O dono/operador (e as contas de teste) não passam pelo checkout."""
    return limites.eh_isento(usuario.email)


def _exigir_pagamento_do_dono(pagamento: dict | None, usuario_id: str) -> dict:
    """O pagamento existe e é DO dono da requisição, ou 404."""
    if not pagamento or str(pagamento.get("usuario_id")) != str(usuario_id):
        raise HTTPException(404, "Pagamento não encontrado.")
    return pagamento


@router.get("/api/planos")
async def planos():
    """Catálogo público. Usado pela home e pela tela de login."""
    return {
        "planos": cobranca.catalogo_para_json(),
        "mensagens_excedentes": limites._tabela_excedentes(),
        "desconto_anual": cobranca.DESCONTO_ANUAL,
    }


@router.get("/api/plano")
async def meu_plano(request: Request):
    """Plano, período, consumo e o que está agendado. A tela de perfil monta
    com isto; os agentes internos (itens 7 e 13) leem o mesmo JSON."""
    usuario = usuario_atual(request)
    return await limites.contexto(usuario.id, usuario.eh_admin)


@router.get("/api/plano/pagamentos")
async def meus_pagamentos(request: Request):
    usuario = usuario_atual(request)
    if limites.sem_cota():
        return []
    return await rc.listar_pagamentos(usuario.id)


@router.post("/api/plano/troca")
async def trocar(request: Request):
    """Upgrade ou downgrade. Agendado para o fim do período pago.

    Aceita o corpo `{"plano": "pro", "ciclo": "anual"}`. Devolve o estado
    inteiro da assinatura para a tela já atualizar sem uma segunda chamada.
    """
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    if usuario.eh_admin or _isento(usuario):
        raise HTTPException(400, "Esta conta não tem assinatura paga.")
    if limites.sem_cota():
        raise HTTPException(503, "Cobrança indisponível: sem banco de dados configurado.")

    body = await request.json()
    alvo = str(body.get("plano") or "").strip()
    ciclo = str(body.get("ciclo") or "mensal").strip()
    if ciclo not in ("mensal", "anual"):
        raise HTTPException(400, "Ciclo inválido: use 'mensal' ou 'anual'.")

    assinatura = await rc.garantir_assinatura(usuario.id)
    atual = assinatura.get("plano_id")
    pode, motivo = cobranca.pode_trocar_de_plano(atual, alvo)
    if not pode:
        raise HTTPException(400, motivo)

    novo = cobranca.plano(alvo)
    await rc.agendar_troca(usuario.id, novo.id, ciclo)
    limites.limpar_cache(usuario.id)

    # O livro de pagamentos registra a intenção com o valor exato do plano
    # pedido. O gateway, quando existir, marca este pagamento como pago; até
    # lá ele fica 'pendente' e nada muda (que é o comportamento correto: um
    # plano pago nunca entra sem pagamento).
    await rc.criar_pagamento(
        usuario.id, novo.id, ciclo, cobranca.preco_do_ciclo(novo, ciclo), metodo="",
    )
    return await limites.contexto(usuario.id)


@router.post("/api/plano/troca/cancelar")
async def cancelar_troca(request: Request):
    """Desfaz uma troca agendada (que ainda não começa a valer)."""
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    if limites.sem_cota():
        raise HTTPException(503, "Cobrança indisponível: sem banco de dados configurado.")
    await rc.cancelar_troca(usuario.id)
    limites.limpar_cache(usuario.id)
    return await limites.contexto(usuario.id)


@router.post("/api/plano/cancelar")
async def cancelar_assinatura(request: Request):
    """Cancela no fim do período pago — nunca na hora.

    O acesso continua até a data que o cliente já pagou: cortar antes seria
    cobrar por um serviço que ele vai deixar de ter, e gera o ticket mais
    irritante que existe ("paguei e me cortaram").
    """
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    if usuario.eh_admin or _isento(usuario):
        raise HTTPException(400, "Esta conta não tem assinatura paga.")
    if limites.sem_cota():
        raise HTTPException(503, "Cobrança indisponível: sem banco de dados configurado.")
    await rc.agendar_cancelamento(usuario.id)
    limites.limpar_cache(usuario.id)
    return await limites.contexto(usuario.id)


@router.post("/api/plano/reativar")
async def reativar(request: Request):
    """Cancela o cancelamento agendado."""
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    if limites.sem_cota():
        raise HTTPException(503, "Cobrança indisponível: sem banco de dados configurado.")
    await rc.reverter_cancelamento(usuario.id)
    limites.limpar_cache(usuario.id)
    return await limites.contexto(usuario.id)


@router.post("/api/plano/pagamento/{pagamento_id}/pagar")
async def pagar(pagamento_id: int, request: Request):
    """Confirma o pagamento e estende a assinatura.

    DOIS CAMINHOS:

      * Conta isenta (dono/operador, contas de teste): o pagamento abre na hora,
        sem gateway — é a garantia de que a operação própria não passa pelo
        próprio checkout.

      * Qualquer outro: ESTA rota era o "paguei agora e o plano abre" de quando
        não havia gateway. Com o Mercado Pago configurado ela é um plano grátis
        com um clique — um buraco —, então RECUSA e manda para o checkout PIX.
        Sem o PAT configurado (dev local/sem pagamento real), mantém o
        comportamento antigo para a operação não travar num ambiente de teste.

    As proteções de dono, idempotência e "troca só no fim do período já pago"
    continuam valendo nos dois caminhos, porque valem para qualquer pagamento.
    """
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    if usuario.eh_admin:
        raise HTTPException(400, "Conta de administrador não tem assinatura.")
    if limites.sem_cota():
        raise HTTPException(503, "Cobrança indisponível: sem banco de dados configurado.")

    pagamento = _exigir_pagamento_do_dono(
        await rc.obter_pagamento(pagamento_id), usuario.id,
    )
    if pagamento.get("status") == "pago":
        return await limites.contexto(usuario.id)

    if _isento(usuario):
        await rc.marcar_pagamento_pago(pagamento_id, referencia="isento")
        await rc.definir_metodo(pagamento_id, "isento")
        await _abrir_periodo_pago(usuario.id, pagamento)
        limites.limpar_cache(usuario.id)
        return await limites.contexto(usuario.id)

    if settings.has_mercadopago:
        raise HTTPException(
            409,
            "O pagamento agora é pelo PIX: use a opção de pagar no livro de "
            "pagamentos para ver o QR code.",
        )

    await rc.marcar_pagamento_pago(pagamento_id, referencia=f"demo-{pagamento_id}")
    await _abrir_periodo_pago(usuario.id, pagamento)
    limites.limpar_cache(usuario.id)
    return await limites.contexto(usuario.id)


async def _abrir_periodo_pago(usuario_id: str, pagamento: dict) -> None:
    """Aplica o pagamento: o período começa quando o anterior termina.

    Se o cliente pagou adiantado (o caso comum: pediu o plano novo e pagou
    agora), o novo período começa no `fim_periodo` antigo — a regra do item 9,
    "preço e regras mudam só no fim do que já foi pago", vale inclusive quando
    o pagamento chega antes. Se o período já venceu, começa agora.
    """
    assinatura = await rc.atualizar_periodo(usuario_id) or {}
    agora = dt.datetime.now(dt.timezone.utc)
    fim = assinatura.get("fim_periodo")
    if isinstance(fim, str):
        fim = dt.datetime.fromisoformat(fim.replace("Z", "+00:00"))
        if fim.tzinfo is None:
            fim = fim.replace(tzinfo=dt.timezone.utc)
    expirada = assinatura.get("status") in ("cancelado", "expirado")
    inicio = agora if (fim is None or expirada or fim <= agora) else fim
    p = cobranca.plano(pagamento.get("plano_id"))
    ciclo = pagamento.get("ciclo") or "mensal"
    novo_fim = cobranca.fim_do_periodo(inicio, ciclo)

    await rc.aplicar_plano_pago(usuario_id, p.id, ciclo, inicio, novo_fim, pagamento["id"])


@router.post("/api/plano/pagamento/{pagamento_id}/checkout")
async def checkout(pagamento_id: int, request: Request):
    """Gera o PIX no Mercado Pago e devolve o QR para o cliente pagar.

    Idempotente: o mesmo pagamento pendente devolve o mesmo QR (guardamos o
    texto no banco na primeira vez). A imagem base64 não fica guardada — é
    pesada e regenerável —, então quem reabre o checkout recebe o texto e
    uma imagem em branco (o navegador mostra o "copia e cola", que é o
    suficiente para pagar).

    Conta isenta nunca chega aqui: o checkout devolve `status='pago'` e abre o
    período sem gateway.
    """
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    if usuario.eh_admin:
        raise HTTPException(400, "Conta de administrador não tem assinatura.")
    if limites.sem_cota():
        raise HTTPException(503, "Cobrança indisponível: sem banco de dados configurado.")

    pagamento = _exigir_pagamento_do_dono(
        await rc.obter_pagamento(pagamento_id), usuario.id,
    )
    if pagamento.get("status") != "pendente":
        # Já pago/cancelado: manda o estado inteiro, a tela se acerta sozinha.
        limites.limpar_cache(usuario.id)
        return await limites.contexto(usuario.id)

    if _isento(usuario):
        await rc.marcar_pagamento_pago(pagamento_id, referencia="isento")
        await rc.definir_metodo(pagamento_id, "isento")
        await _abrir_periodo_pago(usuario.id, pagamento)
        limites.limpar_cache(usuario.id)
        return {"status": "pago", "isento": True,
                "pagamento_id": pagamento_id}

    if not settings.has_mercadopago:
        raise HTTPException(
            503,
            "O pagamento online está indisponível no momento. Tente novamente "
            "em instantes (ou fale com o atendimento).",
        )

    if pagamento.get("referencia") and pagamento.get("qr_code"):
        # PIX já criado: devolve o que temos em vez de gerar outro.
        return {
            "status": "pendente",
            "pagamento_id": pagamento_id,
            "qr_code": pagamento.get("qr_code") or "",
            "qr_code_base64": "",
            "expira_em": pagamento.get("expira_em"),
            "valor": pagamento.get("valor"),
            "plano": cobranca.plano(pagamento.get("plano_id")).detalhe(),
        }

    plano = cobranca.plano(pagamento.get("plano_id"))
    ciclo = pagamento.get("ciclo") or "mensal"
    valor = float(pagamento.get("valor") or cobranca.preco_do_ciclo(plano, ciclo))
    try:
        pix = await mp.criar_pagamento_pix(
            valor,
            usuario.email,
            str(pagamento_id),
            f"Chatbot Project — plano {plano.nome} ({ciclo})",
        )
    except mp.MercadoPagoError as e:
        raise HTTPException(502, f"Falha ao gerar o PIX: {e}") from e

    await rc.guardar_checkout_pix(
        pagamento_id, f"mp-{pix.get('id')}", pix.get("qr_code") or "",
        pix.get("expira_em"),
    )
    return {
        "status": pix.get("status", "pendente"),
        "pagamento_id": pagamento_id,
        "qr_code": pix.get("qr_code") or "",
        "qr_code_base64": pix.get("qr_code_base64") or "",
        "expira_em": pix.get("expira_em"),
        "valor": valor,
        "plano": plano.detalhe(),
    }


@router.get("/api/plano/pagamento/{pagamento_id}")
async def situacao_pagamento(pagamento_id: int, request: Request):
    """Estado vivo do pagamento (polling do checkout PIX).

    O navegador pergunta a cada poucos segundos se o PIX caiu. Quando o
    Mercado Pago responde `approved`, confirma aqui na hora — o webhook é o
    caminho oficial, mas depender só dele deixaria o cliente olhando para o QR
    depois de pagar. Sem `referencia` pix (pendente sem checkout), devolve o
    estado do banco sem consultar ninguém.
    """
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    pagamento = _exigir_pagamento_do_dono(
        await rc.obter_pagamento(pagamento_id), usuario.id,
    )
    if pagamento.get("status") == "pendente" and settings.has_mercadopago:
        referencia = str(pagamento.get("referencia") or "")
        if referencia.startswith("mp-"):
            try:
                estado = await mp.consultar_pagamento(int(referencia[3:]))
            except mp.MercadoPagoError:
                estado = {}
            if estado.get("status") == "approved":
                await rc.marcar_pagamento_pago(pagamento_id, referencia=referencia)
                await _abrir_periodo_pago(usuario.id, pagamento)
                limites.limpar_cache(usuario.id)
                pagamento = await rc.obter_pagamento(pagamento_id) or pagamento
    return pagamento


@router.post("/api/webhooks/mercadopago")
async def webhook_mercadopago(request: Request):
    """Notificação do Mercado Pago de que um pagamento mudou de estado.

    Nunca confia no corpo sozinho: o segredo (quando configurado) corta
    assinatura inválida, e a consulta reversa na API do MP é quem decide — um
    `approved` de verdade é que abre o período. Tudo aqui responde 200 mesmo
    quando ignora, para o MP não repetir a notificação à toa.
    """
    corpo = await request.body()
    if not mp.conferir_assinatura_webhook(dict(request.headers), corpo):
        raise HTTPException(401, "Assinatura de webhook inválida.")
    try:
        dados = json.loads(corpo or b"{}")
    except ValueError:
        dados = {}
    acao = dados.get("action") or ""
    if acao not in ("payment.created", "payment.updated", "payment") and dados.get("type") not in (
        "payment", "payment_intent",
    ):
        return {"recebido": True, "ignorado": True}
    mp_id = (dados.get("data") or {}).get("id")
    if not mp_id:
        return {"recebido": True, "ignorado": "sem id"}
    try:
        estado = await mp.consultar_pagamento(int(mp_id))
    except mp.MercadoPagoError as e:
        log.warning("Webhook MP: falha ao consultar %s: %s", mp_id, e)
        return {"recebido": True, "ignorado": "consulta falhou"}
    if estado.get("status") != "approved":
        return {"recebido": True, "status": estado.get("status")}

    referencia = f"mp-{mp_id}"
    pagamento = await rc.obter_pagamento_por_referencia(referencia)
    if not pagamento:
        # Fallback pelo elo que mandamos na criação (nosso id de pagamentos).
        externa = str(estado.get("external_reference") or "")
        if externa.isdigit():
            pagamento = await rc.obter_pagamento(int(externa))
    if not pagamento:
        # Pagamento de outra base/gateway de teste: não é nosso, sem erro.
        return {"recebido": True, "ignorado": "desconhecido"}
    if pagamento.get("status") != "pendente":
        return {"recebido": True, "status": pagamento.get("status")}

    await rc.marcar_pagamento_pago(pagamento["id"], referencia=referencia)
    await _abrir_periodo_pago(str(pagamento["usuario_id"]), pagamento)
    limites.limpar_cache(str(pagamento["usuario_id"]))
    return {"recebido": True, "pago": True}


@router.get("/api/plano/limites")
async def limites_atuais(request: Request):
    """Só os limites, sem consumo nem histórico.

    Existe para o botão "criar canal" do painel saber se habilita a si mesmo
    antes de o clique falhar com 402 — erro de cota mostrado depois do clique é
    pior do que botão cinza.
    """
    usuario = usuario_atual(request)
    # Isento entra aqui junto com o admin: os dois têm o mesmo desenho no
    # `/api/plano` (limites zerados = ilimitado) e é o que o painel lê para
    # habilitar o botão "criar canal". Deixar a isenta de fora devolvia
    # `sem_cota: false` com um teto de 50 agentes — o botão apareceria
    # desabilitado para uma conta que nunca, em hipótese nenhuma, paga.
    if usuario.eh_admin or await limites.eh_isento_por_id(usuario.id) or limites.sem_cota():
        return {"sem_cota": True, "pode_criar_agente": True, "pode_criar_canal": True}
    p = await limites.plano_atual(usuario.id)
    usados = await limites.contar_agentes(usuario.id)
    return {
        "sem_cota": False,
        "pode_criar_agente": usados < p.max_agentes,
        "agentes_usados": usados,
        "max_agentes": p.max_agentes,
        "max_canais_por_agente": p.max_canais_por_agente,
        "max_mensagens_por_agente_mes": p.max_mensagens_por_agente_mes,
    }

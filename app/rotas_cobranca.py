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
from app.auth import exigir_nao_bloqueado, id_para_coluna_uuid, usuario_atual
from app.config import settings

log = logging.getLogger("rotas_cobranca")

router = APIRouter(tags=["cobranca"])

#: Teto do corpo do webhook do MP. A notificação real tem quelques centenas de
#: bytes; sem limite, um POST público de gigabytes é trabalho de graça para o
#: processo (e um 413 é resposta mais honesta que um 500).
LIMITE_CORPO_WEBHOOK_MP = 64 * 1024


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
    # A conta de emergência (ADMIN_TOKEN) não tem id de uuid: passá-lo como
    # `usuario_id` fazia o Postgres recusar e a rota devolvia 500 — justamente na
    # tela que o operador abre quando algo está quebrado.
    return await rc.listar_pagamentos(id_para_coluna_uuid(usuario))


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
    ciclo = str(body.get("ciclo") or "mensal").strip().lower()
    if ciclo not in ("mensal", "anual"):
        raise HTTPException(400, "Ciclo inválido: use 'mensal' ou 'anual'.")

    assinatura = await rc.garantir_assinatura(usuario.id)
    atual = assinatura.get("plano_id")
    pode, motivo = cobranca.pode_trocar_de_plano(
        atual, alvo, assinatura.get("plano_proximo"),
    )
    if not pode:
        raise HTTPException(400, motivo)

    novo = cobranca.plano(alvo)
    await rc.agendar_troca(usuario.id, novo.id, ciclo)
    limites.limpar_cache(usuario.id)

    # O livro de pagamentos registra a intenção com o valor exato do plano
    # pedido. O gateway, quando existir, marca este pagamento como pago; até
    # lá ele fica 'pendente' e nada muda (que é o comportamento correto: um
    # plano pago nunca entra sem pagamento).
    #
    # Idempotente: se já existe pagamento pendente deste usuário para o mesmo
    # plano e ciclo, ele é reaproveitado. Sem isso, cada clique criava uma linha
    # nova — e o cliente via cinco cobranças iguais no livro de pagamentos só
    # por clicar duas vezes com a página lenta.
    pendente = await rc.pagamento_pendente_equivalente(usuario.id, novo.id, ciclo)
    if not pendente:
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
    # Sem assinatura não há o que cancelar, e o `UPDATE` abaixo não pegaria
    # nenhuma linha. Dizer 400 é honesto; devolver 200 depois de um
    # cancelamento que não aconteceu faria a tela mentir para a pessoa.
    if await rc.obter_assinatura(usuario.id) is None:
        raise HTTPException(400, "Esta conta não tem assinatura paga.")
    await rc.agendar_cancelamento(usuario.id)
    limites.limpar_cache(usuario.id)
    return await limites.contexto(usuario.id)


@router.post("/api/plano/reativar")
async def reativar(request: Request):
    """Cancela o cancelamento agendado."""
    usuario = usuario_atual(request)
    exigir_nao_bloqueado(usuario)
    if usuario.eh_admin or _isento(usuario):
        raise HTTPException(400, "Esta conta não tem assinatura paga.")
    if limites.sem_cota():
        raise HTTPException(503, "Cobrança indisponível: sem banco de dados configurado.")
    assinatura = await rc.obter_assinatura(usuario.id)
    if assinatura is None or not assinatura.get("cancela_em"):
        # Desfazer o que não existe é a mesma mentira do outro lado: a tela
        # mostraria "reativado" sem nunca ter estado cancelada.
        raise HTTPException(400, "Não há cancelamento agendado nesta conta.")
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
        _, transicao = await rc.marcar_pagamento_pago(pagamento_id, referencia="isento")
        await rc.definir_metodo(pagamento_id, "isento")
        if transicao:
            await _abrir_periodo_pago(usuario.id, pagamento)
        limites.limpar_cache(usuario.id)
        return await limites.contexto(usuario.id)

    if settings.has_mercadopago:
        raise HTTPException(
            409,
            "O pagamento agora é pelo PIX: use a opção de pagar no livro de "
            "pagamentos para ver o QR code.",
        )

    if not settings.pagamento_demo:
        # Sem gateway e sem `PAGAMENTO_DEMO=1` escrito de propósito, esta rota
        # não confirma nada. A diferença é entre "plano pago" e "plano grátis
        # com um clique": um PAT do Mercado Pago esquecido no deploy não pode
        # virar botão de presente para qualquer conta logada.
        raise HTTPException(
            503,
            "O pagamento online está indisponível no momento. Tente novamente "
            "em instantes (ou fale com o atendimento).",
        )

    _, transicao = await rc.marcar_pagamento_pago(
        pagamento_id, referencia=f"demo-{pagamento_id}",
    )
    if transicao:
        await _abrir_periodo_pago(usuario.id, pagamento)
    limites.limpar_cache(usuario.id)
    return await limites.contexto(usuario.id)


async def _abrir_periodo_pago(usuario_id: str, pagamento: dict) -> None:
    """Aplica o pagamento: o período começa quando o anterior termina.

    Se o cliente pagou adiantado (o caso comum: pediu o plano novo e pagou
    agora), o novo período começa no `fim_periodo` antigo — a regra do item 9,
    "preço e regras mudam só no fim do que já foi pago", vale inclusive quando
    o pagamento chega antes. Se o período já venceu, começa agora.

    Quem decide se isto roda é o chamador: só quem recebeu `transicao=True` de
    `marcar_pagamento_pago`. Um pagamento já aplicado sai cedo, e
    `aplicar_plano_pago` tem a mesma trava no banco (`aplicado_em IS NULL`),
    então webhook + polling no mesmo instante estendem o período uma vez só.
    """
    if pagamento.get("aplicado_em"):
        return
    assinatura = await rc.garantir_assinatura(usuario_id) or {}
    assinatura = await rc.atualizar_periodo(usuario_id) or assinatura
    agora = dt.datetime.now(dt.timezone.utc)
    fim = assinatura.get("fim_periodo")
    if isinstance(fim, str):
        fim = dt.datetime.fromisoformat(fim.replace("Z", "+00:00"))
        if fim.tzinfo is None:
            fim = fim.replace(tzinfo=dt.timezone.utc)
    expirada = assinatura.get("status") in ("cancelado", "expirado")
    p = cobranca.plano(pagamento.get("plano_id"))
    ciclo = cobranca.normalizar_ciclo(pagamento.get("ciclo"))

    # Pagamento ADIANTADO: o período ainda não terminou. Aqui é onde a regra do
    # item 9 ("preço e regras mudam só no fim do período já pago") estava
    # furada: o período novo começava no `fim_periodo` (futuro) e o `plano_id`
    # era trocado na mesma hora. O cliente ficava com `inicio_periodo` no
    # futuro servindo já as regras do plano novo, e um DOWNGRADE antecipado
    # cortava o limite que ele tinha pago antes de vencer.
    #
    # A correção: o que muda agora é só o FIM do período, estendido até o fim
    # do que foi pago. O `plano_id` continua o que está valendo, e o plano novo
    # vai para `plano_proximo` — que é exatamente para isso que a coluna
    # existe. Ele entra em vigor em `atualizar_periodo`, quando o período
    # antigo realmente termina.
    #
    # Se o período já venceu (ou nunca existiu), o plano novo começa agora.
    ja_vencido = fim is None or expirada or fim <= agora
    if not ja_vencido:
        await rc.aplicar_plano_pago(
            usuario_id, p.id, ciclo, assinatura.get("inicio_periodo"),
            cobranca.fim_do_periodo(fim, ciclo), pagamento["id"],
            adiar=True,
        )
        return

    await rc.aplicar_plano_pago(
        usuario_id, p.id, ciclo, agora, cobranca.fim_do_periodo(agora, ciclo),
        pagamento["id"],
    )


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
        _, transicao = await rc.marcar_pagamento_pago(pagamento_id, referencia="isento")
        await rc.definir_metodo(pagamento_id, "isento")
        if transicao:
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
    if not settings.base_url:
        # Sem `BASE_URL` o PIX é criado sem `notification_url`: o cliente paga e
        # só o polling do navegador confirmaria, o que se perde no primeiro
        # fechar da aba. Melhor recusar agora, com a integration viva.
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
    ciclo = cobranca.normalizar_ciclo(pagamento.get("ciclo"))
    # O valor vem do CATÁLOGO, nunca de `pagamentos.valor`: o catálogo é a
    # única fonte de preço (sincronizado com o `planos` no boot) e a coluna foi
    # gravada no momento do pedido — com o preço antigo, ou editada por quem
    # achasse a tabela. Divergência de centavos aqui é o cliente pagando menos
    # do que o plano vale.
    valor = round(cobranca.preco_do_ciclo(plano, ciclo), 2)
    if valor <= 0:
        raise HTTPException(
            503,
            "O pagamento online está indisponível no momento. Tente novamente "
            "em instantes (ou fale com o atendimento).",
        )
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
        valor=valor,
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
        if referencia.startswith("mp-") and referencia[3:].isdigit():
            try:
                estado = await mp.consultar_pagamento(int(referencia[3:]))
            except mp.MercadoPagoError:
                estado = {}
            if estado.get("status") == "approved" and _valor_bate(estado, pagamento):
                _, transicao = await rc.marcar_pagamento_pago(
                    pagamento_id, referencia=referencia,
                )
                if transicao:
                    await _abrir_periodo_pago(usuario.id, pagamento)
                limites.limpar_cache(usuario.id)
                pagamento = await rc.obter_pagamento(pagamento_id) or pagamento
    return pagamento


def _valor_bate(estado: dict, pagamento: dict) -> bool:
    """O valor que o MP diz ter recebido é o que este pagamento vale?

    A consulta reversa prova que o pagamento existe e foi aprovado, mas o id do
    MP chega no corpo do webhook, que é público. Um PIX de R$ 0,01 criado no
    painel do Mercado Pago passaria por ela e abriria um Specialist. Conferir o
    `transaction_amount` contra o que este registro vale fecha o circuito.

    Se o provedor não devolveu o campo, a decisão é de quem chama: no polling o
    pagamento JÁ tem a referencia `mp-{id}` que gravamos no checkout, então o
    elo é forte; no fallback do webhook não é, e lá o valor é obrigatório.
    """
    recebido = estado.get("transaction_amount")
    if recebido is None:
        return True
    try:
        pago = float(recebido)
    except (TypeError, ValueError):
        return False
    esperado = float(pagamento.get("valor") or 0.0)
    # Compara em centavos inteiros: `abs(80.01 - 80.0)` vale
    # 0.010000000000001563 em ponto flutuante, que reprovaria a diferença de um
    # centavo que o PIX manda (arredondamento do provedor) — e reprovar o
    # pagamento real é pior que aceitar o centavo.
    return abs(round(pago * 100) - round(esperado * 100)) <= 1


@router.post("/api/webhooks/mercadopago")
async def webhook_mercadopago(request: Request):
    """Notificação do Mercado Pago de que um pagamento mudou de estado.

    Nunca confia no corpo sozinho: o segredo (quando configurado) corta
    assinatura inválida, e a consulta reversa na API do MP é quem decide — um
    `approved` de verdade é que abre o período.

    O status da resposta é o que faz o MP repetir a notificação, então ele é
    deliberado: 200 para "não é nosso", "não está aprovado" ou "já foi tratado",
    e 5xx SÓ quando a consulta reversa falhou por algo passageiro (a API do MP
    fora do ar). Responder 200 numa falha de rede é dinheiro recebido que nunca
    abre o período: o MP dá a notificação como entregue e não volta.
    """
    corpo = await request.body()
    if len(corpo) > LIMITE_CORPO_WEBHOOK_MP:
        raise HTTPException(413, "Corpo grande demais para um webhook.")
    if not mp.conferir_assinatura_webhook(dict(request.headers), corpo):
        raise HTTPException(401, "Assinatura de webhook inválida.")
    try:
        dados = json.loads(corpo or b"{}")
    except ValueError:
        return {"recebido": True, "ignorado": "json invalido"}
    if not isinstance(dados, dict):
        return {"recebido": True, "ignorado": "json invalido"}
    acao = dados.get("action") or ""
    if acao not in ("payment.created", "payment.updated", "payment") and dados.get("type") not in (
        "payment", "payment_intent",
    ):
        return {"recebido": True, "ignorado": True}
    mp_id = str((dados.get("data") or {}).get("id") or "")
    if not mp_id.isdigit():
        # `data.id` vem do corpo, que é público: "abc" (ou "²", que passa em
        # isdigit() e quebra o int()) não é pagamento nenhum. Um 500 aqui faria
        # o MP reenviar a mesma notificação ruim para sempre.
        return {"recebido": True, "ignorado": "id invalido"}
    try:
        estado = await mp.consultar_pagamento(int(mp_id))
    except mp.MercadoPagoError as e:
        if e.status in (400, 404):
            # O MP notificou um pagamento que a nossa chave não enxerga (ou que
            # não existe mais): não há nada a confirmar e reenviar a notificação
            # não muda nada, então isso é 200 e não 503. Dinheiro nenhum se
            # perde: se o PIX cair mesmo, é o polling do checkout que confirma.
            log.warning("Webhook MP %s: pagamento não existe no provedor (%s)", mp_id, e)
            return {"recebido": True, "ignorado": "pagamento inexistente"}
        log.warning("Webhook MP: falha ao consultar %s: %s", mp_id, e)
        # 503 = "tenta de novo": o MP repete por horas. Fail-closed é o certo
        # aqui; o que não pode é 200, que é "entregue, não insisto". Token
        # revogado (401/403) e limite de taxa (429) também caem neste ramo de
        # propósito: repetir é o que resolve, e confiar no corpo seria aceitar
        # pagamento que ninguém confirmou.
        raise HTTPException(503, "Consulta ao Mercado Pago indisponível.") from e
    if estado.get("status") != "approved":
        return {"recebido": True, "status": estado.get("status")}

    referencia = f"mp-{mp_id}"
    pagamento = await rc.obter_pagamento_por_referencia(referencia)
    elo_forte = pagamento is not None
    if not pagamento:
        # Fallback pelo elo que mandamos na criação (nosso id de pagamentos).
        externa = str(estado.get("external_reference") or "")
        if externa.isdigit():
            pagamento = await rc.obter_pagamento(int(externa))
    if not pagamento:
        # Pagamento de outra base/gateway de teste: não é nosso, sem erro.
        return {"recebido": True, "ignorado": "desconhecido"}
    if not _valor_bate(estado, pagamento) and not (elo_forte and estado.get("transaction_amount") is None):
        log.warning(
            "Webhook MP %s: valor divergente (recebido %s, esperado %s) — não aplicado",
            mp_id, estado.get("transaction_amount"), pagamento.get("valor"),
        )
        return {"recebido": True, "ignorado": "valor divergente"}
    if pagamento.get("status") != "pendente":
        return {"recebido": True, "status": pagamento.get("status")}

    _, transicao = await rc.marcar_pagamento_pago(
        pagamento["id"], referencia=referencia,
    )
    if transicao:
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

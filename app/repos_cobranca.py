"""Bloco de Planos/Assinatura/Consumo/Payments.

Separado em `app/repos_cobranca.py` (e não no fim de `repositories.py`) porque
é o único pedaço do acesso a dados que é *derivado do tempo*: nada aqui roda por
cron. A virada de período é avaliada na leitura (`atualizar_periodo`), o que
significa que o sistema inteiro funciona sem agendador — e que uma assinatura
expirada para de valer no instante em que alguém abre o painel, sem esperar
nenhum job.

O resto do desenho é igual ao de `repositories.py`: o app conecta com a
`service_role` e por isso o filtro por `dono_id` NÃO pode vir do RLS. Toda
função que devolve dado de conta recebe o `usuario_id` e restringe a query a
ele.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from app import cobranca
from app.database import get_pool

log = logging.getLogger("cobranca")


def mes_atual(quando: dt.datetime | None = None) -> str:
    """'YYYY-MM' em UTC. Chave de `consumo_mensagens`."""
    return (quando or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m")


# --------------------------------------------------------------------------
# Planos (espelho do catálogo em Python)
# --------------------------------------------------------------------------


async def sincronizar_planos(planos: tuple[cobranca.Plano, ...]) -> None:
    """Escreve o catálogo do código na tabela `planos`.

    Roda no boot. É um UPSERT, então mudar o preço em Python e reiniciar já
    atualiza o banco — não há um segundo lugar para editar preço, que era o
    jeito mais garantido de o cliente ver um valor e o sistema cobrar outro.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        for p in planos:
            await con.execute(
                """INSERT INTO planos (id, nome, descricao, preco_mensal, preco_anual,
                                      max_agentes, max_canais_por_agente,
                                      max_mensagens_por_agente_mes, dias_gratis,
                                      ordem, ativo)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, TRUE)
                   ON CONFLICT (id) DO UPDATE SET
                     nome = EXCLUDED.nome, descricao = EXCLUDED.descricao,
                     preco_mensal = EXCLUDED.preco_mensal, preco_anual = EXCLUDED.preco_anual,
                     max_agentes = EXCLUDED.max_agentes,
                     max_canais_por_agente = EXCLUDED.max_canais_por_agente,
                     max_mensagens_por_agente_mes = EXCLUDED.max_mensagens_por_agente_mes,
                     dias_gratis = EXCLUDED.dias_gratis, ordem = EXCLUDED.ordem,
                     ativo = TRUE""",
                p.id, p.nome, p.descricao, p.preco_mensal, p.preco_anual,
                p.max_agentes, p.max_canais_por_agente,
                p.max_mensagens_por_agente_mes, p.dias_gratis, p.ordem,
            )


async def listar_planos(incluir_inativos: bool = False) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            f"""SELECT * FROM planos {'WHERE ativo' if not incluir_inativos else ''}
                ORDER BY ordem""",
        )
    return [dict(r) for r in rows]


async def contar_planos() -> int:
    """O seed rodou? Sem isto, uma conta nova cai no plano padrão e nunca
    aparece o erro 'violates foreign key' — que é o sintoma real."""
    pool = await get_pool()
    async with pool.acquire() as con:
        return int(await con.fetchval("SELECT count(*) FROM planos"))


# --------------------------------------------------------------------------
# Assinatura
# --------------------------------------------------------------------------


def _serializar(row: Any) -> dict:
    d = dict(row)
    for chave in ("inicio_periodo", "fim_periodo", "plano_proximo", "ciclo_proximo",
                  "cancela_em", "criado_em", "atualizado_em", "pago_em",
                  "aplicado_em", "expira_em"):
        if isinstance(d.get(chave), dt.datetime):
            d[chave] = d[chave].isoformat()
    return d


async def obter_assinatura(usuario_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT * FROM assinaturas WHERE usuario_id = $1", usuario_id)
    return _serializar(row) if row else None


async def criar_assinatura(
    usuario_id: str,
    plano_id: str,
    ciclo: str = "mensal",
    status: str | None = None,
    inicio: dt.datetime | None = None,
    fim: dt.datetime | None = None,
) -> dict:
    """Cria (ou devolve) a assinatura. Idempotente por `usuario_id`.

    `status` e `fim` saem do plano quando não vêm: o teste de 7 dias é o
    padrão, e quem chama quase sempre quer só "cria o teste".
    """
    p = cobranca.plano(plano_id)
    inicio = inicio or dt.datetime.now(dt.timezone.utc)
    if status is None:
        status = "teste" if p.dias_gratis else "ativo"
    if fim is None:
        dias = p.dias_gratis or (365 if ciclo == "anual" else 30)
        fim = inicio + dt.timedelta(days=dias)
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """INSERT INTO assinaturas
                 (usuario_id, plano_id, status, ciclo, inicio_periodo, fim_periodo)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (usuario_id) DO UPDATE SET
                 atualizado_em = now()
               RETURNING *""",
            usuario_id, p.id, status, ciclo, inicio, fim,
        )
    return _serializar(row)


async def garantir_assinatura(usuario_id: str, admin: bool = False) -> dict:
    """Assinatura da conta, criando o teste de 7 dias se ainda não houver.

    O `admin` é para não trancar o operador fora do próprio painel: admin tem
    limites liberados na aplicação (é quem cadastra canal de WhatsApp
    não-oficial para testar), então a assinatura dele também não pode barrar.
    """
    atual = await obter_assinatura(usuario_id)
    if atual:
        return await atualizar_periodo(usuario_id) or atual
    if admin:
        return {"usuario_id": usuario_id, "plano_id": "admin", "status": "ativo",
                "ciclo": "mensal", "inicio_periodo": None, "fim_periodo": None,
                "plano_proximo": None, "ciclo_proximo": None, "cancela_em": None}
    return await criar_assinatura(usuario_id, cobranca.PLANO_TESTE)


async def agendar_troca(usuario_id: str, plano_proximo: str, ciclo: str = "mensal") -> dict:
    """Guarda o plano pedido. NÃO muda o plano agora.

    A regra do item 9 mora aqui: preço e regras só mudam no fim do período já
    pago. O que o cliente vê hoje continua valendo até lá.
    """
    if ciclo not in ("mensal", "anual"):
        raise ValueError("ciclo invalido")
    p = cobranca.plano(plano_proximo)
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """UPDATE assinaturas
               SET plano_proximo = $2, ciclo_proximo = $3, atualizado_em = now()
               WHERE usuario_id = $1
               RETURNING *""",
            usuario_id, p.id, ciclo,
        )
    return _serializar(row)


async def cancelar_troca(usuario_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """UPDATE assinaturas
               SET plano_proximo = NULL, ciclo_proximo = NULL, atualizado_em = now()
               WHERE usuario_id = $1 RETURNING *""",
            usuario_id,
        )
    return _serializar(row)


async def agendar_cancelamento(usuario_id: str) -> dict:
    """Cancela no fim do período, como manda o item 9.

    Guardar a data (em vez de apagar a assinatura) deixa o cliente continuar
    usando até o que ele já pagou — cortar acesso antes da data seria cobrar
    por um serviço que o cliente vai deixar de ter.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """UPDATE assinaturas SET cancela_em = fim_periodo, atualizado_em = now()
               WHERE usuario_id = $1 RETURNING *""",
            usuario_id,
        )
    return _serializar(row)


async def reverter_cancelamento(usuario_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """UPDATE assinaturas SET cancela_em = NULL, atualizado_em = now()
               WHERE usuario_id = $1 RETURNING *""",
            usuario_id,
        )
    return _serializar(row)


async def definir_periodo(usuario_id: str, fim: dt.datetime) -> None:
    """Ajusta o fim do período (usado ao aplicar um pagamento)."""
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE assinaturas SET fim_periodo = $2, atualizado_em = now() WHERE usuario_id = $1",
            usuario_id, fim,
        )


async def aplicar_plano_pago(
    usuario_id: str,
    plano_id: str,
    ciclo: str,
    inicio: dt.datetime,
    fim: dt.datetime,
    pagamento_id: int,
) -> dict:
    """Abre o período novo e promove o plano pago, numa transação só.

    A troca é feita em uma trans porque meio aberto seria o pior estado
    possível: `plano_id` novo com `fim_periodo` velho (cliente pagando plano
    grande, com a cota do pequeno) ou o inverso (cota grande, período
    expirado). `aplicado_em` no pagamento vai na mesma trans para que o
    gateway nunca pague duas vezes pelo mesmo registro.

    A trava é `AND aplicado_em IS NULL`, e é a PRIMEIRA escrita da transação: se
    o pagamento já foi aplicado, nada é tocado (devolve None) e a assinatura
    fica como está. Sem isto, duas confirmações simultâneas do mesmo pagamento
    (webhook do MP + polling do navegador) estendiam o período duas vezes — dois
    meses por um PIX.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            marcado = await con.fetchrow(
                """UPDATE pagamentos SET aplicado_em = now()
                   WHERE id = $1 AND aplicado_em IS NULL RETURNING id""",
                pagamento_id,
            )
            if not marcado:
                return None
            row = await con.fetchrow(
                """UPDATE assinaturas SET
                     plano_id = $2, ciclo = $3, status = 'ativo',
                     inicio_periodo = $4, fim_periodo = $5,
                     plano_proximo = NULL, ciclo_proximo = NULL, cancela_em = NULL,
                     atualizado_em = now()
                   WHERE usuario_id = $1 RETURNING *""",
                usuario_id, plano_id, ciclo, inicio, fim,
            )
    return _serializar(row) if row else None


async def atualizar_periodo(usuario_id: str, agora: dt.datetime | None = None) -> dict | None:
    """Avalia a virada do período. Roda na leitura, sem cron.

    Três casos:
      * teste de 7 dias venceu  -> `expirado`, e a troca agendada é descartada
        (virar plano pago sem pagamento seria serviço grátis);
      * cancelamento agendado   -> `cancelado` na data que o cliente pediu;
      * período pago venceu     -> `expirado` até haver pagamento confirmado.
        Um plano pago NUNCA se renova sozinho: o gateway ainda não existe, e
        renovar sem cobrar é dar de graça o que o cliente não pagou.

    O `fim_periodo` é empurrado para `agora` nos casos de expiração para que a
    próxima leitura não reavalie o mesmo período (e não fica pedindo "venceu"
    para sempre).
    """
    agora = agora or dt.datetime.now(dt.timezone.utc)
    a = await obter_assinatura(usuario_id)
    if not a:
        return None
    fim = a.get("fim_periodo")
    if fim is None:
        return a
    if isinstance(fim, str):
        fim = dt.datetime.fromisoformat(fim.replace("Z", "+00:00"))
    fim = fim if fim.tzinfo else fim.replace(tzinfo=dt.timezone.utc)
    if fim > agora:
        return a

    atual = cobranca.plano(a.get("plano_id"))
    cancela = a.get("cancela_em")
    if cancela is not None:
        if isinstance(cancela, str):
            cancela = dt.datetime.fromisoformat(cancela.replace("Z", "+00:00"))
        if cancela <= agora:
            return await _set_status(usuario_id, "cancelado", agora)

    if atual.dias_gratis:
        # O teste não vira plano pago sozinho: a troca agendada morre aqui.
        pool = await get_pool()
        async with pool.acquire() as con:
            row = await con.fetchrow(
                """UPDATE assinaturas
                   SET status = 'expirado', plano_proximo = NULL, ciclo_proximo = NULL,
                       inicio_periodo = $2, fim_periodo = $2, atualizado_em = now()
                   WHERE usuario_id = $1 RETURNING *""",
                usuario_id, agora,
            )
        return _serializar(row)

    return await _set_status(usuario_id, "expirado", agora)


async def _set_status(usuario_id: str, status: str, agora: dt.datetime) -> dict:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """UPDATE assinaturas
               SET status = $2, inicio_periodo = $3, fim_periodo = $3, atualizado_em = now()
               WHERE usuario_id = $1 RETURNING *""",
            usuario_id, status, agora,
        )
    return _serializar(row)


# --------------------------------------------------------------------------
# Pagamentos
# --------------------------------------------------------------------------


async def criar_pagamento(
    usuario_id: str,
    plano_id: str,
    ciclo: str,
    valor: float,
    metodo: str = "",
    referencia: str = "",
) -> dict:
    p = cobranca.plano(plano_id)
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """INSERT INTO pagamentos (usuario_id, plano_id, ciclo, valor, metodo, referencia)
               VALUES ($1, $2, $3, $4, $5, $6) RETURNING *""",
            usuario_id, p.id, ciclo, valor, metodo, referencia,
        )
    return _serializar(row)


async def obter_pagamento(pagamento_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT * FROM pagamentos WHERE id = $1", pagamento_id)
    return _serializar(row) if row else None


async def pagamento_pendente_equivalente(
    usuario_id: str, plano_id: str, ciclo: str,
) -> dict | None:
    """Pagamento pendente da mesma conta para o mesmo plano e ciclo.

    É o que faz `POST /api/plano/troca` ser idempotente: o segundo clique no
    mesmo botão reencontra o pagamento que o primeiro criou, em vez de abrir
    outro. Sem isso, dez cliques dez pagamentos pagáveis — e, com o gateway
    ligado, dez PIX para o mesmo mês.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """SELECT * FROM pagamentos
                WHERE usuario_id = $1 AND plano_id = $2 AND ciclo = $3
                  AND status = 'pendente'
                ORDER BY id DESC LIMIT 1""",
            usuario_id, plano_id, cobranca.normalizar_ciclo(ciclo),
        )
    return _serializar(row) if row else None


async def listar_pagamentos(usuario_id: str, limite: int = 20) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT * FROM pagamentos WHERE usuario_id = $1 ORDER BY criado_em DESC LIMIT $2",
            usuario_id, limite,
        )
    return [_serializar(r) for r in rows]


async def obter_pagamento_por_referencia(referencia: str) -> dict | None:
    """Acha o pagamento pelo elo com o gateway (ex.: `mp-123456`).

    É o caminho do webhook: o MP manda `data.id` dele, e a nossa referência é
    `mp-{id do MP}` — nunca tem ambiguidade entre o id do MP e o nosso.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT * FROM pagamentos WHERE referencia = $1", referencia,
        )
    return _serializar(row) if row else None


async def guardar_checkout_pix(
    pagamento_id: int,
    referencia_mp: str,
    qr_code: str,
    expira_em: dt.datetime | str | None = None,
    valor: float | None = None,
) -> None:
    """Grava os dados do PIX gerado no provedor no pagamento pendente.

    O `qr_code` guardado é o texto "copia e cola" (pequeno e reexibível); a
    imagem base64 é pesada e fica só na resposta do checkout.

    `valor` é o que o checkout realmente mandou ao gateway, para a conferência
    de valor na confirmação não depender de uma coluna escrita antes do pedido.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        if valor is None:
            await con.execute(
                """UPDATE pagamentos
                   SET metodo = 'pix', referencia = $2, qr_code = $3, expira_em = $4
                   WHERE id = $1 AND status = 'pendente'""",
                pagamento_id, referencia_mp, qr_code, expira_em,
            )
            return
        await con.execute(
            """UPDATE pagamentos
               SET metodo = 'pix', referencia = $2, qr_code = $3, expira_em = $4,
                   valor = $5
               WHERE id = $1 AND status = 'pendente'""",
            pagamento_id, referencia_mp, qr_code, expira_em, float(valor),
        )


async def definir_metodo(pagamento_id: int, metodo: str) -> None:
    """Registry do meio usado. Serve para os isentos não saírem com o método
    em branco no livro de pagamentos."""
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE pagamentos SET metodo = $2 WHERE id = $1", pagamento_id, metodo,
        )


async def marcar_pagamento_pago(pagamento_id: int, referencia: str = "") -> tuple[dict | None, bool]:
    """Marca o pagamento como pago. Devolve `(linha, fez_a_transicao)`.

    O segundo valor é a trava contra período duplicado. O UPDATE só afeta
    pagamento ainda `pendente`, então de duas confirmações simultâneas do mesmo
    pagamento (webhook do MP + polling do navegador) uma recebe True e a outra
    False — e só a que recebeu True abre o período. Antes, as duas abriam.

    Devolve `({}, False)` quando o pagamento não existe: a leitura é feita
    dentro do mesmo bloco da conexão, porque usar `con` depois do
    `async with` levanta `InterfaceError` do asyncpg (a conexão já voltou para
    o pool) — o que fazia a confirmação idempotente estourar 500.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """UPDATE pagamentos
               SET status = 'pago', pago_em = now(),
                   referencia = COALESCE(NULLIF($2, ''), referencia)
               WHERE id = $1 AND status = 'pendente' RETURNING *""",
            pagamento_id, referencia,
        )
        if row:
            return _serializar(row), True
        row = await con.fetchrow("SELECT * FROM pagamentos WHERE id = $1", pagamento_id)
    return (_serializar(row) if row else None), False


# --------------------------------------------------------------------------
# Consumo de mensagens
# --------------------------------------------------------------------------


async def registrar_consumo(
    usuario_id: str | None,
    agente_id: int,
    quantidade: int = 1,
) -> None:
    """+1 mensagem na cota do agente, no mês corrente.

    `usuario_id` nulo (agente legado sem dono) não conta para ninguém: não há
    assinatura a que pertença.

    Falha de banco aqui NÃO pode derrubar a mensagem: o contador é limite
    comercial, não integridade da conversa. Log e segue. O item 15 (áudio,
    vídeo, foto) passa a contar várias mensagens por evento, então o
    `quantidade` é parâmetro e não constante.
    """
    if not usuario_id:
        return
    try:
        pool = await get_pool()
        async with pool.acquire() as con:
            await con.execute(
                """INSERT INTO consumo_mensagens (usuario_id, agente_id, mes, mensagens)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (usuario_id, agente_id, mes)
                   DO UPDATE SET mensagens = consumo_mensagens.mensagens + EXCLUDED.mensagens""",
                usuario_id, agente_id, mes_atual(), quantidade,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("Nao consegui registrar consumo (agente %s): %s", agente_id, e)


async def consumo_do_mes(usuario_id: str, mes: str | None = None) -> dict[int, int]:
    """{agente_id: mensagens usadas} no mês. Fonte da tela de perfil."""
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT agente_id, mensagens FROM consumo_mensagens
               WHERE usuario_id = $1 AND mes = $2""",
            usuario_id, mes or mes_atual(),
        )
    return {int(r["agente_id"]): int(r["mensagens"]) for r in rows}


async def zerar_consumo(usuario_id: str, mes: str | None = None) -> None:
    """Zera o mês (usado ao estender a assinatura, para a cota recomeçar)."""
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            "DELETE FROM consumo_mensagens WHERE usuario_id = $1 AND mes = $2",
            usuario_id, mes or mes_atual(),
        )


async def agentes_do_dono(dono_id: str) -> list[int]:
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT id FROM agentes WHERE dono_id = $1", dono_id)
    return [int(r["id"]) for r in rows]

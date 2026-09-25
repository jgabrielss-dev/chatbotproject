from __future__ import annotations

import asyncio
import json
from typing import Any

from app.database import get_pool

# Valor devolvido no lugar de um segredo salvo. O PUT de canal ignora esse
# marcador, então reenviar o config mascarado nunca sobrescreve o valor real.
MASCARA = "********"

# Chaves de canais.config que nunca podem sair da API.
CHAVES_SECRETAS = ("token", "senha", "sessionid", "secret")


def redigir_config(config: dict) -> dict:
    """Cópia segura para enviar ao navegador: segredos viram MASCARA."""
    saida: dict[str, Any] = {}
    for chave, valor in (config or {}).items():
        if chave in CHAVES_SECRETAS and valor:
            saida[chave] = MASCARA
        else:
            saida[chave] = valor
    return saida


def _json(value: Any) -> str:
    """Converte dict/list para string json (para bind em coluna JSONB)."""
    return json.dumps(value, ensure_ascii=False)


def _load(value: Any) -> Any:
    """asyncpg retorna JSONB como str; converte de volta para objeto."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value


# ---------- Agentes ----------

async def criar_agente(nome: str, system_prompt: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """INSERT INTO agentes (nome, system_prompt) VALUES ($1, $2)
               RETURNING id, nome, system_prompt, ativo, criado_em""",
            nome, system_prompt,
        )
    return dict(row)


async def listar_agentes() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT id, nome, system_prompt, ativo, criado_em FROM agentes ORDER BY criado_em DESC"
        )
    return [dict(r) for r in rows]


async def obter_agente(agente_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT id, nome, system_prompt, ativo, criado_em FROM agentes WHERE id = $1",
            agente_id,
        )
    return dict(row) if row else None


async def atualizar_agente(agente_id: int, nome: str, system_prompt: str, ativo: bool) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """UPDATE agentes SET nome = $1, system_prompt = $2, ativo = $3
               WHERE id = $4 RETURNING id, nome, system_prompt, ativo, criado_em""",
            nome, system_prompt, ativo, agente_id,
        )
    return dict(row) if row else None


async def excluir_agente(agente_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as con:
        result = await con.execute("DELETE FROM agentes WHERE id = $1", agente_id)
    return "DELETE 1" in result


# ---------- Canais ----------

async def criar_canal(agente_id: int, tipo: str, nome: str, config: dict) -> dict:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """INSERT INTO canais (agente_id, tipo, nome, config)
               VALUES ($1, $2, $3, $4::jsonb)
               RETURNING id, agente_id, tipo, nome, config, ativo, criado_em""",
            agente_id, tipo, nome, _json(config),
        )
    return _serialize_canal(row)


def _serialize_canal(row: Any) -> dict:
    d = dict(row)
    d["config"] = _load(d.get("config"))
    return d


async def listar_canais(agente_id: int | None = None) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as con:
        if agente_id is not None:
            rows = await con.fetch(
                "SELECT id, agente_id, tipo, nome, config, ativo, criado_em FROM canais WHERE agente_id = $1 ORDER BY criado_em",
                agente_id,
            )
        else:
            rows = await con.fetch(
                "SELECT id, agente_id, tipo, nome, config, ativo, criado_em FROM canais ORDER BY criado_em"
            )
    return [_serialize_canal(r) for r in rows]


async def obter_canal(canal_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT id, agente_id, tipo, nome, config, ativo, criado_em FROM canais WHERE id = $1",
            canal_id,
        )
    return _serialize_canal(row) if row else None


async def atualizar_canal(canal_id: int, nome: str, config: dict, ativo: bool) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """UPDATE canais SET nome = $1, config = $2::jsonb, ativo = $3
               WHERE id = $4 RETURNING id, agente_id, tipo, nome, config, ativo, criado_em""",
            nome, _json(config), ativo, canal_id,
        )
    return _serialize_canal(row) if row else None


async def excluir_canal(canal_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as con:
        result = await con.execute("DELETE FROM canais WHERE id = $1", canal_id)
    return "DELETE 1" in result


async def patch_canal_config(canal_id: int, campo: str, valor: Any) -> None:
    """Atualiza apenas uma chave do JSONB config do canal."""
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """UPDATE canais SET config = jsonb_set(config, $2::text[], $3::jsonb, true)
               WHERE id = $1""",
            canal_id, [campo],
            _json(valor),
        )


# ---------- Sessões ----------

async def obter_ou_criar_sessao(agente_id: int, canal_id: int, usuario_externo: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """INSERT INTO sessoes (agente_id, canal_id, usuario_externo)
               VALUES ($1, $2, $3)
               ON CONFLICT (canal_id, usuario_externo)
               DO UPDATE SET atualizado_em = now()
               RETURNING id, agente_id, canal_id, usuario_externo, memoria, criado_em, atualizado_em""",
            agente_id, canal_id, usuario_externo,
        )
    return dict(row)


async def atualizar_memoria(sessao_id: int, memoria: dict) -> None:
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE sessoes SET memoria = $1::jsonb, atualizado_em = now() WHERE id = $2",
            _json(memoria), sessao_id,
        )


# ---------- Mensagens ----------

async def salvar_mensagem(sessao_id: int, de_ia: bool, texto: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """INSERT INTO mensagens (sessao_id, de_ia, texto) VALUES ($1, $2, $3)
               RETURNING id, sessao_id, de_ia, texto, criado_em""",
            sessao_id, de_ia, texto,
        )
    return dict(row)


async def historico_sessao(sessao_id: int, limite: int = 20) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT de_ia, texto FROM mensagens
               WHERE sessao_id = $1 ORDER BY criado_em DESC, id DESC LIMIT $2""",
            sessao_id, limite,
        )
    return list(reversed([{**dict(r)} for r in rows]))


async def ultimos_trechos(sessao_id: int, limite: int = 6) -> list[str]:
    """Últimas mensagens em texto (para atualização de memória)."""
    hist = await historico_sessao(sessao_id, limite)
    return [f"{'assistente' if m['de_ia'] else 'usuario'}: {m['texto']}" for m in hist]


# ---------- Caixa de entrada (inbox durável) ----------

async def salvar_na_caixa(
    canal_id: int,
    remetente: str,
    texto: str,
    origem: str,
    payload: Any = None,
    status: str = "pendente",
    resposta: str | None = None,
) -> int | None:
    """Persiste uma mensagem recebida. Devolve o id criado, ou None se for
    duplicata (mesma origem, o mesmo evento reenviado pelo canal).

    'origem' e sempre o id unico do evento (tg:update_id, wa:key.id, ig:... ou
    web:uuid do webhook generico). O ON CONFLICT casa com o indice NAO parcial
    uq_caixa_origem, que e o unico formato que o PostgREST (upsert da edge
    function) consegue inferir. Por isso origem e obrigatoria e nunca pode ser
    vazia: com o indice nao parcial, duas mensagens vazias no mesmo canal
    colidiram e a segunda seria descartada em silencio.
    """
    if not origem:
        raise ValueError("salvar_na_caixa exige 'origem' unica: origem vazia perde mensagem.")
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """INSERT INTO caixa_entrada (canal_id, remetente, texto, origem, payload_json, status, resposta)
               VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
               ON CONFLICT (canal_id, origem) DO NOTHING
               RETURNING id""",
            canal_id, remetente, texto, origem, _json(payload or {}), status, resposta,
        )
    return row["id"] if row else None


# Colunas de caixa_entrada expostas ao painel. O erro cru fica em ultimo_erro e
# so e mostrado no html quando o usuario pede.
_COLS_CAIXA = (
    "c.id, c.canal_id, c.remetente, c.texto, c.origem, c.status, c.tentativas, "
    "c.ultimo_erro, c.criado_em, c.proxima_tentativa, c.processado_em, "
    "k.tipo AS tipo, k.nome AS canal_nome"
)
_JOIN_CAIXA = "FROM caixa_entrada c LEFT JOIN canais k ON k.id = c.canal_id"


async def listar_caixa_para_processar(limite: int = 10) -> list[dict]:
    """Fila em ordem de chegada. Como quem falha recebe 'proxima_tentativa' no
    futuro, ele sai da frente e volta para o fim da fila."""
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            f"""SELECT {_COLS_CAIXA} {_JOIN_CAIXA}
               WHERE c.status IN ('pendente', 'erro') AND c.proxima_tentativa <= now()
               ORDER BY c.proxima_tentativa ASC, c.id ASC
               LIMIT $1""",
            limite,
        )
    return [dict(r) for r in rows]


async def marcar_caixa_processando(msg_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """UPDATE caixa_entrada
               SET status = 'processando', tentativas = tentativas + 1, processando_em = now()
               WHERE id = $1""",
            msg_id,
        )


async def reenfileirar_processando(tolerancia_segundos: int = 600) -> int:
    """Devolve a fila mensagens presas em 'processando' (ex.: o app foi
    dormido/morto no meio do processamento). O corte usa processando_em e nao
    criado_em: sem isso, uma mensagem antiga que comecou a ser processada agora
    seria reenfileirada pela varredura seguinte (2s depois) e respondida 2x."""
    pool = await get_pool()
    async with pool.acquire() as con:
        result = await con.execute(
            """UPDATE caixa_entrada
               SET status = 'erro', proxima_tentativa = now(), processando_em = NULL,
                   ultimo_erro = COALESCE(ultimo_erro, 'processamento interrompido')
               WHERE status = 'processando'
                 AND processando_em IS NOT NULL
                 AND processando_em < now() - ($1 * interval '1 second')""",
            tolerancia_segundos,
        )
    return int(result.rsplit(" ", 1)[-1]) if result else 0


async def concluir_caixa(msg_id: int, status: str, resposta: str | None = None) -> None:
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """UPDATE caixa_entrada
               SET status = $2, resposta = $3, processado_em = now(),
                   processando_em = NULL, ultimo_erro = NULL
               WHERE id = $1""",
            msg_id, status, resposta,
        )


async def falhar_caixa(msg_id: int, proxima_tentativa: Any, ultimo_erro: str) -> None:
    """Mantem a mensagem na fila (nunca e descartada) e a devolve ao fim dela."""
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """UPDATE caixa_entrada
               SET status = 'erro', proxima_tentativa = $2, ultimo_erro = $3,
                   processando_em = NULL
               WHERE id = $1""",
            msg_id, proxima_tentativa, ultimo_erro,
        )


async def problemas_caixa(limite: int = 20) -> list[dict]:
    """Mensagens ainda nao entregues. E o que o painel mostra de forma concisa."""
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            f"""SELECT {_COLS_CAIXA} {_JOIN_CAIXA}
               WHERE c.status IN ('pendente', 'processando', 'erro')
               ORDER BY c.tentativas DESC, c.id ASC
               LIMIT $1""",
            limite,
        )
    return [dict(r) for r in rows]


async def resumo_caixa(limite: int = 10) -> dict:
    pool = await get_pool()
    async with pool.acquire() as con:
        counts_rows = await con.fetch(
            """SELECT status, count(*) AS total FROM caixa_entrada GROUP BY status"""
        )
        recentes = await con.fetch(
            f"""SELECT {_COLS_CAIXA} {_JOIN_CAIXA}
               ORDER BY c.id DESC LIMIT $1""",
            limite,
        )
        problemas = await con.fetch(
            f"""SELECT {_COLS_CAIXA} {_JOIN_CAIXA}
               WHERE c.status IN ('pendente', 'processando', 'erro')
               ORDER BY c.tentativas DESC, c.id ASC LIMIT 20"""
        )
    contagem = {str(r["status"]): r["total"] for r in counts_rows}
    return {
        "contagem": contagem,
        "problemas": [dict(r) for r in problemas],
        "recentes": [dict(r) for r in recentes],
    }


async def aguardar_caixa(msg_id: int, timeout_s: float = 25.0, passo_s: float = 0.4) -> dict | None:
    """Espera, sem bloquear o event loop, a fila terminar um item. Usado pelo
    webhook generico, que precisa devolver a resposta sincrona ao cliente mas
    nao pode perder o pedido se o worker demorar: expirado o prazo, o item
    continua na fila e sera respondido assim que houver tempo."""
    pool = await get_pool()
    loop = asyncio.get_running_loop()
    limite = loop.time() + timeout_s
    while True:
        async with pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT status, resposta, ultimo_erro FROM caixa_entrada WHERE id = $1", msg_id
            )
        if row is not None and (
            row["status"] in ("respondido", "sem_resposta") or row["ultimo_erro"]
        ):
            return dict(row)
        if loop.time() >= limite:
            return None
        await asyncio.sleep(passo_s)


# ---------- Sessoes do Instagram (persistidas no Postgres, sem custo) ----------

async def salvar_sessao_instagram(username: str, dados: dict) -> None:
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO instagram_sessoes (username, dados) VALUES ($1, $2::jsonb)
               ON CONFLICT (username) DO UPDATE
               SET dados = EXCLUDED.dados, atualizado_em = now()""",
            username, _json(dados),
        )


async def obter_sessao_instagram(username: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT dados FROM instagram_sessoes WHERE username = $1", username)
    return _load(row["dados"]) if row else None


async def excluir_sessao_instagram(username: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("DELETE FROM instagram_sessoes WHERE username = $1", username)

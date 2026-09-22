from __future__ import annotations

import json
from typing import Any

from app.database import get_pool


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
    origem: str = "",
    payload: Any = None,
    status: str = "pendente",
    resposta: str | None = None,
) -> bool:
    """Persiste uma mensagem recebida. Retorna False se for duplicata (mesmo origem)."""
    pool = await get_pool()
    async with pool.acquire() as con:
        result = await con.execute(
            """INSERT INTO caixa_entrada (canal_id, remetente, texto, origem, payload_json, status, resposta)
               VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
               ON CONFLICT (canal_id, origem) WHERE origem <> '' DO NOTHING""",
            canal_id, remetente, texto, origem, _json(payload or {}), status, resposta,
        )
    return "INSERT 0 1" in result


async def listar_caixa_para_processar(limite: int = 10) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT id, canal_id, remetente, texto, origem, payload_json, status,
                      tentativas, proxima_tentativa, ultimo_erro, criado_em
               FROM caixa_entrada
               WHERE status IN ('pendente', 'erro') AND proxima_tentativa <= now()
               ORDER BY criado_em ASC, id ASC
               LIMIT $1""",
            limite,
        )
    return [dict(r) for r in rows]


async def marcar_caixa_processando(msg_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE caixa_entrada SET status = 'processando' WHERE id = $1", msg_id
        )


async def concluir_caixa(msg_id: int, status: str, resposta: str | None = None) -> None:
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """UPDATE caixa_entrada
               SET status = $2, resposta = $3, processado_em = now()
               WHERE id = $1""",
            msg_id, status, resposta,
        )


async def falhar_caixa(msg_id: int, tentativas: int, proxima_tentativa: Any, ultimo_erro: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """UPDATE caixa_entrada
               SET status = 'erro', tentativas = $2, proxima_tentativa = $3, ultimo_erro = $4
               WHERE id = $1""",
            msg_id, tentativas, proxima_tentativa, ultimo_erro,
        )


async def resumo_caixa(limite: int = 10) -> dict:
    pool = await get_pool()
    async with pool.acquire() as con:
        counts_rows = await con.fetch(
            """SELECT status, count(*) AS total FROM caixa_entrada GROUP BY status"""
        )
        recentes = await con.fetch(
            """SELECT id, canal_id, remetente, texto, origem, status, tentativas,
                      ultimo_erro, criado_em, processado_em
               FROM caixa_entrada ORDER BY id DESC LIMIT $1""",
            limite,
        )
    contagem = {str(r["status"]): r["total"] for r in counts_rows}
    return {"contagem": contagem, "recentes": [dict(r) for r in recentes]}
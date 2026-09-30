"""Persistência. Tudo que devolve dado de agente passa por `dono_id`.

Regra do multi-tenant: o app roda com o papel `postgres`, que ignora RLS, então
o filtro por dono NÃO pode depender do banco. Quem chama precisa passar o
`dono_id` (uuid da conta logada) e o repositório restringe o SELECT a ele. Sem
esse argumento as funções de agente não expõem nada — é o que impede um
`WHERE id = $1` com id chuteado de virar leitura da fila de outro cliente.
O admin é a única exceção: passa `dono_id=None` e vê tudo.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.database import get_pool

log = logging.getLogger("repositories")

# Valor devolvido no lugar de um segredo salvo. O PUT de canal ignora esse
# marcador, então reenviar o config mascarado nunca sobrescreve o valor real.
MASCARA = "********"

# Chaves de canais.config que nunca podem sair da API.
#
# Havia DUAS listas aqui e elas divergiam: a curta (usada por main._canal_publico,
# que atendia GET /api/agentes/{id}/canais) cobria só token/senha/sessionid/
# secret, e deixava o access_token, o app_secret e o verify_token da Meta
# irem em claro para o navegador. A lista é única agora, e `redigir_config` é a
# única função de redação: um segredo novo entra aqui e vale para toda resposta.
CHAVES_SECRETAS = {
    "token",                 # Telegram
    "access_token",          # Meta oficial
    "meta_access_token",
    "app_secret",            # Meta oficial (assinatura do webhook)
    "meta_app_secret",
    "verify_token",          # Meta oficial (handshake do webhook)
    "meta_verify_token",
    "senha",                 # legado
    "sessionid",             # Instagram não-oficial
    "secret",                # segredo de webhook do Telegram
    "ig_vistos",             # estado interno do poller
    "qr",                    # QR code do Baileys
}


def redigir_config(config: Any) -> Any:
    """Cópia segura para enviar ao navegador: segredos viram MASCARA.

    Use em TODA resposta que carregue `canais.config`. O placeholder é
    idempotente, então reenviar o config mascarado no PUT não sobrescreve o
    valor real (ver `mesclar_config_sync`).
    """
    if not isinstance(config, dict):
        return config
    saida: dict[str, Any] = {}
    for chave, valor in config.items():
        if chave in CHAVES_SECRETAS:
            saida[chave] = MASCARA if valor else ""
        else:
            saida[chave] = valor
    return saida


def canal_publico(canal: dict | None) -> dict | None:
    """Canal com a config redigida — a forma que pode sair da API."""
    if not canal:
        return canal
    saida = dict(canal)
    saida["config"] = redigir_config(canal.get("config") or {})
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


def _sem_secrets(canal: dict) -> dict:
    """Alias legado de `canal_publico`, mantido porque o poller e o worker usam."""
    return canal_publico(canal)


# ---------- Perfis (contas de usuário) ----------

async def criar_perfil(usuario_id: str, email: str, role: str = "usuario") -> dict:
    """Perfil para uma conta que o trigger do banco não alcançou (conta criada
    antes da 0004, ou trigger removido). Nunca promove para admin: o papel só
    vem do app_config, por SQL."""
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """INSERT INTO perfis (id, email, role) VALUES ($1, $2, $3)
               ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email
               RETURNING id, nome, email, role, bloqueado, mfa_ativo, criado_em""",
            usuario_id, email or "", role,
        )
    return dict(row)


async def obter_perfil(usuario_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT id, nome, email, role, bloqueado, mfa_ativo, criado_em FROM perfis WHERE id = $1",
            usuario_id,
        )
    return dict(row) if row else None


async def atualizar_nome_perfil(usuario_id: str, nome: str) -> None:
    """Nome de exibição. O e-mail NÃO muda aqui: o login é no Supabase Auth."""
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("UPDATE perfis SET nome = $2 WHERE id = $1", usuario_id, nome or "")


async def ler_config(chave: str, padrao: str = "") -> str:
    """Valor de `app_config`. Ausente devolve o padrão, não None.

    `app_config` guarda o e-mail de atendimento (item 10) e a lista de admins
    que o trigger do banco lê. Uma chave nova não precisa de migration: ela é
    criada no primeiro `gravar_config`.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        valor = await con.fetchval("SELECT valor FROM app_config WHERE chave = $1", chave)
    return padrao if valor is None else str(valor)


async def gravar_config(chave: str, valor: str) -> None:
    """Upsert em `app_config`. `valor` é sempre TEXT; a lista de admins é
    separada por linha, e quem consome faz o split."""
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO app_config (chave, valor) VALUES ($1, $2)
               ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor""",
            chave, valor or "",
        )


async def definir_mfa_ativo(usuario_id: str, ativo: bool) -> None:
    """Liga ou desliga a exigencia do segundo fator na nossa API.

    Quem chama precisa ter um token `aal2`: e o jeito de provar, sem trocar
    nenhum segredo com o GoTrue, que a pessoa tem o app autenticador na mao.
    Apagar o fator no Supabase tambem exige `aal2`, entao o mesmo prova vale
    para desligar -- nao existe caminho para contornar o 2FA pelo painel.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE perfis SET mfa_ativo = $2 WHERE id = $1", usuario_id, bool(ativo)
        )


async def atualizar_email_perfil(usuario_id: str, email: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("UPDATE perfis SET email = $2 WHERE id = $1", usuario_id, email or "")


async def definir_papel(usuario_id: str, role: str, bloqueado: bool | None = None) -> dict | None:
    """Muda papel (e opcionalmente o bloqueio). Só o admin chega até aqui."""
    pool = await get_pool()
    async with pool.acquire() as con:
        if bloqueado is None:
            row = await con.fetchrow(
                """UPDATE perfis SET role = $2 WHERE id = $1
                   RETURNING id, email, role, bloqueado, criado_em""",
                usuario_id, role,
            )
        else:
            row = await con.fetchrow(
                """UPDATE perfis SET role = $2, bloqueado = $3 WHERE id = $1
                   RETURNING id, email, role, bloqueado, criado_em""",
                usuario_id, role, bloqueado,
            )
    return dict(row) if row else None


async def contar_admins() -> int:
    """Quantos admins ativos existem. O painel usa isto para não deixar o
    último admin se rebaixar e trancar todo mundo fora do painel."""
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT count(*) AS total FROM perfis WHERE role = 'admin' AND NOT bloqueado"
        )
    return int(row["total"]) if row else 0


async def listar_perfis() -> list[dict]:
    """Contas com o total de agentes de cada uma. Só o admin chama."""
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT p.id, p.nome, p.email, p.role, p.bloqueado, p.criado_em,
                      (SELECT count(*) FROM agentes a WHERE a.dono_id = p.id) AS agentes
               FROM perfis p
               ORDER BY (p.role = 'admin') DESC, lower(p.email) ASC"""
        )
    return [dict(r) for r in rows]


# ---------- Agentes ----------

async def criar_agente(nome: str, system_prompt: str, dono_id: str | None) -> dict:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """INSERT INTO agentes (nome, system_prompt, dono_id) VALUES ($1, $2, $3)
               RETURNING id, nome, system_prompt, ativo, dono_id, criado_em""",
            nome, system_prompt, dono_id,
        )
    return dict(row)


async def listar_agentes(dono_id: str | None) -> list[dict]:
    """Agentes do dono. `dono_id=None` significa admin: vê todos.

    Mesmo quando a lista é restrita, cada linha volta com o total de sessões:
    o painel do cliente mostra uso real e não só o cadastro.

    `interno IS NULL` em ambos os casos: os agentes da plataforma (itens 7, 12 e
    13) não são de ninguém, e por isso apareceriam na lista do admin — que
    enxerga tudo. Eles não são cadastro de agente, são código
    (`app/agentes_internos.py`), e uma tela que lista e oferece editar um
    arquivo de código é um convite a editá-lo.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        # `sessoes` e `canais` são os totais por agente. Vêm no mesmo SELECT
        # porque o painel do cliente mostra uso real, e dois COUNT por agente
        # dariam N+1 numa lista.
        base = """SELECT a.id, a.nome, a.system_prompt, a.ativo, a.dono_id, a.criado_em,
                  (SELECT count(*) FROM sessoes s WHERE s.agente_id = a.id) AS sessoes,
                  (SELECT count(*) FROM canais c WHERE c.agente_id = a.id) AS canais,
                  p.email AS dono_email
               FROM agentes a
               LEFT JOIN perfis p ON p.id = a.dono_id"""
        if dono_id is None:
            rows = await con.fetch(
                base + " WHERE a.interno IS NULL ORDER BY a.criado_em DESC")
        else:
            rows = await con.fetch(
                base + " WHERE a.dono_id = $1 AND a.interno IS NULL"
                " ORDER BY a.criado_em DESC", dono_id)
    return [dict(r) for r in rows]


async def obter_agente(agente_id: int, dono_id: str | None) -> dict | None:
    """Agente, mas só se o dono bater. `dono_id=None` = admin, sem filtro."""
    pool = await get_pool()
    async with pool.acquire() as con:
        if dono_id is None:
            row = await con.fetchrow(
                """SELECT a.id, a.nome, a.system_prompt, a.ativo, a.dono_id, a.criado_em,
                          p.email AS dono_email
                   FROM agentes a LEFT JOIN perfis p ON p.id = a.dono_id
                   WHERE a.id = $1""",
                agente_id,
            )
        else:
            row = await con.fetchrow(
                """SELECT a.id, a.nome, a.system_prompt, a.ativo, a.dono_id, a.criado_em,
                          p.email AS dono_email
                   FROM agentes a LEFT JOIN perfis p ON p.id = a.dono_id
                   WHERE a.id = $1 AND a.dono_id = $2""",
                agente_id, dono_id,
            )
    return dict(row) if row else None


async def atualizar_agente(
    agente_id: int, nome: str, system_prompt: str, ativo: bool, dono_id: str | None
) -> dict | None:
    # `interno IS NULL` no WHERE: os agentes da plataforma não são cadastro, e
    # editar o prompt da linha não mudaria o agente de verdade (o prompt vem
    # de `app/agentes_internos.py`). O que a edição faria é tirar o agente do
    # ar enquanto a tela mostra que ele está editável — o item 14 fala em não
    # poder desviar, e esta é a porta por onde isso aconteceria.
    pool = await get_pool()
    async with pool.acquire() as con:
        if dono_id is None:
            row = await con.fetchrow(
                """UPDATE agentes SET nome = $1, system_prompt = $2, ativo = $3
                   WHERE id = $4 AND interno IS NULL
                   RETURNING id, nome, system_prompt, ativo, dono_id, criado_em""",
                nome, system_prompt, ativo, agente_id,
            )
        else:
            row = await con.fetchrow(
                """UPDATE agentes SET nome = $1, system_prompt = $2, ativo = $3
                   WHERE id = $4 AND dono_id = $5 AND interno IS NULL
                   RETURNING id, nome, system_prompt, ativo, dono_id, criado_em""",
                nome, system_prompt, ativo, agente_id, dono_id,
            )
    return dict(row) if row else None


async def excluir_agente(agente_id: int, dono_id: str | None) -> bool:
    pool = await get_pool()
    async with pool.acquire() as con:
        if dono_id is None:
            result = await con.execute(
                "DELETE FROM agentes WHERE id = $1 AND interno IS NULL", agente_id)
        else:
            result = await con.execute(
                "DELETE FROM agentes WHERE id = $1 AND dono_id = $2 AND interno IS NULL",
                agente_id, dono_id,
            )
    return "DELETE 1" in result


# --------------------------------------------------------------------------
# Agentes internos (itens 7, 12, 13)
#
# A linha existe por dois motivos práticos e nenhum de identidade: `sessoes`
# tem `agente_id NOT NULL` e `canais` tem `agente_id NOT NULL`, então o chat
# interno precisa de um par agente/canal para gravar a conversa. A identidade
# do agente — nome e prompt — NÃO sai daqui: vem de `app/agentes_internos.py`.
# Editar esta linha não muda o que o agente responde, e o item 14 pede
# exatamente isso.
# --------------------------------------------------------------------------


async def garantir_agentes_internos(definicoes: list[tuple[str, str, str]]) -> list[dict]:
    """Cria (ou reaproveita) agente e canal 'site' de cada agente interno.

    `definicoes` é `[(chave, nome, slug_do_canal), ...]`. Roda no boot, e é
    idempotente pelo `interno`: subir de novo não cria um segundo agente nem
    perde o histórico de quem já conversou.

    O `system_prompt` gravado é o do código, mas só como registro: a rota de
    chat sempre usa `agentes_internos.PRODUTO.prompt`, nunca a coluna. Deixar a
    coluna sincronizada evita que alguém que forje um agente interno na linha
    (ou leia a tabela) veja um prompt diferente do que o agente usa.
    """
    pool = await get_pool()
    saida: list[dict] = []
    async with pool.acquire() as con:
        for chave, nome, system_prompt in definicoes:
            agente = await con.fetchrow(
                """INSERT INTO agentes (nome, system_prompt, interno, ativo)
                   VALUES ($1, $2, $3, TRUE)
                   ON CONFLICT (interno) WHERE interno IS NOT NULL
                   DO UPDATE SET nome = EXCLUDED.nome,
                                 system_prompt = EXCLUDED.system_prompt
                   RETURNING id, nome, system_prompt, ativo, dono_id, interno""",
                nome, system_prompt, chave,
            )
            canal = await con.fetchrow(
                """INSERT INTO canais (agente_id, tipo, nome, config, ativo)
                   VALUES ($1, 'site', $2, $3::jsonb, TRUE)
                   ON CONFLICT (agente_id) WHERE tipo = 'site'
                   DO UPDATE SET nome = EXCLUDED.nome
                   RETURNING id, agente_id, tipo, nome, config, ativo""",
                agente["id"], f"chat interno: {chave}", _json({"interno": chave}),
            )
            saida.append({"agente": dict(agente), "canal": dict(canal)})
    return saida


async def par_interno(chave: str) -> tuple[dict, dict] | None:
    """(agente, canal) do agente interno. `None` se ele não foi criado ainda."""
    pool = await get_pool()
    async with pool.acquire() as con:
        agente = await con.fetchrow(
            """SELECT id, nome, system_prompt, ativo, dono_id, interno FROM agentes
               WHERE interno = $1""", chave,
        )
        if not agente:
            return None
        canal = await con.fetchrow(
            """SELECT id, agente_id, tipo, nome, config, ativo FROM canais
               WHERE agente_id = $1 AND tipo = 'site'""", agente["id"],
        )
    if not canal:
        return None
    return dict(agente), dict(canal)


async def reivindicar_agentes_sem_dono(dono_id: str) -> int:
    """Adota os agentes criados antes do multi-tenant (dono_id NULL).

    Roda no primeiro login de um admin: é o que impede a perda dos agentes que
    já estavam em produção quando a 0004 entrou. Sem dono, eles seriam invisíveis
    para todo mundo — o painel do admin ainda os veria, mas o dono nunca os
    encontraria.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        result = await con.execute(
            # `interno IS NULL` também aqui: um agente interno não tem dono, mas
            # "reivindicar" não pode fazer dele do agente de ninguém — a casa do
            # agente interno é o código (app/agentes_internos.py), não a conta.
            "UPDATE agentes SET dono_id = $1 WHERE dono_id IS NULL AND interno IS NULL", dono_id
        )
    total = int(result.rsplit(" ", 1)[-1]) if result else 0
    if total:
        log.info("%s agente(s) legado(s) adotado(s) pelo admin %s", total, dono_id)
    return total


async def definir_dono_agente(agente_id: int, dono_id: str | None) -> dict | None:
    """Transfere um agente para outra conta (admin)."""
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "UPDATE agentes SET dono_id = $2 WHERE id = $1 RETURNING id, nome, dono_id, criado_em",
            agente_id, dono_id,
        )
    return dict(row) if row else None


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


# Versão com segredos redigidos, para devolver ao navegador via API.
def _serialize_canal_publico(row: Any) -> dict:
    return _sem_secrets(_serialize_canal(row))


async def listar_canais(agente_id: int | None = None, *, redigir: bool = False) -> list[dict]:
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
    return [(_serialize_canal_publico if redigir else _serialize_canal)(r) for r in rows]


async def obter_canal(canal_id: int, *, redigir: bool = False) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT id, agente_id, tipo, nome, config, ativo, criado_em FROM canais WHERE id = $1",
            canal_id,
        )
    if not row:
        return None
    return (_serialize_canal_publico if redigir else _serialize_canal)(row)


async def obter_canal_do_dono(canal_id: int, dono_id: str | None) -> dict | None:
    """Canal, mas só se o agente dele pertencer a quem pergunta.

    É o portão das rotas que recebem só `canal_id` (editar, excluir, testar,
    ler a URL do webhook): sem o join por `dono_id`, bastaria chutar o id
    sequencial para operar no canal de outro cliente.
    `dono_id=None` = admin, sem filtro.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        if dono_id is None:
            row = await con.fetchrow(
                """SELECT c.id, c.agente_id, c.tipo, c.nome, c.config, c.ativo, c.criado_em
                   FROM canais c WHERE c.id = $1""",
                canal_id,
            )
        else:
            row = await con.fetchrow(
                """SELECT c.id, c.agente_id, c.tipo, c.nome, c.config, c.ativo, c.criado_em
                   FROM canais c JOIN agentes a ON a.id = c.agente_id
                   WHERE c.id = $1 AND a.dono_id = $2""",
                canal_id, dono_id,
            )
    return _serialize_canal(row) if row else None


async def listar_canais_do_dono(dono_id: str | None) -> list[dict]:
    """Todos os canais de todos os agentes do dono (painel do cliente)."""
    pool = await get_pool()
    async with pool.acquire() as con:
        if dono_id is None:
            rows = await con.fetch(
                """SELECT c.id, c.agente_id, c.tipo, c.nome, c.config, c.ativo, c.criado_em,
                          a.nome AS agente_nome
                   FROM canais c JOIN agentes a ON a.id = c.agente_id
                   ORDER BY c.criado_em"""
            )
        else:
            rows = await con.fetch(
                """SELECT c.id, c.agente_id, c.tipo, c.nome, c.config, c.ativo, c.criado_em,
                          a.nome AS agente_nome
                   FROM canais c JOIN agentes a ON a.id = c.agente_id
                   WHERE a.dono_id = $1
                   ORDER BY c.criado_em""",
                dono_id,
            )
    return [_serialize_canal(r) for r in rows]


async def atualizar_canal(canal_id: int, nome: str, config: dict, ativo: bool) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """UPDATE canais SET nome = $1, config = $2::jsonb, ativo = $3
               WHERE id = $4 RETURNING id, agente_id, tipo, nome, config, ativo, criado_em""",
            nome, _json(config), ativo, canal_id,
        )
    return _serialize_canal(row) if row else None


async def atualizar_canal_publico(canal_id: int, nome: str, config: dict, ativo: bool) -> dict | None:
    """Igual a atualizar_canal, mas devolve a config já redigida."""
    canal = await atualizar_canal(canal_id, nome, config, ativo)
    return _sem_secrets(canal) if canal else None


def mesclar_config_sync(canal: dict, patch: dict) -> dict:
    """Mescla `patch` na config já salva, preservando os segredos.

    O painel reexibe '********' nas chaves secretas; se o usuário salvar sem
    digitar nada, esse placeholder NÃO pode sobrescrever o valor real.
    """
    base = dict(canal.get("config") or {})
    for chave, valor in (patch or {}).items():
        if valor is None:
            continue
        if valor == MASCARA and chave in CHAVES_SECRETAS:
            continue
        if isinstance(valor, str) and not valor.strip() and chave in CHAVES_SECRETAS:
            # Campo vazio = "não quero trocar o segredo", não "apague o segredo".
            continue
        base[chave] = valor
    return base


async def mesclar_config(canal_id: int, patch: dict) -> dict | None:
    """Atualiza só as chaves presentes em `patch`, preservando os segredos."""
    canal = await obter_canal(canal_id)
    if not canal:
        return None
    base = mesclar_config_sync(canal, patch)
    return await atualizar_canal(canal_id, canal["nome"], base, canal["ativo"])


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


async def listar_sessoes(agente_id: int, limite: int = 50) -> list[dict]:
    """Sessões de um agente, com o canal de origem e o total de mensagens.

    A memória (JSONB) vem junto: é o resumo estruturado que a IA extraiu do
    cliente, que é justamente o dado que o dono do agente quer conferir.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT s.id, s.agente_id, s.canal_id, s.usuario_externo, s.memoria,
                      s.criado_em, s.atualizado_em,
                      c.tipo AS canal_tipo, c.nome AS canal_nome,
                      (SELECT count(*) FROM mensagens m WHERE m.sessao_id = s.id) AS mensagens
               FROM sessoes s LEFT JOIN canais c ON c.id = s.canal_id
               WHERE s.agente_id = $1
               ORDER BY s.atualizado_em DESC LIMIT $2""",
            agente_id, limite,
        )
    return [{**dict(r), "memoria": _load(r["memoria"])} for r in rows]


async def obter_sessao_do_dono(sessao_id: int, dono_id: str | None) -> dict | None:
    """Sessão, mas só se o agente dela pertencer a quem pergunta."""
    pool = await get_pool()
    async with pool.acquire() as con:
        if dono_id is None:
            row = await con.fetchrow(
                """SELECT s.*, c.tipo AS canal_tipo, c.nome AS canal_nome
                   FROM sessoes s LEFT JOIN canais c ON c.id = s.canal_id
                   WHERE s.id = $1""",
                sessao_id,
            )
        else:
            row = await con.fetchrow(
                """SELECT s.*, c.tipo AS canal_tipo, c.nome AS canal_nome
                   FROM sessoes s
                   JOIN agentes a ON a.id = s.agente_id
                   LEFT JOIN canais c ON c.id = s.canal_id
                   WHERE s.id = $1 AND a.dono_id = $2""",
                sessao_id, dono_id,
            )
    return {**dict(row), "memoria": _load(row["memoria"])} if row else None


# ---------- Mensagens ----------

async def registrar_consumo(usuario_id: str | None, agente_id: int, quantidade: int = 1) -> None:
    """Conta 1 mensagem na cota do agente. Encaminha para o bloco de cobrança.

    Existe aqui (e não só em `repos_cobranca`) porque `pipeline.py` já conversa
    com `repo.` e um import a mais ali só criaria um segundo nome para a mesma
    coisa. `dono_id` nulo (agente legado) não conta para ninguém.
    """
    from app.repos_cobranca import registrar_consumo as _registrar

    await _registrar(usuario_id, agente_id, quantidade)


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


async def id_da_sessao(agente_id: int, canal_id: int, usuario_externo: str) -> int | None:
    """O id da sessão (agente, canal, pessoa), ou None se ainda não existe.

    Existe para o front recarregar a conversa sem criá-la: perguntar o histórico
    de uma conversa que ninguém começou não deveria gravar uma linha nova.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """SELECT id FROM sessoes
               WHERE agente_id = $1 AND canal_id = $2 AND usuario_externo = $3""",
            agente_id, canal_id, usuario_externo,
        )
    return int(row["id"]) if row else None


async def contar_mensagens_da_sessao(agente_id: int, canal_id: int,
                                    usuario_externo: str) -> int:
    """Quantas vezes a PESSOA falou nesta conversa.

    Só as mensagens dela: a cota do item 14 para o agente da home é "quantas
    perguntas o visitante pode fazer", e contar a resposta do agente faria a
    conversa acabar mais rápido quanto mais elle corresse.

    A contagem vai no banco, e não em memória, porque memória morre no restart
    do Render — e um limite que se resolve com F5 não é limite.
    """
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """SELECT count(*) AS total FROM mensagens m
               JOIN sessoes s ON s.id = m.sessao_id
               WHERE s.agente_id = $1 AND s.canal_id = $2
                 AND s.usuario_externo = $3 AND m.de_ia = FALSE""",
            agente_id, canal_id, usuario_externo,
        )
    return int(row["total"]) if row else 0


async def mensagens_da_sessao(sessao_id: int, limite: int = 200) -> list[dict]:
    """Conversa completa em ordem cronológica, para a tela de sessão."""
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT id, de_ia, texto, criado_em FROM mensagens
               WHERE sessao_id = $1 ORDER BY criado_em, id LIMIT $2""",
            sessao_id, limite,
        )
    return [dict(r) for r in rows]


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
    # `payload_json` entra na lista porque e de onde sai a REFERENCIA do
    # anexo (item 15): url, base64 ou o id que o canal reconhece. O worker
    # nao baixa nada aqui - ele so precisa saber que ha anexo e onde
    # busca-lo. Sem esta coluna, `listar_caixa_para_processar` devolvia um
    # item sem informacao de midia e o item 15 seria um botao morto.
    "c.payload_json, "
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


async def resumo_caixa(limite: int = 10, dono_id: str | None = None) -> dict:
    """Contadores + pendências da fila.

    Com `dono_id` a visão é do tenant: só as mensagens que entraram nos canais
    dos agentes daquele usuário. Sem ele é a visão global do admin. O `texto`
    e o `remetente` são de conversas de cliente, então devolver a fila inteira
    para qualquer usuário logado seria vazar dado de terceiros — daí o filtro
    ser obrigatório para todo mundo que não seja admin.
    """
    # O join é sempre necessário: é ele que liga a fila ao dono. Sem dono
    # (admin) o filtro fica vazio e o join não restringe nada — o agente é
    # removido em cascata junto com o canal, então o inner join não oculta fila.
    join_dono = " JOIN agentes a ON a.id = c.canal_id"
    filtro_dono = ""
    params_extra: list[Any] = []
    if dono_id is not None:
        # $1 e o dono, e o LIMIT do `recentes` e ${len(params_extra)+1} = $2.
        # Filtrar em $2 comparava o uuid de a.dono_id com o limite (um int) e a
        # rota quebrava com erro de tipo justamente para o usuario comum.
        filtro_dono = " AND a.dono_id = $1"
        params_extra = [dono_id]

    pool = await get_pool()
    async with pool.acquire() as con:
        counts_rows = await con.fetch(
            f"""SELECT c.status, count(*) AS total
                FROM caixa_entrada c{join_dono}
                WHERE 1 = 1{filtro_dono}
                GROUP BY c.status""",
            *params_extra,
        )
        recentes = await con.fetch(
            f"""SELECT {_COLS_CAIXA} {_JOIN_CAIXA}{join_dono}
                WHERE 1 = 1{filtro_dono}
                ORDER BY c.id DESC LIMIT ${len(params_extra) + 1}""",
            *params_extra, limite,
        )
        problemas = await con.fetch(
            f"""SELECT {_COLS_CAIXA} {_JOIN_CAIXA}{join_dono}
                WHERE c.status IN ('pendente', 'processando', 'erro'){filtro_dono}
                ORDER BY c.tentativas DESC, c.id ASC LIMIT 20""",
            *params_extra,
        )
    contagem = {str(r["status"]): r["total"] for r in counts_rows}
    return {
        "contagem": contagem,
        "problemas": [dict(r) for r in problemas],
        "recentes": [dict(r) for r in recentes],
    }


async def estatisticas_do_dono(dono_id: str | None) -> dict:
    """Números da home do cliente: agentes, canais, sessões, mensagens e fila.

    Um SELECT só, porque a home abre em toda visita e o plano grátis do Render
    paga por tempo de CPU. `dono_id=None` agrega a plataforma inteira (admin).
    """
    filtro = " AND a.dono_id = $1" if dono_id is not None else ""
    params: list[Any] = [dono_id] if dono_id is not None else []
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            f"""SELECT
                  (SELECT count(*) FROM agentes a WHERE 1 = 1{filtro}) AS agentes,
                  (SELECT count(*) FROM canais c JOIN agentes a ON a.id = c.agente_id
                    WHERE 1 = 1{filtro}) AS canais,
                  (SELECT count(*) FROM sessoes s JOIN agentes a ON a.id = s.agente_id
                    WHERE 1 = 1{filtro}) AS sessoes,
                  (SELECT count(*) FROM mensagens m
                     JOIN sessoes s ON s.id = m.sessao_id
                     JOIN agentes a ON a.id = s.agente_id
                    WHERE 1 = 1{filtro}) AS mensagens,
                  (SELECT count(*) FROM caixa_entrada c JOIN agentes a ON a.id = c.canal_id
                    WHERE c.status IN ('pendente', 'processando', 'erro'){filtro}) AS fila_pendente,
                  (SELECT count(*) FROM mensagens m
                     JOIN sessoes s ON s.id = m.sessao_id
                     JOIN agentes a ON a.id = s.agente_id
                    WHERE m.criado_em > now() - interval '7 days'{filtro}) AS mensagens_7d,
                  (SELECT count(*) FROM mensagens m
                     JOIN sessoes s ON s.id = m.sessao_id
                     JOIN agentes a ON a.id = s.agente_id
                    WHERE m.de_ia AND m.criado_em > now() - interval '7 days'{filtro}) AS respostas_7d""",
            *params,
        )
    return dict(row)


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

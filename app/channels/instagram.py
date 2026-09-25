from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from instagrapi import Client

from app import repositories as repo
from app.config import settings

log = logging.getLogger("instagram")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Clientes autenticados por usuário, com lock por usuário (instagrapi é síncrono).
_clientes: dict[str, Client] = {}
_locks: dict[str, asyncio.Lock] = {}

# ATENÇÃO: automação de Instagram por meios não-oficiais (instagrapi) viola os
# termos do Meta, pode quebrar a qualquer momento e causar bloqueio da conta.
# Para uso comercial estável, o CLIENTE deve obter a API oficial da Meta
# (Instagram Messaging API) e usar o adapter oficial; aqui fica o método
# não-oficial com aviso explícito.


def _arquivo_sessao(username: str) -> Path:
    return DATA_DIR / f"ig_{username}.json"


def _chave(username: str, password: str, sessionid: str) -> str:
    """Chave de cache/lock que muda quando as credenciais mudam."""
    marca = hashlib.sha1(f"{password}|{sessionid}".encode()).hexdigest()[:8]
    return f"{username}:{marca}"


def _lock(chave: str) -> asyncio.Lock:
    return _locks.setdefault(chave, asyncio.Lock())


async def _salvar_sessao(username: str, client: Client) -> None:
    """Guarda a sessão onde ela sobrevive ao cold start do Render.

    O disco do Render é efêmero e o Postgres já está pago: depender só do disco
    obrigava a refazer login a cada wake, que é exatamente o que dispara
    checkpoint/login-blocked do Instagram. O Postgres é a fonte da verdade e o
    disco continua só como cache local.
    """
    dados = client.settings
    if settings.has_db:
        try:
            await repo.salvar_sessao_instagram(username, dados)
        except Exception as e:
            log.warning("Não consegui salvar a sessão do Instagram no banco: %s", e)
    try:
        client.dump_settings(_arquivo_sessao(username))
    except Exception as e:
        log.warning("Não consegui salvar a sessão do Instagram em disco: %s", e)


def _login_sync(username: str, password: str, sessionid: str, dados_banco: dict | None) -> Client:
    """Restaura a sessão salva (sem refazer login) e autentica se necessário."""
    client = Client()
    client.delay_range = [1, 3]
    restaurada = False
    if dados_banco:
        try:
            client.set_settings(dados_banco)
            restaurada = True
        except Exception as e:
            log.warning("Sessão do banco inválida, tentando o cache em disco: %s", e)
    if not restaurada and _arquivo_sessao(username).exists():
        try:
            client.load_settings(_arquivo_sessao(username))
        except Exception as e:
            log.warning("Sessão Instagram inválida, relogando: %s", e)
    try:
        if sessionid:
            # Alternativa quando o login por senha é bloqueado (nta_upsell):
            # reutiliza o cookie "sessionid" de um navegador já logado.
            client.login_by_sessionid(sessionid)
        else:
            client.login(username, password)
    except Exception as e:
        log.error("Falha ao autenticar Instagram de %s: %s", username, e)
        raise
    return client


async def _obter_cliente_locked(chave: str, username: str, password: str, sessionid: str) -> Client:
    """Cliente logado. Exige o lock da chave já adquirido."""
    if chave in _clientes:
        return _clientes[chave]
    dados = None
    if settings.has_db:
        try:
            dados = await repo.obter_sessao_instagram(username)
        except Exception as e:
            log.warning("Falha ao ler a sessão do Instagram do banco: %s", e)
    client = await asyncio.to_thread(_login_sync, username, password, sessionid, dados)
    _clientes[chave] = client
    await _salvar_sessao(username, client)
    return client


async def obter_cliente(username: str, password: str = "", sessionid: str = "") -> Client:
    """Garante um cliente logado (com cache em memória, disco e banco)."""
    chave = _chave(username, password, sessionid)
    async with _lock(chave):
        return await _obter_cliente_locked(chave, username, password, sessionid)


async def _coletar_sync(client: Client, ja_vistos: dict) -> tuple[list[tuple[str, str, str]], dict]:
    vistos = dict(ja_vistos or {})
    novos: list[tuple[str, str, str]] = []
    threads = []
    try:
        threads += client.direct_messages.pending_threads(amount=20)
    except Exception as e:
        log.warning("pending_threads: %s", e)
    try:
        threads += client.direct_messages.threads(amount=20, thread_message_amount=20)
    except Exception as e:
        log.warning("threads: %s", e)

    for thread in threads:
        thread_id = thread.thread_id
        processados = set(vistos.get(thread_id, []))
        novos_ids = set()
        for msg in reversed(thread.messages):
            if str(msg.user_id) == str(client.user_id):
                continue
            if not msg.text:
                continue
            if msg.id in processados:
                continue
            novos_ids.add(msg.id)
            novos.append((thread_id, msg.id, msg.text))
        if novos_ids:
            vistos[thread_id] = list(set(processados) | novos_ids)
    return novos, vistos


async def coletar_novas(
    username: str, password: str, sessionid: str, ja_vistos: dict
) -> tuple[list[tuple[str, str, str]], dict]:
    """Retorna (mensagens novas, vistos atualizado).
    (thread_id, msg_id, texto) - apenas mensagens de outras pessoas."""
    chave = _chave(username, password, sessionid)
    async with _lock(chave):
        client = await _obter_cliente_locked(chave, username, password, sessionid)
        novos, vistos = await asyncio.to_thread(_coletar_sync, client, ja_vistos)
        # a navegação renova cookies: só então vale a pena persistir de novo
        await _salvar_sessao(username, client)
        return novos, vistos


async def _enviar_sync(client: Client, thread_id: str, texto: str) -> None:
    try:
        client.direct_messages.send(thread_id, texto)
    except Exception as e:
        log.error("Falha ao enviar DM Instagram: %s", e)
        raise


async def enviar_mensagem(
    username: str, password: str, sessionid: str, thread_id: str, texto: str
) -> None:
    chave = _chave(username, password, sessionid)
    async with _lock(chave):
        client = await _obter_cliente_locked(chave, username, password, sessionid)
        await asyncio.to_thread(_enviar_sync, client, thread_id, texto)
        await _salvar_sessao(username, client)

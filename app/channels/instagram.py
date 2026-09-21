from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from instagrapi import Client

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


def _login_sync(username: str, password: str, sessionid: str) -> Client:
    client = Client()
    client.delay_range = [1, 3]
    arquivo = _arquivo_sessao(username)
    if arquivo.exists():
        try:
            client.load_settings(arquivo)
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
    client.dump_settings(arquivo)
    return client


async def _com_lock(chave: str, func, *args):
    lock = _locks.setdefault(chave, asyncio.Lock())
    async with lock:
        return await asyncio.to_thread(func, *args)


def _cliente_sync(username: str, password: str, sessionid: str) -> Client:
    """Cliente logado SEM adquirir lock (uso interno dentro de _com_lock)."""
    chave = _chave(username, password, sessionid)
    if chave in _clientes:
        return _clientes[chave]
    _clientes[chave] = _login_sync(username, password, sessionid)
    return _clientes[chave]


async def obter_cliente(username: str, password: str = "", sessionid: str = "") -> Client:
    """Garante um cliente logado (com cache em disco)."""
    chave = _chave(username, password, sessionid)
    return await _com_lock(chave, _cliente_sync, username, password, sessionid)


async def coletar_novas(
    username: str, password: str, sessionid: str, ja_vistos: dict
) -> tuple[list[tuple[str, str, str]], dict]:
    """Retorna (mensagens novas, vistos atualizado).
    (thread_id, msg_id, texto) - apenas mensagens de outras pessoas."""
    def _coletar() -> tuple[list[tuple[str, str, str]], dict]:
        client = _cliente_sync(username, password, sessionid)
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

    return await _com_lock(_chave(username, password, sessionid), _coletar)


async def enviar_mensagem(
    username: str, password: str, sessionid: str, thread_id: str, texto: str
) -> None:
    def _enviar() -> None:
        client = _cliente_sync(username, password, sessionid)
        try:
            client.direct_messages.send(thread_id, texto)
        except Exception as e:
            log.error("Falha ao enviar DM Instagram: %s", e)
            raise

    await _com_lock(_chave(username, password, sessionid), _enviar)
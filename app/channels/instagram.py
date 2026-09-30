from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

from instagrapi import Client

from app import config, repositories as repo

settings = config.settings

log = logging.getLogger("instagram")

# Fora de app/ de propósito: o Render monta o app em disco efêmero, então a
# sessão é reautenticada pelo sessionid guardado no canal a cada deploy.
# O nome do arquivo é o hash do sessionid e o diretório é ignorado pelo Git.
DATA_DIR = Path(os.getenv("IG_DATA_DIR") or (Path(__file__).resolve().parent.parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Clientes autenticados por usuário, com lock por usuário (instagrapi é síncrono).
_clientes: dict[str, Client] = {}
_locks: dict[str, asyncio.Lock] = {}

# ---------------------------------------------------------------------------
# Canal Instagram — método NÃO OFICIAL (instagrapi / API privada)
#
# ⚠️ RISCO: viola os Termos de Uso do Instagram/Meta, usa endpoints privados
# que mudam sem aviso e pode BANIR a conta a qualquer momento. Não há SLA.
# Para produção, use o canal oficial `instagram_oficial` (Instagram Messaging
# API da Meta), que tem webhook, é estável e não arrisca a conta.
#
# ATIVAÇÃO: apenas por `sessionid`. O login por senha foi REMOVIDO de propósito:
# desde 2026 o Instagram exige atestação de app (Play Integrity / keystore) no
# `accounts/login/`, que um cliente Python puro não consegue produzir, e
# responde "Your version of Instagram is out of date" para qualquer
# app_version/version_code. Verificar o problema 1 do README deste arquivo:
# não existe versão do instagrapi que contorne isso.
#
# Como obter o sessionid:
#   1. No Chrome/Edge, abra https://www.instagram.com e logue normalmente.
#   2. F12 -> aba "Application" (Chrome) ou "Storage" (Edge)
#      -> Cookies -> https://www.instagram.com -> campo "sessionid".
#   3. Copie o valor e cole no campo "sessionid" do canal.
# O cookie expira; quando estourar, repita o passo 2.
# ---------------------------------------------------------------------------


def _chave(sessionid: str) -> str:
    """Chave de cache/lock: deriva só do sessionid, sem guardar o valor cru."""
    marca = hashlib.sha1(sessionid.encode()).hexdigest()[:8]
    return f"sessionid:{marca}"


def _arquivo_sessao(sessionid: str) -> Path:
    # Nome do arquivo usa o hash, nunca o sessionid cru (que é segredo).
    marca = hashlib.sha1(sessionid.encode()).hexdigest()[:8]
    return DATA_DIR / f"ig_session_{marca}.json"


def _login_sync(sessionid: str, dados_banco: dict | None = None) -> Client:
    if not sessionid:
        raise ValueError(
            "Instagram nao-oficial exige 'sessionid'. "
            "O login por senha foi desativado pelo Instagram (atestacao de app). "
            "Extraia o cookie sessionid de um navegador ja logado."
        )
    client = Client()
    client.delay_range = [1, 3]
    arquivo = _arquivo_sessao(sessionid)
    restaurada = False
    # A sessão vive no Postgres (o disco do Render é efêmero). Sem isto, cada
    # cold start refazia login_by_sessionid e o Instagram acabava respondendo
    # "out of date" / bloqueando a conta.
    if dados_banco:
        try:
            client.set_settings(dados_banco)
            restaurada = True
        except Exception as e:
            log.warning("Sessão do Instagram no banco inválida, usando o disco: %s", e)
    if not restaurada and arquivo.exists():
        try:
            client.load_settings(arquivo)
        except Exception as e:
            log.warning("Sessão Instagram inválida, relogando: %s", e)
    try:
        client.login_by_sessionid(sessionid)
    except Exception as e:
        msg = str(e)
        if "out of date" in msg.lower() or "upgrade your app" in msg.lower():
            log.error(
                "Instagram recusou o sessionid com 'out of date'. O cookie "
                "expirou ou foi revogado: extraia um novo sessionid de um "
                "navegador ja logado."
            )
        else:
            log.error("Falha ao autenticar Instagram por sessionid: %s", e)
        raise
    client.dump_settings(arquivo)
    return client


async def _salvar_sessao(chave: str, client: Client) -> None:
    """Persiste a sessão no Postgres (fonte da verdade) e no disco (cache)."""
    dados = client.settings
    if settings.has_db:
        try:
            await repo.salvar_sessao_instagram(chave, dados)
        except Exception as e:
            log.warning("Não consegui salvar a sessão do Instagram no banco: %s", e)
    try:
        client.dump_settings(DATA_DIR / f"ig_session_{chave.split(':', 1)[-1]}.json")
    except Exception as e:
        log.warning("Não consegui salvar a sessão do Instagram em disco: %s", e)


async def _obter_cliente_locked(chave: str, sessionid: str) -> Client:
    """Cliente logado, com sessão restaurada do Postgres. Exige o lock adquirido."""
    if chave in _clientes:
        return _clientes[chave]
    dados = None
    if settings.has_db:
        try:
            dados = await repo.obter_sessao_instagram(chave)
        except Exception as e:
            log.warning("Falha ao ler a sessão do Instagram do banco: %s", e)
    client = await asyncio.to_thread(_login_sync, sessionid, dados)
    _clientes[chave] = client
    await _salvar_sessao(chave, client)
    return client


async def obter_cliente(sessionid: str) -> Client:
    """Garante um cliente logado (cache em memória, sessão no Postgres)."""
    chave = _chave(sessionid)
    lock = _locks.setdefault(chave, asyncio.Lock())
    async with lock:
        return await _obter_cliente_locked(chave, sessionid)


def _thread_key(thread) -> str:
    """ID numerico da thread usado por approve/send.

    DirectThread.pk e o thread_v2_id (ex.: 17898572618026348) e e o que
    direct_pending_approve/direct_send convertem com int(). DirectThread.id e o
    thread_id legado e NAO serve para responder. DirectThread nao possui
    atributo thread_id.
    """
    return str(getattr(thread, "pk", "") or getattr(thread, "id", "") or "")


async def coletar_novas(
    sessionid: str, ja_vistos: dict
) -> tuple[list[tuple[str, str, str]], dict]:
    """Retorna (mensagens novas, vistos atualizado).
    (thread_id, msg_id, texto) - apenas mensagens de outras pessoas."""
    chave = _chave(sessionid)
    lock = _locks.setdefault(chave, asyncio.Lock())
    async with lock:
        client = await _obter_cliente_locked(chave, sessionid)
        novos, vistos = await asyncio.to_thread(_coletar_sync, client, ja_vistos)
        # Navegar renova cookies: só agora vale persistir de novo.
        await _salvar_sessao(chave, client)
        return novos, vistos


def _coletar_sync(client: Client, ja_vistos: dict) -> tuple[list[tuple[str, str, str]], dict]:
    vistos = dict(ja_vistos or {})
    novos: list[tuple[str, str, str]] = []

    def _registrar(thread) -> None:
        # `vistos` e a fonte de verdade da deduplicacao, entao a mesma
        # thread vista na caixa de pendentes e depois na geral nao gera
        # mensagem repetida.
        thread_id = _thread_key(thread)
        if not thread_id:
            return
        processados = set(vistos.get(thread_id, []))
        for msg in reversed(thread.messages or []):
            if getattr(msg, "is_sent_by_viewer", False):
                continue
            if str(msg.user_id) == str(client.user_id):
                continue
            if not msg.text:
                continue
            if msg.id in processados:
                continue
            processados.add(msg.id)
            novos.append((thread_id, str(msg.id), msg.text))
        vistos[thread_id] = list(processados)

    # 1) Pedidos de contato: aprovar imediatamente.
    #    Os métodos são do próprio Client (`client.direct_*`). Não existe um
    #    namespace `client.direct_messages`: esse nome é o método que lê as
    #    mensagens de UMA thread (client.direct_messages(thread_id)).
    try:
        pendentes = client.direct_pending_inbox(amount=20)
    except Exception as e:
        log.warning("direct_pending_inbox falhou: %s", e)
        pendentes = []
    for thread in pendentes:
        thread_id = _thread_key(thread)
        try:
            ok = client.direct_pending_approve(int(thread_id))
            log.info("Pedido de contato %s aprovado=%s", thread_id, ok)
        except Exception as e:
            log.warning("Falha ao aprovar pedido de contato %s: %s", thread_id, e)
        # A mensagem do solicitante e registrada de todo modo, mesmo se a
        # aprovacao falhar, para nao perder o primeiro contato.
        _registrar(thread)

    # 2) Caixa principal, que passa a conter as threads recem-aprovadas.
    try:
        threads = client.direct_threads(amount=20, thread_message_limit=20)
    except Exception as e:
        log.warning("direct_threads falhou: %s", e)
        threads = []
    for thread in threads:
        _registrar(thread)

    return novos, vistos


def _enviar_sync(client: Client, thread_id: str, texto: str) -> None:
    try:
        client.direct_send(texto, thread_ids=[int(thread_id)])
    except Exception as e:
        log.error("Falha ao enviar DM Instagram: %s", e)
        raise


async def enviar_mensagem(sessionid: str, thread_id: str, texto: str) -> None:
    chave = _chave(sessionid)
    lock = _locks.setdefault(chave, asyncio.Lock())
    async with lock:
        client = await _obter_cliente_locked(chave, sessionid)
        await asyncio.to_thread(_enviar_sync, client, thread_id, texto)
        await _salvar_sessao(chave, client)
"""Testes dos canais: validacao de credenciais, redaction e tarefas de fundo.

Roda sem banco e sem rede:

    python -m pytest tests/test_app.py -q
    python tests/test_app.py          # tambem funciona, sem pytest
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import repositories as repo  # noqa: E402
from app.channels import meta_oficial  # noqa: E402
from app.main import (  # noqa: E402
    TIPOS_CANAL_OFICIAL,
    _reconciliar_tarefas_de_fundo,
    _url_webhook_meta,
    _validar_config_canal,
)
import app.main as main_mod  # noqa: E402

falhas: list[str] = []


def check(nome: str, cond: bool, detalhe: str = "") -> None:
    print(("  ok   " if cond else "  FALHA ") + nome + (f" -> {detalhe}" if detalhe else ""))
    if not cond:
        falhas.append(nome)


def espera_erro(nome: str, tipo: str, config: dict, trecho: str) -> None:
    from fastapi import HTTPException

    try:
        _validar_config_canal(tipo, config)
    except HTTPException as e:
        check(nome, trecho in str(e.detail), f"HTTP {e.status_code}: {e.detail[:70]}")
    else:
        check(nome, False, "deveria ter recusado, mas aceitou")


# --------------------------------------------------------------------------
print("\n== canais oficiais exigem credenciais ==")
wa = _validar_config_canal("whatsapp_oficial", {
    "phone_number_id": "1234567890", "access_token": "EAAG",
    "verify_token": "vt1", "app_secret": "sec",
})
check("whatsapp oficial guarda as 5 chaves", set(wa) == {
    "phone_number_id", "access_token", "verify_token", "app_secret", "webhook_url"}, str(set(wa)))
ig = _validar_config_canal("instagram_oficial", {
    "ig_user_id": "17841400", "access_token": "EAAG",
    "verify_token": "vt1", "app_secret": "sec",
})
check("instagram oficial guarda as 5 chaves", set(ig) == {
    "ig_user_id", "access_token", "verify_token", "app_secret", "webhook_url"}, str(set(ig)))

espera_erro("whatsapp oficial exige phone_number_id", "whatsapp_oficial",
            {"access_token": "a", "verify_token": "v", "app_secret": "s"}, "phone_number_id")
espera_erro("whatsapp oficial exige access_token", "whatsapp_oficial",
            {"phone_number_id": "1", "verify_token": "v", "app_secret": "s"}, "access_token")
espera_erro("whatsapp oficial exige verify_token", "whatsapp_oficial",
            {"phone_number_id": "1", "access_token": "a", "app_secret": "s"}, "verify_token")
espera_erro("instagram oficial exige ig_user_id", "instagram_oficial",
            {"access_token": "a", "verify_token": "v", "app_secret": "s"}, "ig_user_id")
# app_secret e obrigatorio: sem ele nao da para validar X-Hub-Signature-256 e
# qualquer um poderia forjar mensagem no canal.
espera_erro("whatsapp oficial exige app_secret", "whatsapp_oficial",
            {"phone_number_id": "1", "access_token": "a", "verify_token": "v"}, "app_secret")
espera_erro("instagram oficial exige app_secret", "instagram_oficial",
            {"ig_user_id": "1", "access_token": "a", "verify_token": "v"}, "app_secret")

# --------------------------------------------------------------------------
print("\n== instagram nao-oficial so aceita sessionid ==")
ig_nao_oficial = _validar_config_canal("instagram", {"sessionid": "abc123"})
check("sessionid guardada", ig_nao_oficial.get("sessionid") == "abc123")
check("password nao e aceito como credencial", "password" not in ig_nao_oficial)
espera_erro("instagram sem sessionid e recusado", "instagram", {}, "sessionid")

# --------------------------------------------------------------------------
print("\n== segredo nenhum volta para o navegador ==")
redigido = repo._redigir_config({
    "token": "TG", "access_token": "META", "meta_access_token": "META2",
    "app_secret": "SEC", "verify_token": "VT-SECRETO", "senha": "P",
    "sessionid": "SID", "secret": "WH", "qr": "QR", "ig_vistos": {"1": ["m"]},
    "phone_number_id": "123", "ig_user_id": "456", "instance_name": "ag1",
})
for chave in ("token", "access_token", "meta_access_token", "app_secret", "verify_token",
              "senha", "sessionid", "secret", "qr", "ig_vistos"):
    check(f"{chave} redigido", redigido.get(chave) == "********", str(redigido.get(chave)))
for chave in ("phone_number_id", "ig_user_id", "instance_name"):
    check(f"{chave} continua visivel", redigido.get(chave) not in (None, "********"))

# --------------------------------------------------------------------------
print("\n== a URL do webhook oficial nao carrega segredo ==")
url_wa, url_ig = _url_webhook_meta("whatsapp_oficial"), _url_webhook_meta("instagram_oficial")
check("rota do whatsapp", url_wa.endswith("/meta/whatsapp"), url_wa)
check("rota do instagram", url_ig.endswith("/meta/instagram"), url_ig)
for termo in ("token", "secret", "sig"):
    check(f"URL do whatsapp sem '{termo}'", termo not in url_wa.lower())
    check(f"URL do instagram sem '{termo}'", termo not in url_ig.lower())

# --------------------------------------------------------------------------
print("\n== assinatura HMAC fecha por padrao ==")
import hashlib  # noqa: E402
import hmac  # noqa: E402

corpo = b'{"object":"instagram"}'
segredo = "segredo"
cab = "sha256=" + hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()
check("assinatura valida aceita", meta_oficial.assinatura_valida(corpo, cab, segredo))
check("corpo adulterado rejeita", not meta_oficial.assinatura_valida(b"{}", cab, segredo))
check("segredo errado rejeita", not meta_oficial.assinatura_valida(corpo, cab, "outro"))
check("sem cabecalho rejeita", not meta_oficial.assinatura_valida(corpo, None, segredo))
check("sem app_secret rejeita (em vez de aceitar)", not meta_oficial.assinatura_valida(corpo, None, ""))

cfg_vt = {"verify_token": "vt-secreto"}
check("handshake aceita token certo",
      meta_oficial.conferir_verificacao(cfg_vt, "subscribe", "vt-secreto", "123"))
check("handshake recusa token errado",
      not meta_oficial.conferir_verificacao(cfg_vt, "subscribe", "outro", "123"))
check("canal sem verify_token recusa handshake",
      not meta_oficial.conferir_verificacao({}, "subscribe", "vt", "123"))

# --------------------------------------------------------------------------
print("\n== tarefas de fundo seguem os canais ativos ==")

_canais: dict[int, dict] = {}


async def _listar_falso(agente_id=None, *, redigir=False):
    return [dict(c) for c in _canais.values()]


repo.listar_canais = _listar_falso
main_mod.repo = repo


def vivo(t) -> bool:
    return t is not None and not t.done()


async def _tarefas() -> None:
    await _reconciliar_tarefas_de_fundo()
    check("sem canal nao-oficial nenhuma tarefa liga",
          not vivo(main_mod._poller_task) and not vivo(main_mod._keepalive_task)
          and not vivo(main_mod._sync_task))

    _canais[1] = {"id": 1, "tipo": "instagram", "ativo": True, "nome": "ig", "config": {}}
    await _reconciliar_tarefas_de_fundo()
    check("criou instagram ativo: poller liga na hora", vivo(main_mod._poller_task))
    check("whatsapp segue desligado", not vivo(main_mod._keepalive_task))

    _canais[2] = {"id": 2, "tipo": "whatsapp", "ativo": True, "nome": "wa",
                  "config": {"instance_name": "a"}}
    await _reconciliar_tarefas_de_fundo()
    check("criou Evolution ativo: keepalive e sync ligam",
          vivo(main_mod._keepalive_task) and vivo(main_mod._sync_task))

    p, k, s = main_mod._poller_task, main_mod._keepalive_task, main_mod._sync_task
    await _reconciliar_tarefas_de_fundo()
    check("reconciliar de novo nao duplica tarefa",
          main_mod._poller_task is p and main_mod._keepalive_task is k
          and main_mod._sync_task is s)

    _canais[1]["ativo"] = False
    await _reconciliar_tarefas_de_fundo()
    check("pausou instagram: poller desliga", not vivo(main_mod._poller_task))
    check("evolution ativa mantem keepalive", vivo(main_mod._keepalive_task))

    del _canais[2]
    await _reconciliar_tarefas_de_fundo()
    check("removeu Evolution: keepalive e sync desligam",
          not vivo(main_mod._keepalive_task) and not vivo(main_mod._sync_task))

    _canais.clear()
    _canais[3] = {"id": 3, "tipo": "whatsapp_oficial", "ativo": True, "nome": "wa", "config": {}}
    _canais[4] = {"id": 4, "tipo": "instagram_oficial", "ativo": True, "nome": "ig", "config": {}}
    await _reconciliar_tarefas_de_fundo()
    check("so canais oficiais: nada de fundo, o Render pode hibernar",
          not vivo(main_mod._poller_task) and not vivo(main_mod._keepalive_task)
          and not vivo(main_mod._sync_task))

    for t in (main_mod._poller_task, main_mod._keepalive_task, main_mod._sync_task):
        if vivo(t):
            t.cancel()


asyncio.run(_tarefas())

# --------------------------------------------------------------------------
print("\n== painel: o form de Instagram nao pede mais usuario/senha ==")
html = (ROOT / "index.html").read_text(encoding="utf-8")

# O login por senha foi desativado pelo Instagram. Se sobrar referencia a
# usuario/senha na validacao do botao "Criar canal", o usuario fica preso
# preenchendo um campo que nem existe mais na tela.
for obsoleto in ("config.usuario", "config.senha", 'data-config="usuario"', 'data-config="senha"'):
    check(f"sem referencia a '{obsoleto}'", obsoleto not in html)

check("validacao do Instagram exige sessionid",
      'tipo === "instagram" && !config.sessionid' in html)
bloco = html[html.index('if (tipo === "instagram") return `') + 30:]
bloco = bloco[:bloco.index("`;")]
check("formulario do Instagram so tem o campo sessionid",
      'data-config="sessionid"' in bloco and "data-config=" not in bloco.replace(
          'data-config="sessionid"', ""),
      bloco.strip()[:80])

# --------------------------------------------------------------------------
print("\n== resumo ==")
if falhas:
    print(f"FALHAS ({len(falhas)}): " + ", ".join(falhas))
    sys.exit(1)
print("TODOS OS TESTES PASSARAM")


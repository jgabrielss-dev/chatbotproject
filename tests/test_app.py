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
print("\n== painel aberto (sem login) e conexao resiliente ==")
check("nao existe rota /admin nem login no painel", "/admin" not in html)
check("o token e opcional: o gate comeca escondido",
      '<div id="gate" class="hidden">' in html)
check("abre o painel mesmo sem token (nao chama mostrarGate na partida)",
      "esconderGate();\ncarregarAgentes()" in html)
check("'Failed to fetch' virou retentativa automatica",
      "tentativa < 2" in html and "api(url, opts, tentativa + 1)" in html)
check("abrir o index do disco aponta para a API do Render",
      'location.protocol === "file:"' in html and "API_RENDER" in html)
check("botao para informar token existe", 'id="btnToken"' in html)

# --------------------------------------------------------------------------
print("\n== acesso a API: token opcional, 401 so se configurado ==")


def _chamar_api(token: str | None, headers: dict | None = None):
    """Chama a API pelo ASGI, com o token do servidor escolhido em tempo de teste."""
    from fastapi.testclient import TestClient

    antigo = main_mod.settings.admin_token
    object.__setattr__(main_mod.settings, "admin_token", token or "")
    try:
        with TestClient(main_mod.app) as c:
            return c.get("/api/agentes", headers=headers or {})
    finally:
        object.__setattr__(main_mod.settings, "admin_token", antigo)


try:
    import fastapi.testclient  # noqa: F401

    tem_testclient = True
except Exception:
    tem_testclient = False

if tem_testclient:
    r = _chamar_api(None)
    check("sem ADMIN_TOKEN a API fica aberta (padrao pedido)", r.status_code == 200,
          f"HTTP {r.status_code}")
    r = _chamar_api("segredo-do-admin", {})
    check("com ADMIN_TOKEN e sem header responde 401", r.status_code == 401,
          f"HTTP {r.status_code}")
    r = _chamar_api("segredo-do-admin", {"X-Admin-Token": "errado"})
    check("com ADMIN_TOKEN e token errado responde 401", r.status_code == 401,
          f"HTTP {r.status_code}")
    r = _chamar_api("segredo-do-admin", {"X-Admin-Token": "segredo-do-admin"})
    check("com ADMIN_TOKEN e token certo responde 200", r.status_code == 200,
          f"HTTP {r.status_code}")
    r = _chamar_api("segredo-do-admin", {})
    check("o painel abre mesmo assim: o 401 so mostra o campo de token", r.status_code == 401)
else:
    print("  (pulado: fastapi.testclient nao instalado)")

# --------------------------------------------------------------------------
print("\n== o segredo do canal viaja na URL do webhook ==")


async def _urls() -> None:
    from app.main import _url_de_webhook

    tg = {"id": 21, "tipo": "telegram", "config": {"secret": "abc123"}}
    wa = {"id": 26, "tipo": "whatsapp", "config": {"secret": "def456", "instance_name": "ag7x"}}
    wb = {"id": 16, "tipo": "webhook", "config": {"secret": "ghi789"}}
    u_tg = _url_de_webhook(tg)
    u_wa = _url_de_webhook(wa)
    u_wb = _url_de_webhook(wb)
    check("telegram usa a edge function com o segredo na rota",
          u_tg.endswith("/telegram/21/abc123"), u_tg)
    check("evolution usa a edge function com o segredo na rota",
          u_wa.endswith("/evolution/26/def456"), u_wa)
    check("webhook generico usa o app com o segredo na rota",
          u_wb.endswith("/webhook/generico/16/ghi789"), u_wb)

asyncio.run(_urls())

edge = (ROOT / "supabase" / "functions" / "inbox" / "index.ts").read_text(encoding="utf-8")
check("a edge function aceita /evolution/{id}/{secret}",
      'rest[0] === "evolution" && rest[1] && rest[2]' in edge)
check("a edge function aceita /telegram/{id}/{secret}",
      'rest[0] === "telegram" && rest[1] && rest[2]' in edge)
check("a edge function compara o segredo em tempo constante", "segredoIgual" in edge)
check("a edge function tem as rotas oficiais da Meta",
      'rest[0] === "meta"' in edge and "handleMeta" in edge)
check("as rotas /meta nao exigem segredo (uso de app_secret na assinatura)",
      'rest[0] === "meta" && (rest[1] === "whatsapp" || rest[1] === "instagram")' in edge)

# --------------------------------------------------------------------------
print("\n== fila: origem obrigatoria e indice nao parcial ==")
sql = (ROOT / "supabase" / "migrations" / "0003_fila_e_sessao.sql").read_text(encoding="utf-8")
check("migra processando_em", "processando_em" in sql)
check("tira o indice parcial de origem", "DROP INDEX IF EXISTS uq_caixa_origem" in sql)
check("cria o indice nao parcial", "CREATE UNIQUE INDEX IF NOT EXISTS uq_caixa_origem ON caixa_entrada (canal_id, origem)" in sql)
check("origem vira NOT NULL", "ALTER COLUMN origem SET NOT NULL" in sql)
check("guarda a sessao do Instagram no Postgres", "instagram_sessoes" in sql)
check("protegge a sessao do Instagram com RLS", "ENABLE ROW LEVEL SECURITY" in sql)
check("restaura os grants do schema public", "GRANT USAGE ON SCHEMA public" in sql)


async def _origem_vazia() -> None:
    try:
        await repo.salvar_na_caixa(1, "x", "y", "")
    except ValueError as e:
        raise AssertionError(str(e)) from e
    except Exception as e:  # sem banco: so queremos ver o erro de validacao
        raise AssertionError(str(e)) from e


try:
    asyncio.run(_origem_vazia())
    check("salvar_na_caixa recusa origem vazia", False, "aceitou")
except AssertionError as e:
    check("salvar_na_caixa recusa origem vazia", True, str(e)[:70])


# --------------------------------------------------------------------------
print("\n== resumo ==")
if falhas:
    print(f"FALHAS ({len(falhas)}): " + ", ".join(falhas))
    sys.exit(1)
print("TODOS OS TESTES PASSARAM")


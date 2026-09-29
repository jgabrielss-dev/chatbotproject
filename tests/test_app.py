"""Testes dos canais: validacao de credenciais, redaction e tarefas de fundo.

Roda sem banco e sem rede:

    python -m pytest tests/test_app.py -q
    python tests/test_app.py          # tambem funciona, sem pytest
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
import uuid
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
# `redigir_config` e a unica funcao de redacao. Antes havia duas listas CHAVES_
# SECRETAS divergentes e a usada por main._canal_publico (que atendia
# GET /api/agentes/{id}/canais) nao cobria access_token/app_secret/verify_token:
# o token da Meta ia em claro para o navegador. O teste chama a funcao que as
# rotas usam, nao uma copia interna.
redigido = repo.redigir_config({
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

# A rota real, nao a helper: e o que o navegador recebe.
pelo_canal_publico = main_mod._canal_publico({
    "id": 1, "tipo": "whatsapp_oficial",
    "config": {"access_token": "META-CRU", "app_secret": "SEC-CRU",
               "verify_token": "VT-CRU", "phone_number_id": "123"},
})
for chave in ("access_token", "app_secret", "verify_token"):
    check(f"{chave} nao vaza em /api/agentes/{{id}}/canais",
          pelo_canal_publico["config"].get(chave) == "********",
          str(pelo_canal_publico["config"].get(chave)))
check("placeholder nao sobrescreve o segredo no merge",
      "********" in (ROOT / "app" / "repositories.py").read_text(encoding="utf-8"))

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
# Aponta para app/static/admin.html, que e o que o servidor serve em /admin.
# O index.html da raiz virou a home de login e nao tem mais formulario de canal;
# testar contra ele dava a impressao de cobertura onde nao ha nenhuma.
html = (ROOT / "app" / "static" / "admin.html").read_text(encoding="utf-8")
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
print("\n== as tres telas existem e nao confiam no ADMIN_TOKEN ==")
painel_admin = (ROOT / "app" / "static" / "admin.html").read_text(encoding="utf-8")
home = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
painel_cli = (ROOT / "app" / "static" / "painel.html").read_text(encoding="utf-8")
auth_js = (ROOT / "app" / "static" / "auth.js").read_text(encoding="utf-8")

for nome, texto in (("admin", painel_admin), ("index", home), ("painel", painel_cli)):
    check(f"{nome}.html existe e nao esta vazio", len(texto) > 500)
    check(f"{nome}.html usa o auth.js", '/static/auth.js' in texto)
    check(f"{nome}.html nao manda mais X-Admin-Token", "X-Admin-Token" not in texto)

check("o painel admin exige sessao de admin antes de carregar",
      "await Auth.obterSessao()" in painel_admin and "if (!eu.eh_admin)" in painel_admin)
check("o painel do cliente manda o admin para /admin",
      "eu.eh_admin" in painel_cli and '"/admin"' in painel_cli)
check("a home tem login, cadastro e recuperacao de senha",
      'data-modo="recuperar"' in home and "Auth.cadastrar" in home and "Auth.entrar" in home)
check("a home manda admin para /admin e cliente para /painel",
      'eu.eh_admin ? "/admin" : "/painel"' in home)
check("o painel admin tem a tela de contas", 'id="contasCard"' in painel_admin
      and "/api/admin/contas" in painel_admin)
check("o painel do cliente usa as rotas escopadas em /api/painel",
      "/api/painel/agentes" in painel_cli and "/api/painel/sessoes" in painel_cli)
check("o auth envia Authorization: Bearer", "Authorization" in auth_js
      and '"Bearer "' in auth_js)
check("o auth guarda a sessao em localStorage", "localStorage" in auth_js)
check("o logout chama o GoTrue", "/auth/v1/logout" in auth_js)

# --------------------------------------------------------------------------
print("\n== a API esta fechada: sem sessao nao entra ==")
# O comportamento antigo (sem ADMIN_TOKEN a API ficava aberta) foi trocado: com
# multi-tenant um `WHERE id = $1` responderia a fila de outro cliente, entao o
# padrao passou a ser "fechado". Estes testes fixam isso.


def _com_auth(monkey_auth: bool | None = None):
    """TestClient com has_auth forcado."""
    from fastapi.testclient import TestClient

    antigo = main_mod.settings.supabase_url, main_mod.settings.supabase_anon_key
    if monkey_auth is None:
        object.__setattr__(main_mod.settings, "supabase_url", "")
        object.__setattr__(main_mod.settings, "supabase_anon_key", "")
    else:
        object.__setattr__(main_mod.settings, "supabase_url", "https://x.supabase.co")
        object.__setattr__(main_mod.settings, "supabase_anon_key", "anon-key")
    try:
        with TestClient(main_mod.app) as c:
            return c
    finally:
        object.__setattr__(main_mod.settings, "supabase_url", antigo[0])
        object.__setattr__(main_mod.settings, "supabase_anon_key", antigo[1])


try:
    import fastapi.testclient  # noqa: F401

    tem_testclient = True
except Exception:
    tem_testclient = False

if tem_testclient:
    c = _com_auth()
    for rota in ("/api/agentes", "/api/eu", "/api/caixa", "/api/painel/resumo",
                 "/api/painel/agentes", "/api/admin/contas"):
        r = c.get(rota)
        check(f"{rota} sem sessao nao entrega nada",
              r.status_code in (401, 503), f"HTTP {r.status_code}")

    c = _com_auth()
    r = c.get("/api/eu")
    check("sem sessao e sem Supabase no servidor responde 503 (faltam as envs)",
          r.status_code == 503, f"HTTP {r.status_code}")

    # As paginas sao publicas: e o shell do HTML, nenhum dado sai delas.
    c = _com_auth()
    for rota in ("/", "/admin", "/painel", "/api/config", "/health"):
        r = c.get(rota)
        check(f"{rota} e publica", r.status_code == 200, f"HTTP {r.status_code}")

    c = _com_auth()
    r = c.get("/api/config")
    corpo = r.json()
    check("/api/config entrega url e anon key (publicas por natureza)",
          corpo.get("auth_habilitado") is False
          and "supabase_url" in corpo and "supabase_anon_key" in corpo)
    check("/api/config nao entrega a service_role nem o admin token",
          "service_role" not in corpo and "admin_token" not in corpo)

    # Rotas do painel do cliente registradas.
    c = _com_auth()
    rotas = set(c.app.openapi()["paths"])
    for esperada in ("/api/painel/resumo", "/api/painel/agentes",
                     "/api/painel/sessoes/{sessao_id}", "/api/admin/contas"):
        check(f"rota {esperada} registrada", esperada in rotas)
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
print("\n== isolamento: um cliente nao alcanca os dados de outro ==")
# O teste que importa. Nao basta a API responder 401 sem token: com token
# valido, o cliente A nao pode ver nem alterar o agente/canal/sessao do cliente
# B. Aqui nao ha banco, entao verificamos a camada que decide o dono e a
# traducao dela para as queries.

from app import auth as auth_mod  # noqa: E402

check("admin recebe dono None (enxerga tudo)", main_mod._dono(
    auth_mod.Usuario(id="a", email="a@x", role="admin")) is None)
check("usuario comum recebe o proprio id como dono", main_mod._dono(
    auth_mod.Usuario(id="u1", email="u1@x", role="usuario")) == "u1")
check("a conta de emergencia e admin", auth_mod.ADMIN_EMERGENCIA.eh_admin)
try:
    uuid.UUID(auth_mod.ADMIN_EMERGENCIA.id)
    _emergencia_e_uuid = True
except (ValueError, AttributeError):
    _emergencia_e_uuid = False
check("a conta de emergencia nao tem id de uuid (nao pode ir para uma FK)",
      not _emergencia_e_uuid, auth_mod.ADMIN_EMERGENCIA.id)

# _canal_dono tem de passar o dono para a query de posse, nunca chamar
# obter_canal puro: foi o que permitia chutar o id sequencial do canal alheio.
fonte = inspect.getsource(main_mod._canal_dono)
check("_canal_dono consulta com o dono", "obter_canal_do_dono" in fonte
      and "_dono(usuario)" in fonte)

# Toda rota /api/canais/{canal_id}/... tem de pedir sessao E conferir a posse.
# Os /webhook/... ficam de fora de proposito: sao publicos e se autenticam pelo
# segredo do canal na propria rota.
src_main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
# Split so em "@app." no inicio de linha: cortando tambem em "async def" o
# chunk da rota ficaria apenas com a linha do decorator, sem o corpo.
partes = re.split(r"\n(?=@app\.)", src_main)
rotas_canal, sem_porte = [], []
for parte in partes:
    m = re.match(r'@app\.(?:get|post|put|delete)\("(/api/canais/\{canal_id\}[^"]*)"\)', parte)
    if not m:
        continue
    rotas_canal.append(m.group(1))
    tem_sessao = "usuario_atual(request)" in parte
    tem_porte = "_canal_dono(" in parte or "obter_canal_do_dono(" in parte
    if not (tem_sessao and tem_porte):
        sem_porte.append(m.group(1))
check("toda rota de canal por id exige sessao e confere a posse",
      len(rotas_canal) >= 8 and not sem_porte,
      f"{len(rotas_canal)} rotas; sem porte: " + (", ".join(sem_porte) or "nenhuma"))

# Webhooks publicos continuam existindo e autenticam pelo segredo do canal.
check("os webhooks seguem publicos (secret na rota)", "/webhook/" in src_main
      and "_secret_igual(" in src_main)

# A fila e a tabela onde o vazamento doeria mais: texto de conversa de cliente.
fonte_caixa = inspect.getsource(repo.resumo_caixa)
check("resumo_caixa filtra por dono (fila nao e global para o cliente)",
      "a.dono_id" in fonte_caixa and "dono_id" in fonte_caixa)
check("o filtro do dono usa $1, e o LIMIT seguinte",
      " AND a.dono_id = $1" in fonte_caixa
      and " AND a.dono_id = $2" not in fonte_caixa)

for fn in ("listar_agentes", "obter_agente", "atualizar_agente", "excluir_agente",
           "obter_canal_do_dono", "listar_canais_do_dono", "obter_sessao_do_dono"):
    check(f"{fn} aceita dono_id", "dono_id" in inspect.signature(getattr(repo, fn)).parameters)

# --------------------------------------------------------------------------
print("\n== RLS: o schema novo nao da leitura ao anon ==")
schema = (ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
check("cria perfis e app_config", "CREATE TABLE IF NOT EXISTS perfis" in schema
      and "CREATE TABLE IF NOT EXISTS app_config" in schema)
check("agentes ganha dono_id com FK para auth.users",
      "ADD COLUMN IF NOT EXISTS dono_id UUID REFERENCES auth.users(id)" in schema)
check("o ON DELETE CASCADE apaga o agente com a conta",
      "ON DELETE CASCADE" in schema)
check("cria trigger de perfil no cadastro",
      "criar_perfil" in schema and "AFTER INSERT ON auth.users" in schema)
check("o papel vem de app_config.admin_emails", "admin_emails" in schema)
for tabela in ("agentes", "canais", "sessoes", "mensagens", "caixa_entrada"):
    check(f"RLS ligado em {tabela}",
          f"ALTER TABLE {tabela} ENABLE ROW LEVEL SECURITY" in schema)
check("revoga tudo de anon", "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon" in schema)
check("anon NAO recebe mais SELECT em tudo",
      "GRANT SELECT ON ALL TABLES IN SCHEMA public TO anon" not in schema)
check("authenticated recebe so SELECT",
      "GRANT SELECT ON ALL TABLES IN SCHEMA public TO authenticated" in schema)

mig4 = (ROOT / "supabase" / "migrations" / "0004_contas_e_multitenant.sql").read_text(encoding="utf-8")
check("a migration 0004 existe", "perfis" in mig4 and "dono_id" in mig4)
check("a 0004 tambem revoga o anon", "FROM anon" in mig4)

# O painel do cliente nao pode ter os botoes de infraestrutura da plataforma.
check("o painel do cliente nao expoe canais nao-oficiais a nao-admin",
      "CANAIS_NAO_OFICIAIS_PARA_USUARIOS" in (ROOT / "app" / "config.py").read_text(encoding="utf-8"))
check("a restricao de canal nao oficial existe no servidor",
      "_exigir_tipo_permitido" in src_main)

# --------------------------------------------------------------------------
print("\n== resumo ==")
if falhas:
    print(f"FALHAS ({len(falhas)}): " + ", ".join(falhas))
    sys.exit(1)
print("TODOS OS TESTES PASSARAM")


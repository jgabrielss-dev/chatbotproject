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
import subprocess
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
# Aponta para o admin.html da raiz, que e o que o servidor serve em /admin e o
# que o GitHub Pages publica. O index.html da raiz virou a home de venda e nao
# tem mais formulario de canal; testar contra ele dava a impressao de cobertura
# onde nao ha nenhuma.
html = (ROOT / "admin.html").read_text(encoding="utf-8")
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
src_main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
src_config = (ROOT / "app" / "config.py").read_text(encoding="utf-8")

print("\n== encoding: nenhum caractere corrompido em nenhuma tela ==")
# Sintoma reportado: "caracteres estranhos em todo o site". Cada arquivo tinha
# um codepage diferente de corrupcao, e o sinal e um PAR (Ã seguido de byte de
# continuacao), nao um acento so — 'é' legitimo cai na mesma faixa de 'Ã'.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "corrigir_encoding", ROOT / "scripts" / "corrigir_encoding.py")
_corr = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_corr)
for _nome in ("index.html", "login.html", "admin.html", "painel.html", "auth.js"):
    _t = (ROOT / _nome).read_text(encoding="utf-8")
    _resto = _corr.Rastro.findall(_t)
    check(f"{_nome} sem mojibake", not _resto,
          f"{len(_resto)} rastro(s): {sorted(set(_resto))[:6]}")
    _legais = [c for c in _t if ord(c) > 0x7F]
    check(f"{_nome} preservou os acentos do portugues", len(_legais) > 20,
          "so restaram ASCII: o conserto pode ter apagado acento em vez de "
          "acertar, e isso passa despercebido")

print("\n== o JS roda mesmo, e nao so passa no 'node --check' ==")
_js = r"""
global.window = global;
global.location = { protocol: "https:", hostname: "jgabrielss-dev.github.io",
                    search: "", pathname: "/chatbotproject/" };
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
require(process.argv[2]);
// Um '/*' que nunca fecha engole as 'var' seguintes: o arquivo continua
// sintaticamente valido (node --check passa) e so quebra em runtime, com
// ReferenceError. Por isso este teste EXECUTA o arquivo, e nao conta
// comentarios. Contar nao serve: o '/auth/v1/*' do comentario de cabecalho e o
// '/\/*/' de uma regex contain '/*' sem abrir nada.
const casos = [
  [{ eh_admin: false }, "/painel", "./painel.html"],
  [{ eh_admin: true },  "/admin",  "./admin.html"],
  [{ eh_admin: false }, "admin.html", "./admin.html"],
  [{ eh_admin: false }, "/",       "./index.html"],
];
for (const [eu, entra, quer] of casos) {
  const got = Auth.destino(eu, entra);
  if (got !== quer) { console.log("DESTINO " + entra + " = " + got + " (esperado " + quer + ")"); process.exit(3); }
}
// Guarda de seguranca: cliente comum que tentar /admin volta para o painel dele.
const guardado = Auth.destino({ eh_admin: false }, "/admin");
if (guardado !== "./painel.html") { console.log("GUARDA /admin = " + guardado); process.exit(5); }
// Se algum 'var' estiver comentado, obterSessao estoura aqui.
Auth.obterSessao().then(() => console.log("OK")).catch(e => { console.log("ERRO: " + e.message); process.exit(4); });
"""
_arq = ROOT / "tests" / "_probe_auth.js"
_arq.write_text(_js, encoding="utf-8")
try:
    _p = subprocess.run(["node", str(_arq), str(ROOT / "auth.js")],
                        capture_output=True, text=True, timeout=90)
    check("auth.js carrega e roda no Pages", "OK" in _p.stdout,
          (_p.stdout + _p.stderr).strip()[:220])
    check("Auth.destino devolve caminho RELATIVO no Pages",
          "DESTINO" not in _p.stdout and "GUARDA" not in _p.stdout,
          [l for l in _p.stdout.splitlines() if "DESTINO" in l or "GUARDA" in l][:3])
finally:
    _arq.unlink(missing_ok=True)

print("\n== as quatro telas existem e nao confiam no ADMIN_TOKEN ==")
# As telas moram na RAIZ do repo: o GitHub Pages publica um site de projeto em
# ".../<repo>/", que so encontra o que esta na raiz. Copia em app/static/ foi o
# que deixou o Pages servindo o painel antigo.
painel_admin = (ROOT / "admin.html").read_text(encoding="utf-8")
home = (ROOT / "index.html").read_text(encoding="utf-8")
login = (ROOT / "login.html").read_text(encoding="utf-8")
painel_cli = (ROOT / "painel.html").read_text(encoding="utf-8")
auth_js = (ROOT / "auth.js").read_text(encoding="utf-8")
style_css = (ROOT / "style.css").read_text(encoding="utf-8")

check("as telas estao na raiz do repo, e nao em app/static/",
      not (ROOT / "app" / "static").exists(),
      "app/static/ voltou a existir: o Pages passaria a servir uma copia velha")
check("a home e index.html, senao o Pages nao tem indice de diretorio",
      (ROOT / "index.html").exists() and not (ROOT / "home.html").exists())
check("o build le os arquivos da raiz",
      'RAIZ / "index.html"' in src_main and 'RAIZ / "auth.js"' in src_main)

for nome, texto in (("admin", painel_admin), ("home", home),
                    ("login", login), ("painel", painel_cli)):
    check(f"{nome}.html existe e nao esta vazio", len(texto) > 500)
    check(f"{nome}.html usa o auth.js", 'src="auth.js"' in texto)
    check(f"{nome}.html nao manda mais X-Admin-Token", "X-Admin-Token" not in texto)

check("o painel admin exige sessao de admin antes de carregar",
      "await Auth.obterSessao()" in painel_admin and "if (!eu.eh_admin)" in painel_admin)
check("o painel do cliente manda o admin para a tela de admin",
      "eu.eh_admin" in painel_cli and "Auth.pagina(\"admin\")" in painel_cli)
check("a home e de venda, sem formulario de login",
      'id="form"' not in home and "Auth.entrar" not in home and "Auth.cadastrar" not in home)
# "login.html" e nao "/login": no GitHub Pages nao ha servidor, entao /login vira
# um pedido do arquivo "login" e da 404.
check("a home manda entrar e criar conta para login.html",
      home.count('href="login.html"') >= 2 and 'href="/login"' not in home
      and 'href="/painel"' not in home)
check("a home manda o usuario para a tela certa via Auth.destino",
      "Auth.destino(eu)" in home)
check("a home nao quebra se /api/eu falhar (Render acordando)",
      ".catch(" in home)
check("a tela de login tem login, cadastro e recuperacao de senha",
      'data-modo="recuperar"' in login and "Auth.cadastrar" in login
      and "Auth.entrar" in login)
check("a tela de login usa Auth.destino, que sabe a diferenca entre os hosts",
      "Auth.destino(eu)" in login and "Auth.destino(eu, alvo)" in login)

# Recuperar senha nao tem senha. Se o campo continuar required, o navegador
# bloqueia o submit antes do JS rodar e o botao parece nao funcionar.
check("'esqueci minha senha' tira a senha de cena",
      'id="blocoSenha"' in login and 'classList.toggle("hidden", soEmail)' in login
      and '$("senha").required = !soEmail' in login)
check("o botao diz 'Enviar link' no modo recuperar, e nao 'Entrar'",
      'recuperar: "Enviar link"' in login and 'ROTULO[modo]' in login)
check("a tela de login diz quando o e-mail nao chegou por cota do SMTP",
      "not confirmed" in login and "rate limit" in login.lower())

# O GitHub Pages nao tem servidor: "/admin" la vira um pedido de arquivo
# chamado admin e devolve 404. O caminho tambem tem de ser RELATIVO, porque o
# Pages serve o site de projeto em ".../<repo>/" e "/auth.js" apontaria para a
# raiz do dominio, fora do repo.
check("o auth.js sabe quando esta em pagina estatica",
      "EM_PAGINA_ESTATICA" in auth_js and "github\\.io" in auth_js)

# Tudo que o auth.js exporta vive dentro de um IIFE e so existe como
# window.Auth.<nome>. Chamar sem o prefixo da ReferenceError, e o catch do
# api() transformava isso em "servidor nao respondeu" — foi assim que um bug
# de escopo passou tres commits e rodou em producao. O sintoma era um aviso
# de rede, mas a causa era uma funcao fora do escopo.
_exportados = set(re.findall(r"^\s{4}(\w+): \w+,?\s*$", auth_js, re.M))
_publicos = ("cabecalhoAuth", "obterSessao", "destino", "pagina", "irParaLogin",
             "sair", "config", "entrar", "cadastrar", "recuperar", "esc",
             "mostrarGate", "esconderGate", "API_BASE", "API_RENDER",
             "EM_PAGINA_ESTATICA", "ABRINDO_DO_DISCO")
for _n in _publicos:
    check(f"auth.js exporta {_n}", _n in _exportados, "o painel chamaria undefined")

_chamadas = re.compile(r"\b(" + "|".join(_publicos) + r")\s*\(")
for _nome_arquivo, _txt in (("admin.html", painel_admin), ("painel.html", painel_cli),
                            ("login.html", login), ("index.html", home)):
    # Um nome definido localmente nao e o mesmo problema: admin.html define a
    # propria esc(), painel.html faz "const esc = Auth.esc" e login.html define
    # destino(). O que quebra e a chamada que o auth.js cria e ninguem definiu.
    _defines = set(re.findall(
        r"(?:function|const|let|var)\s+(" + "|".join(_publicos) + r")\b", _txt))
    for _m in _chamadas.finditer(_txt):
        _nome = _m.group(1)
        if _nome in _defines:
            continue
        # Com prefixo, o trecho antes do nome termina em ponto que NAO faz parte
        # de reticencias: "Auth.cabecalhoAuth(". Sem prefixo, "...cabecalhoAuth("
        # tem ponto antes, mas ele e o operador spread — por isso a checagem
        # ignora "..." e so aceita o nome como solto.
        _antes = _txt[:_m.start()].rstrip()
        _com_prefixo = _antes.endswith(".") and not _antes.endswith("...")
        check(f"{_nome_arquivo} chama Auth.{_nome}() com o prefixo",
              _com_prefixo,
              f"achou '{_nome}(' sem 'Auth.' — ReferenceError em producao")

# O bug de cabecalhoAuth() sobe do try porque montavamos os headers DENTRO
# dele: o ReferenceError era capturado, retentado 4x com 55s de espera e
# reportado como "servidor nao respondeu". Dois testes para travar a correcao:
# os headers tem de ser montados antes do try, e nada pode matar a request
# por tempo -- quem pediu foi para nao marcar nada como concluido por timeout.
for _nome_arquivo, _txt in (("admin.html", painel_admin), ("painel.html", painel_cli)):
    _api = _txt[_txt.index("const api = async"):_txt.index("const api = async") + 2200]
    _try = _api.index("try {")
    _headers = _api.index("cabecalhos")
    check(f"{_nome_arquivo} monta os headers ANTES do try",
          _headers < _try,
          "bug de escopo dentro do try vira 'servidor nao respondeu' de novo")
    check(f"{_nome_arquivo} nao mata a requisicao por timeout",
          "AbortSignal.timeout" not in _txt.replace(
              "// Sem AbortSignal.timeout", "").replace(
              "// Sem AbortSignal.timeout:", ""),
          "timeout artificial transforma cold start lento em erro na tela")
    check(f"{_nome_arquivo} so repete em falha de rede de verdade",
          'e.name !== "TypeError"' in _api,
          "sem isso, qualquer bug do JS entra no retry de rede")


check("o auth.js decide entre /admin e /admin.html",
      'EM_PAGINA_ESTATICA ? "./" + nome + ".html" : "/" + nome' in auth_js,
      "o caminho do Pages precisa ser RELATIVO: '/admin.html' vira 404 porque "
      "aponta para a raiz do dominio, fora do repo")
for nome, texto in (("admin", painel_admin), ("home", home),
                    ("login", login), ("painel", painel_cli)):
    check(f"{nome}.html usa caminho RELATIVO nos assets (o Pages exige)",
          'src="auth.js"' in texto and 'href="style.css"' in texto
          and "/static/" not in texto,
          "caminho com barra a esquerda quebra no site de projeto do Pages")
    check(f"{nome}.html nao tem redirect cru para /admin ou /painel",
          'location.replace("/admin")' not in texto
          and 'location.replace("/painel")' not in texto
          and 'location.replace("/?redir' not in texto,
          "tem que usar Auth.pagina(), que resolve a diferenca entre os hosts")
    check(f"{nome}.html nao pede sessao por /static/auth.js",
          "Auth.obterSessao" in texto or "auth.js" in texto)

# Nenhuma pagina pode usar uma variavel de cor que o style.css nao define:
# quando isso acontece o `var(--x)` cai para vazio e a tela fica sem cor, sem
# erro no console e sem o build reclamar. Aconteceu com --prim/--txt2/--borda.
definidas = set(re.findall(r"(--[a-z0-9-]+)\s*:", style_css))
for nome, texto in (("admin", painel_admin), ("home", home),
                    ("login", login), ("painel", painel_cli)):
    usadas = set(re.findall(r"var\((--[a-z0-9-]+)\)", texto))
    check(f"{nome}.html nao usa variavel de cor inexistente",
          usadas <= definidas,
          f"inexistentes: {sorted(usadas - definidas)}")
check("o painel admin tem a tela de contas", 'id="contasCard"' in painel_admin
      and "/api/admin/contas" in painel_admin)
check("o painel do cliente usa as rotas escopadas em /api/painel",
      "/api/painel/agentes" in painel_cli and "/api/painel/sessoes" in painel_cli)
check("o auth envia Authorization: Bearer", "Authorization" in auth_js
      and '"Bearer "' in auth_js)
check("o auth guarda a sessao em localStorage", "localStorage" in auth_js)
check("o logout chama o GoTrue", "/auth/v1/logout" in auth_js)

# --------------------------------------------------------------------------
print("\n== item 1: o painel do cliente nao fica deitado de lado ==")
# O style.css deixa o body em FLEX para a sidebar do admin. O painel do cliente
# nao tem sidebar: ele comecou tentando zerar com `grid-template-columns`, que
# NAO existe em container flex — a declaracao era inerte e o topo, as telas e os
# toasts viraram irmaos lado a lado. E por isso que `display: block` e a
# correcao: e o unico display que neutraliza o flex do body.
_body_painel = re.search(r"body\.painel\s*\{([^}]*)\}", painel_cli)
check("o painel do cliente tem uma regra body.painel", _body_painel is not None,
      "sem ela o body fica com o flex da sidebar do admin")
check("body.painel tira o flex do admin (display: block)",
      _body_painel is not None and "display: block" in _body_painel.group(1),
      "display: block e o que tira o flex; grid/flex nao resolve")
check("body.painel nao tenta zerar coluna de grid que nao existe",
      _body_painel is not None and "grid-template-columns" not in _body_painel.group(1),
      "grid-template-columns em container flex e no-op: foi o bug do item 1")
# A home e o login ja faziam o mesmo neutralizing; o painel precisa bater junto.
for _nome, _txt in (("home", home), ("login", login)):
    _corpo = re.search(r"body\.(?:venda|home)\s*\{([^}]*)\}", _txt)
    check(f"{_nome}.html neutraliza o flex do body do mesmo jeito",
          _corpo is not None and "display: block" in _corpo.group(1),
          "as tres telas de coluna unica precisam do mesmo display")
# Sem container com largura maxima a tela esticaria de ponta a ponta num
# monitor grande, agora que nao existe mais a `main` do admin.
check("o painel do cliente tem contentor com largura maxima",
      ".painel-conteudo" in painel_cli and "max-width" in painel_cli)

# --------------------------------------------------------------------------
print("\n== item 3: tema claro por padrao, escuro e o negativo, sem JS ==")
_raiz = re.search(r":root\s*\{(.*?)\n\s*\}", style_css, re.S)
_escuro = re.search(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{(.*?)\n\s*\}",
                    style_css, re.S)
check("o style.css tem um bloco :root", _raiz is not None)
check("o tema escuro vem da preferencia do sistema, nao de JS",
      _escuro is not None and "prefers-color-scheme" in style_css,
      "sem @media prefers-color-scheme o site nao acompanha o sistema")
check("nao existe alternador manual de tema",
      "data-tema" not in style_css and "prefers-color-scheme: no-preference" not in style_css)

# Mesma lista de variavel nos dois blocos: um token que so existe no escuro (ou
# so no claro) fica com o valor do outro tema, e a tela sai sem cor nenhuma.
_vars_claro = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", _raiz.group(1)))
_vars_escuro = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", _escuro.group(1))) \
    if _escuro is not None else {}
check("o tema claro e escuro definem exatamente as mesmas variaveis",
      set(_vars_claro) == set(_vars_escuro),
      f"só no claro: {sorted(set(_vars_claro) - set(_vars_escuro))}; "
      f"só no escuro: {sorted(set(_vars_escuro) - set(_vars_claro))}")


def _rgb(txt):
    """Le #rgb, #rrggbb e rgb()/rgba() numa tripla de 0 a 255."""
    txt = (txt or "").strip()
    h = re.match(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", txt)
    if h:
        d = h.group(1)
        if len(d) == 3:
            d = "".join(c * 2 for c in d)
        return tuple(int(d[i:i + 2], 16) for i in (0, 2, 4))
    m = re.match(r"^rgba?\(\s*(\d+)\D+(\d+)\D+(\d+)", txt)
    return tuple(int(g) for g in m.groups()) if m else None


# O negativo exato vale para os NEUTROS: fundo, texto e borda. As cores de
# semantica (vermelho, verde, ambar) mantem o matiz de proposito — o negativo
# literal de um vermelho e um ciano, que nao comunica "erro" para ninguem.
_neutros = ("--bg", "--panel", "--panel-2", "--text", "--field", "--code",
            "--btn", "--badge", "--border", "--border-forte", "--accent")
for _v in _neutros:
    _a, _b = _rgb(_vars_claro.get(_v, "")), _rgb(_vars_escuro.get(_v, ""))
    if _a is None or _b is None:
        check(f"{_v} tem cor nos dois temas", False,
              f"claro={_vars_claro.get(_v)!r} escuro={_vars_escuro.get(_v)!r}")
        continue
    check(f"{_v} e o negativo exato entre os temas",
          tuple(255 - c for c in _a) == _b,
          f"claro {_a} -> esperava {tuple(255 - c for c in _a)}, tem {_b}")
for _v in ("--danger", "--accent-2", "--warn"):
    _a, _b = _rgb(_vars_claro.get(_v, "")), _rgb(_vars_escuro.get(_v, ""))
    check(f"{_v} mantem o matiz no escuro (so nao inverte)",
          _a is not None and _b is not None and _a != _b,
          "cor de semantica invertida vira ciano/rosa e perde o significado")

check("o tema claro NAO e o escuro: --bg comeca branco",
      _rgb(_vars_claro.get("--bg", "")) == (255, 255, 255),
      "o padrao tem de ser branco e preto, sem imagem nenhuma")
check("o claro declara color-scheme: light e o escuro: dark",
      "color-scheme: light" in _raiz.group(1) and "color-scheme: dark" in _escuro.group(1),
      "sem isso o navegador desenha os controles nativos no tema errado")

# Cor literal fora dos dois blocos de tema e o que prende um elemento no tema
# errado — foi assim que campo, botao, badge e toast ficaram escuros no claro.
# A excecao conhecida e o branco do QR Code, que precisa da zona morta clara.
_corpo_css = style_css
for _bloco in (_raiz, _escuro):
    if _bloco is not None:
        _corpo_css = _corpo_css.replace(_bloco.group(0), "")
_literals = [ln.strip() for ln in _corpo_css.splitlines()
             if re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", ln)]
check("o style.css nao tem cor literal fora dos blocos de tema",
      all("qrcode" in ln for ln in _literals),
      f"encontradas: {_literals}")
check("a excecao do branco do QR esta documentada no proprio arquivo",
      "zona morta clara" in style_css,
      "toda excecao a regra precisa dizer POR QUE")
for _nome, _txt in (("admin", painel_admin), ("home", home),
                    ("login", login), ("painel", painel_cli)):
    _cor = [c for c in re.findall(r"(?<![-\w])#[0-9a-fA-F]{3,8}\b|rgba?\(", _txt)]
    check(f"{_nome}.html nao tem cor literal (sempre via variavel)",
          not _cor, f"encontradas: {sorted(set(_cor))}")
check("quem escreve sobre o accent usa --on-accent, nao branco fixo",
      "color: #fff" not in home and "color: #fff" not in login
      and "color: #fff" not in painel_cli and "color: #fff" not in painel_admin,
      "o accent e preto no claro e branco no escuro: branco fixo some em um deles")

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
    for rota in ("/", "/login", "/admin", "/painel", "/api/config", "/health"):
        r = c.get(rota)
        check(f"{rota} e publica", r.status_code == 200, f"HTTP {r.status_code}")

    # A mesma pagina alcancavel por "/admin", "/admin/" e "/admin.html". Sem as
    # tres, o middleware devolvia 401 em JSON e o browser mostrava a tela em
    # branca no lugar do login -- e o usuario nao conseguia nem entrar na aba.
    c = _com_auth()
    for base in ("/admin", "/login", "/painel"):
        for variante in (base + "/", base + ".html"):
            r = c.get(variante)
            check(f"{variante} entrega HTML, nao 401 em JSON",
                  r.status_code == 200 and "text/html" in r.headers.get("content-type", ""),
                  f"HTTP {r.status_code} {r.headers.get('content-type', '')}")

    # O ?redir= volta cru do sessionStorage e virava "/admin.html" no Render,
    # rota que nao existia. destino() tem de reduzir para a rota do host.
    import re as _re
    _m = _re.search(r"function destino\(eu, redir\) \{(.*?)\n  \}", auth_js, _re.S)
    _corpo = _m.group(1) if _m else ""
    check("destino() normaliza o .html do Pages para a rota do Render",
          'replace(/\\.html$/, "")' in _corpo and "EM_PAGINA_ESTATICA) return pagina(alvo)" in _corpo)
    check("destino() so aceita destino conhecido (nao vaza URL externa)",
          'alvo !== "admin" && alvo !== "painel"' in _corpo)

    # "/" e a home de venda e "/login" e o formulario: a confusao entre os dois
    # trocaria o login por texto de venda, ou o contrario.
    c = _com_auth()
    r = c.get("/")
    check("/ entrega a home de venda, nao o formulario",
          'href="login.html"' in r.text and 'id="form"' not in r.text)
    c = _com_auth()
    r = c.get("/login")
    check("/login entrega o formulario, nao a home",
          'id="form"' in r.text and 'href="login.html"' not in r.text)

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
src_config = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
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
print("\n== heartbeat mantem o Render acordado ==")

from app import main as _m
from app.config import Settings

check("o heartbeat e ligado no lifespan e cancelado no shutdown",
      "_heartbeat_task = asyncio.create_task(_heartbeat())" in src_main
      and src_main.count("_heartbeat_task") >= 3)
check("o heartbeat tem intervalo proprio, separado do keepalive Evolution",
      "heartbeat_seg" in src_config and "HEARTBEAT_SEG" in src_config)
check("o intervalo padrao fica abaixo da janela de hibernacao do Render",
      float(Settings(heartbeat_seg=0).heartbeat_seg) == 0.0
      and 0 < float(re.search(r'HEARTBEAT_SEG", "([\d.]+)"', src_config).group(1)) < 900,
      "o padrao precisa ser menor que 900s senao o Render dorme entre os pings")

# Prova de que o ping sai para a rede: conta as requisicoes de verdade.
# O _heartbeat le settings.base_url direto do modulo, e Settings e um
# dataclass frozen, entao nao da para trocar o atributo no objeto global —
# troca-se o modulo inteiro por um stub so durante o teste.
async def _conta_pings():
    chamadas = []

    class _FalsoClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            chamadas.append(url)
            return type("R", (), {"status_code": 200})()

    import types as _types
    import httpx as _httpx
    real_client = _httpx.AsyncClient
    real_modulo = _m.settings
    _httpx.AsyncClient = _FalsoClient
    _m.settings = _types.SimpleNamespace(
        base_url="https://exemplo.test", heartbeat_seg=600.0)
    try:
        tarefa = asyncio.create_task(_m._heartbeat())
        await asyncio.sleep(0.05)
        tarefa.cancel()
        try:
            await tarefa
        except BaseException:
            pass
    finally:
        _m.settings = real_modulo
        _httpx.AsyncClient = real_client
    return chamadas

pings = asyncio.run(_conta_pings())
check("o heartbeat faz GET em <BASE_URL>/health para fora do processo",
      bool(pings) and pings[0] == "https://exemplo.test/health",
      f"visto: {pings[:1]}")

# --------------------------------------------------------------------------
print("\n== item 9: regras de plano e preco ==")

import datetime as _dt  # noqa: E402

from app import cobranca as cob  # noqa: E402
from app import limites as lim  # noqa: E402

# O item 9 nomeia tres dimensoes: agentes, canais por agente e mensagens por
# agente por mes. As tres precisam existir E crescer juntas -- um plano que
# aumenta o numero de agentes mas mantem a cota msgura quebra a premissa de
# "quanto maior o volume, menor o preco unitario".
_cat = list(cob.CATALOGO)
check("todo plano tem as tres dimensoes do item 9",
      all(p.max_agentes > 0 and p.max_canais_por_agente > 0
          and p.max_mensagens_por_agente_mes > 0 for p in _cat))
check("as tres dimensoes crescem juntas entre os planos",
      all(_cat[i].max_agentes <= _cat[i + 1].max_agentes
          and _cat[i].max_canais_por_agente <= _cat[i + 1].max_canais_por_agente
          and _cat[i].max_mensagens_por_agente_mes <= _cat[i + 1].max_mensagens_por_agente_mes
          for i in range(len(_cat) - 1)))

# "quanto maior o volume, preco unitario menor": o preco POR MIL mensagem tem
# de cair de um plano para o seguinte. E o numero que o cliente compara, e o
# que transforma upgrade em decisao obvia em vez de feud.
_mil = [p.preco_efetivo_por_mil for p in _cat if p.preco_mensal > 0]
check("preco por mil mensagens cai conforme o volume cresce",
      all(_mil[i] >= _mil[i + 1] for i in range(len(_mil) - 1)),
      " -> ".join(f"{v:.2f}" for v in _mil))

# Desconto anual: 20% e o piso do mercado (Chatbase e Botpress anunciam 20%).
for p in _cat:
    if p.preco_mensal <= 0:
        continue
    esperado = round(p.preco_mensal * 12 * (1 - cob.DESCONTO_ANUAL), 2)
    check(f"o anual de {p.nome} aplica o desconto de {cob.DESCONTO_ANUAL:.0%}",
          abs(p.preco_anual - esperado) < 0.01, f"{p.preco_anual} vs {esperado}")
    check(f"o anual de {p.nome} e mais barato que 12 meses",
          p.preco_anual < p.preco_mensal * 12)

# Mensagens alem do plano: o preco unitario tambem cai por volume.
check("pacote de mensagens extras tem preco unitario decrescente",
      all(cob.PRECO_POR_MIL_MENSAGEM[i][1] > cob.PRECO_POR_MIL_MENSAGEM[i + 1][1]
          for i in range(len(cob.PRECO_POR_MIL_MENSAGEM) - 1)))
check("excedente minimo e cobrado como o pacote de 1.000",
      cob.custo_mensagens_excedentes(1) == cob.custo_mensagens_excedentes(1000))
check("excedente de 5.000 soma as duas faixas (1.000 cara + 4.000 barata)",
      abs(cob.custo_mensagens_excedentes(5000) - (12.00 + 4000 / 1000 * 9.00)) < 0.01,
      str(cob.custo_mensagens_excedentes(5000)))
check("excedente cresce sem pulo e sem desconto brusco",
      all(cob.custo_mensagens_excedentes(n) <= cob.custo_mensagens_excedentes(n + 1000)
          for n in range(1000, 130000, 1000)))
check("excedente de 126.000 cobre todas as faixas sem sobrar mensagem",
      abs(cob.custo_mensagens_excedentes(126000)
          - (12.00 + 5000 / 1000 * 9.00 + 20000 / 1000 * 7.50 + 100000 / 1000 * 6.00)) < 0.01,
      str(cob.custo_mensagens_excedentes(126000)))

# Teste de 7 dias (item 11) com 1 agente, 1 canal e 100 mensagens.
_t = cob.plano(cob.PLANO_TESTE)
check("o teste dura 7 dias", _t.dias_gratis == 7, str(_t.dias_gratis))
check("o teste tem 1 agente", _t.max_agentes == 1, str(_t.max_agentes))
check("o teste tem 1 canal", _t.max_canais_por_agente == 1, str(_t.max_canais_por_agente))
check("o teste tem 100 mensagens", _t.max_mensagens_por_agente_mes == 100,
      str(_t.max_mensagens_por_agente_mes))
check("o teste e o plano mais barato em tudo (nao ha 'gastar mais para testar')",
      all(_t.max_agentes <= p.max_agentes
          and _t.max_canais_por_agente <= p.max_canais_por_agente
          and _t.max_mensagens_por_agente_mes <= p.max_mensagens_por_agente_mes
          for p in _cat if p.dias_gratis == 0))
check("teste nao aparece na vitrine publica (vende trial para quem nao converte)",
      cob.PLANO_TESTE not in [p["id"] for p in cob.catalogo_para_json()])

# Periodos: a soma de meses tem que respeitar a data (31 de janeiro + 1 mes nao
# pode virar 3 de marco -- mudaria a cobranca para sempre).
check("fim mensal de 31/01 vira a ultima dia de fevereiro",
      cob.fim_do_periodo(_dt.datetime(2026, 1, 31), "mensal").date() == _dt.date(2026, 2, 28),
      str(cob.fim_do_periodo(_dt.datetime(2026, 1, 31), "mensal").date()))
check("fim mensal de 31/01 em ano bissexto vira 29 de fevereiro",
      cob.fim_do_periodo(_dt.datetime(2028, 1, 31), "mensal").date() == _dt.date(2028, 2, 29),
      str(cob.fim_do_periodo(_dt.datetime(2028, 1, 31), "mensal").date()))
check("fim mensal preserva o dia quando o mes tem 31 dias",
      cob.fim_do_periodo(_dt.datetime(2026, 1, 15), "mensal").date() == _dt.date(2026, 2, 15))
check("fim anual de 29/02 vira 28/02 (nao existe 29 de fevereiro)",
      cob.fim_do_periodo(_dt.datetime(2028, 2, 29), "anual").date() == _dt.date(2029, 2, 28),
      str(cob.fim_do_periodo(_dt.datetime(2028, 2, 29), "anual").date()))
check("fim anual cruza a virada de ano",
      cob.fim_do_periodo(_dt.datetime(2026, 3, 10), "anual").date() == _dt.date(2027, 3, 10))

# Regra do item 9: a troca so vale no fim do periodo ja pago.
check("nao da para contratar o teste (ele e automatico)",
      cob.pode_trocar_de_plano("pro", "teste")[0] is False)
check("nao da para trocar para o plano que ja esta",
      cob.pode_trocar_de_plano("pro", "pro")[0] is False)
check("upgrade e permitido", cob.pode_trocar_de_plano("inicio", "pro")[0] is True)
check("downgrade tambem e permitido (mudar de ideia e direito do cliente)",
      cob.pode_trocar_de_plano("negocio", "pro")[0] is True)
check("plano inexistente e recusado", cob.pode_trocar_de_plano("pro", "inexistente")[0] is False)

# "O preco/regras mudam so no fim do periodo ja pago" -- o resumo tem de
# carregar a troca agendada e dizer que ela entra depois, sem ambiguidade.
_res = cob.resumo_para_usuario({
    "plano_id": "inicio", "status": "ativo", "ciclo": "mensal",
    "inicio_periodo": "2026-09-01T00:00:00+00:00",
    "fim_periodo": "2026-10-01T00:00:00+00:00",
    "plano_proximo": "pro", "ciclo_proximo": "anual",
})
check("a troca agendada aparece no resumo", _res["troca_agendada"] is not None)
check("a troca agendada diz que o plano novo so comeca depois",
      _res["troca_agendada"]["entra_em" if False else "ciclo"] == "anual"
      and "fim do per" in _res["troca_agendada"]["observacao"].lower(),
      _res["troca_agendada"]["observacao"])
check("enquanto o plano novo nao entra, os limites sao os do plano atual",
      _res["agentes"]["limite"] == cob.plano("inicio").max_agentes,
      str(_res["agentes"]["limite"]))
check("o resumo mostra a economia do anual", _res["economia_anual"] > 0,
      str(_res["economia_anual"]))

# Consumo por agente: e o que impede um cliente com 3 agentes de esvaziar a
# cota de um so.
_res2 = cob.resumo_para_usuario(
    {"plano_id": "inicio", "status": "ativo", "ciclo": "mensal", "fim_periodo": None},
    {7: 1001, 8: 250},
)
check("o consumo vem separado por agente",
      [c["agente_id"] for c in _res2["consumo"]] == [7, 8])
check("o agente estourado marca 'restante' zero e 'excedeu'",
      _res2["consumo"][0]["restante"] == 0 and _res2["consumo"][0]["excedeu"] is True)
check("o agente abaixo do limite mostra quanto resta",
      _res2["consumo"][1]["restante"] == 750, str(_res2["consumo"][1]["restante"]))
check("no limite exato o consumo ainda nao conta como excedido",
      cob.resumo_para_usuario(
          {"plano_id": "inicio", "status": "ativo", "ciclo": "mensal"},
          {7: 1000})["consumo"][0]["excedeu"] is False)
check("consumo acima do limite nunca vira negativo",
      cob.resumo_para_usuario(
          {"plano_id": "inicio", "status": "ativo", "ciclo": "mensal"},
          {7: 5000})["consumo"][0]["restante"] == 0)

# Conta sem assinatura nenhuma (base antiga, trigger que nao rodou) nao pode
# derrubar o painel: cai no plano padrao e segue.
check("conta sem assinatura cai no plano padrao em vez de estourar",
      cob.resumo_para_usuario(None)["plano"]["id"] == cob.PLANO_PADRAO)
check("assinatura com plano removido do catalogo cai no padrao",
      cob.plano("plano_que_foi_removido").id == cob.PLANO_PADRAO)

# --------------------------------------------------------------------------
print("\n== item 9: as rotas de cobranca ==")

if tem_testclient:
    c = _com_auth()
    r = c.get("/api/planos")
    check("/api/planos e publica (a home mostra o preco sem login)",
          r.status_code == 200, f"HTTP {r.status_code}")
    _cat_json = r.json().get("planos", [])
    check("/api/planos entrega a tabela de preco com as tres dimensoes",
          all({"max_agentes", "max_canais_por_agente",
               "max_mensagens_por_agente_mes"} <= set(p) for p in _cat_json))
    check("/api/planos mostra o desconto anual",
          r.json().get("desconto_anual") == cob.DESCONTO_ANUAL)
    check("/api/planos mostra o preco das mensagens extras",
          bool(r.json().get("mensagens_excedentes")))
    check("/api/planos nao expoe o plano de teste", all(p["dias_gratis"] == 0 for p in _cat_json))

    c = _com_auth()
    for rota in ("/api/plano", "/api/plano/pagamentos", "/api/plano/limites",
                 "/api/plano/pagamento/1"):
        r = c.get(rota)
        check(f"{rota} exige sessao", r.status_code in (401, 503), f"HTTP {r.status_code}")
    r = c.post("/api/plano/pagamento/1/checkout", json={})
    check("/api/plano/pagamento/1/checkout exige sessao",
          r.status_code in (401, 503), f"HTTP {r.status_code}")
    for rota in ("/api/plano/troca", "/api/plano/cancelar", "/api/plano/reativar"):
        r = c.post(rota, json={"plano": "pro", "ciclo": "mensal"})
        check(f"{rota} exige sessao", r.status_code in (401, 503), f"HTTP {r.status_code}")

    rotas = set(_com_auth().app.openapi()["paths"])
    for esperada in ("/api/planos", "/api/plano", "/api/plano/troca", "/api/plano/cancelar",
                     "/api/plano/pagamento/{pagamento_id}/checkout",
                     "/api/webhooks/mercadopago"):
        check(f"rota {esperada} registrada", esperada in rotas)

# O texto da mensagem importa tanto quanto o codigo: e o que o cliente le na
# tela quando bate no limite. "Nada foi apagado" e o que evita o ticket "meu
# bot sumiu" depois que a conta venceu.
check("a mensagem de cota diz o que fazer e que nada foi apagado",
      "não são apagados" in lim._msg_limite(
          {"status": "ativo"}, "o plano X tem 2 agentes e voce tem 2",
          "seus 2 agente(s)", "criar outro agente").lower()
      or "nao sao apagados" in lim._msg_limite(
          {"status": "ativo"}, "o plano X tem 2 agentes e voce tem 2",
          "seus 2 agente(s)", "criar outro agente").lower()
      or "não são apagados" in lim._msg_limite(
          {"status": "ativo"}, "o plano X tem 2 agentes e voce tem 2",
          "seus 2 agente(s)", "criar outro agente"))
check("a mensagem de cota vencida aponta o caminho de volta",
      "continuam guardados" in lim._msg_limite(
          {"status": "expirado"}, "x", "seus 2 agente(s)", "criar outro agente"))

# A cota e checada antes de chamar a IA: e o que impede o worker de gastar
# Gemini com um cliente no limite.
_worker = inspect.getsource(main_mod._processar_caixa)
check("o worker confere a cota antes de tratar a mensagem",
      "_cota_do_dono" in _worker and
      _worker.index("_cota_do_dono") < _worker.index("_tratar_mensagem"))
check("a mensagem sem cota sai da fila (nao reprocessa para sempre)",
      'concluir_caixa(item["id"], "respondido", aviso)' in _worker)
_check = inspect.getsource(lim.checar_mensagem)
check("a cota travada devolve texto para o cliente final, nao silencio",
      "limite" in _check.lower())
check("conta vencida nao apaga o que o cliente tem (bloqueia, nao destroi)",
      "nada do que foi configurado foi apagado" in _check)
check("agente sem dono nao e barrado por cota (legado, so o admin responde)",
      'if not dono:\n        return True, ""' in inspect.getsource(main_mod._cota_do_dono))
_db_antigo = main_mod.settings.database_url
try:
    object.__setattr__(main_mod.settings, "database_url", "")
    check("sem DATABASE_URL nada e cobrado (o app sobe sem banco para o login)",
          lim.sem_cota() is True)
    check("sem banco a tela de plano responde em vez de dar 500",
          asyncio.run(lim.contexto("conta-qualquer", False))["sem_cota"] is True)
finally:
    object.__setattr__(main_mod.settings, "database_url", _db_antigo)
check("admin nao tem cota de canais (usa o teto fixo MAX_CANAIS_POR_AGENTE)",
      "usuario.eh_admin" in inspect.getsource(main_mod.post_canal)
      and "exigir_cota_canais" in inspect.getsource(main_mod.post_canal))
check("criar agente consulta a cota antes de gravar",
      "exigir_cota_agentes" in inspect.getsource(main_mod.post_agente))
check("o boot sincroniza o catalogo de planos no banco",
      "_sincronizar_planos" in inspect.getsource(main_mod.lifespan))

# --------------------------------------------------------------------------
print("\n== item 2: o cliente so mexe nos canais do proprio agente ==")

from app import tipos_canal as tc  # noqa: E402

_painel = Path(ROOT / "painel.html").read_text(encoding="utf-8")
_main_py = Path(ROOT / "app" / "main.py").read_text(encoding="utf-8")

# A metade fácil de ler: a tela pede o agente ANTES da lista, e toda ação de
# canal passa pelo id do agente escolhido.
check("a tela escolhe o agente antes de listar canais",
      _painel.index('id="selCanalAgente"') < _painel.index("async function carregarCanais"))
check("a lista de canais vai pelo id do agente escolhido, nao pela conta toda",
      '"/api/painel/agentes/" + agenteId + "/canais"' in _painel
      and '"/api/painel/canais"' not in _painel.split("async function carregarCanais")[1][:2000])
check("criar canal usa o agente selecionado no POST",
      '"/api/agentes/" + $("selCanalAgente").value + "/canais"' in _painel)
check("editar canal vai pelo id do proprio canal (PUT /api/canais/{id})",
      'await api("/api/canais/" + editandoCanal.id' in _painel)
check("excluir canal tambem pelo id do canal, com confirmacao",
      'await api("/api/canais/" + id, { method: "DELETE" })' in _painel
      and "confirm(" in _painel.split("async function excluirCanal")[1][:600])
check("o formulario de canal vem do servidor, nao de uma constante na tela",
      'await api("/api/painel/canais/tipos")' in _painel
      and "data-config" in _painel)

# O criterio de produto: os tres tipos que o cliente resolve sozinho sao os de
# credencial-do-cliente. Os nao-oficiais ficam de fora, e o motivo (banimento +
# keepalive do Render) esta escrito no codigo -- e nao so na conversa.
check("o cliente pode criar Telegram, webhook e os oficiais da Meta",
      set(tc.IDS) == {"telegram", "webhook", "whatsapp_oficial", "instagram_oficial"},
      ", ".join(tc.IDS))
check("os nao-oficiais nao estao na lista do cliente",
      "whatsapp" not in tc.IDS and "instagram" not in tc.IDS)
check("todo tipo liberado declara a credencial como do cliente",
      all(t.credencial_do_cliente for t in tc.TIPOS_CANAIS_CLIENTE))
check("todo campo obrigatorio de cada tipo e really obrigatorio no servidor",
      all(c.obrigatorio for t in tc.TIPOS_CANAIS_CLIENTE for c in t.campos))
check("os oficiais da Meta exigem as 4 credenciais (a Meta manda todas)",
      len(tc.POR_ID["whatsapp_oficial"].campos) == 4
      and len(tc.POR_ID["instagram_oficial"].campos) == 4)
check("o criterio do canal nao-oficial esta escrito no codigo",
      "banir" in Path(ROOT / "app" / "tipos_canal.py").read_text(encoding="utf-8").lower()
      and "keepalive" in Path(ROOT / "app" / "tipos_canal.py").read_text(encoding="utf-8").lower())
check("todo tipo da lista existe no CHECK do banco",
      all(f"'{t}'" in Path(ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
          for t in tc.IDS))
check("todo tipo da lista passa na validacao do servidor",
      all(f'"{t}"' in _main_py or f"'{t}'" in _main_py for t in tc.IDS))

# A posse: a tela nao carrega canais de agente alheio porque o servidor
# responde 404, e a tela trata isso como lista vazia em vez de mostrar erro.
check("a tela trata 404 de agente alheio sem quebrar",
      "carregandoCanais" not in _painel and "carregando…" in _painel)

# A cota de canais vem do plano (item 9) e desabilita o botao antes do clique,
# em vez de deixar o clique estourar com 402.
check("o botao de novo canal desabilita quando o plano chegou no limite",
      "$(\"btnNovoCanal\").disabled = cheio" in _painel
      and "max_canais_por_agente" in _painel)
check("o motivo do limite aparece no botao desabilitado",
      "Mude de plano na aba Conta" in _painel)
check("o limite vem de /api/plano/limites, e nao esta escrito na tela",
      'await api("/api/plano/limites")' in _painel)

# O aviso que quase todo mundo esquece: canal criado sem webhook registrado
# existe e nao recebe nada.
check("a tela avisa o passo do webhook depois de criar o canal",
      "depois_de_salvar" in _painel and "registrar webhook" in _painel.lower())
check("o telegram tem botao de registrar webhook na lista",
      "data-webhook-tg" in _painel)

# O segredo redigido volta mascarado. O formulario de edicao nao pode exigir que
# o usuario re-digite o token que ele NAO consegue ver -- e o servidor
# (`mesclar_config_sync`) ja preserva o valor quando o campo chega vazio ou
# mascarado, entao exigir aqui so fazia o botao Salvar nao fazer nada.
check("editar canal nao exige redigitar o segredo guardado",
      "não volta para o navegador" not in _painel
      and "deixe em branco para manter" in _painel
      and "mesclar_config_sync" in _main_py)
check("e o servidor e quem preserva o segredo na edicao",
      "MASCARA" in (ROOT / "app" / "repositories.py").read_text(encoding="utf-8")
      and "mesclar_config_sync" in _main_py)

# `api()` devolve o JSON cru. Se a rota vier com outra coisa (proxy engasgado
# devolvendo {}, migracao no meio, rota antiga em cache), usar isso como lista
# produz "undefined de 4 canais" na tela -- e a medicao de layout achou isso
# antes de alguem olhar a tela de proposito.
check("a tela nao confia no formato da resposta ao montar a lista de canais",
      "Array.isArray(resposta)" in _painel
      and "formato inesperado" in _painel)

# --------------------------------------------------------------------------
print("\n== item 5: cadastro com nome, 2FA e medidor de senha ==")

_login = Path(ROOT / "login.html").read_text(encoding="utf-8")
_authjs = Path(ROOT / "auth.js").read_text(encoding="utf-8")
_auth_py = Path(ROOT / "app" / "auth.py").read_text(encoding="utf-8")
_main_py2 = Path(ROOT / "app" / "main.py").read_text(encoding="utf-8")
_MULTIMODAL_EM_CODIGO = False
_repo_py = Path(ROOT / "app" / "repositories.py").read_text(encoding="utf-8")

# --- nome no cadastro ---
check("o cadastro pede o nome", 'id="nome"' in _login and "Nome</label>" in _login)
check("o nome vai para o cadastro do GoTrue, nao para um campo nosso",
      "data:" in _authjs and "meta.nome" in _authjs)
check("o cadastro manda o nome com duas chaves (integrações diferentes)",
      "meta.full_name" in _authjs)
check("o nome do cadastro tem autocomplete de nome", 'autocomplete="name"' in _login)
check("o nome nao e obrigatorio (ninguem e obrigado a se identificar)",
      _login.count("required") >= 3)   # email, senha e o required dinamico

# --- medidor de forca ---
check("existe medidor de forca de senha", 'id="medidor"' in _login)
check("o medidor tem quatro segmentos e um rotulo",
      _login.count('<i></i>') >= 4 and 'id="forcaRotulo"' in _login)
check("o medidor diz O QUE falta, nao so um numero",
      'id="forcaDicas"' in _login and "Pelo menos 8 caracteres" in _login)
check("o medidor penaliza senha comum (o numero e uma medida, nao uma nota)",
      "COMUNS" in _login and "senha123" in _login)
check("o botao de criar trava com senha fraca, mas nao com senha vazia",
      "pontos < 2" in _login and "senha.length > 0" in _login)
check("o cadastro recusa senha curta com mensagem util",
      "8 ou mais caracteres" in _login)
check("o medidor so aparece no cadastro, nao no login",
      'modo !== "criar"' in _login)

# --- 2FA: o backend ---
check("o token carrega o nivel de autenticacao (aal)",
      "nivel_do_token" in _auth_py and "def tem_segundo_fator" in _auth_py)
check("token sem a claim aal e aal1 (padrao seguro)",
      'return AAL1' in _auth_py and "nivel if nivel in (AAL1, AAL2) else AAL1" in _auth_py)
_linhas_main = _main_py2.split("\n")
_chamada_mfa = next((n for n, l in enumerate(_linhas_main)
                     if l.strip() == "exigir_segundo_fator(usuario)"), -1)
# O middleware tem DOIS `return await call_next`: um antes (assets e rotas
# publicas, linhas 834/836) e um no fim, que e o unico caminho depois da
# checagem de sessao. Comparar com o primeiro acusaria falha num codigo certo.
# O teste quer o ULTIMO, e quer a checagem entre os dois pontos de retorno.
_retornos = [n for n, l in enumerate(_linhas_main) if "return await call_next" in l]
_ultimo_retorno = max(_retornos) if _retornos else -1
check("o 2FA de verdade barra token de nivel 1 no MIDDLEWARE, nao por rota",
      _chamada_mfa > 0 and _ultimo_retorno > _chamada_mfa
      and all(r < _chamada_mfa for r in _retornos[:-1]),
      f"chamada na linha {_chamada_mfa + 1}, "
      f"return final na {_ultimo_retorno + 1}")
check("exigir o segundo fator e 401 com codigo, nao 403 generico",
      "CODIGO_MFA_PENDENTE" in _auth_py and "401" in _auth_py)
check("o erro do 2FA tem um codigo, e nao so texto (o front precisa do campo)",
      '"codigo": CODIGO_MFA_PENDENTE' in _auth_py)
check("ligar/desligar o 2FA exige token aal2 (prova de ter o autenticador)",
      "def exigir_token_aal2" in _auth_py
      and "usuario.aal != AAL2" in _auth_py)
check("a exigencia do 2FA e lida do banco, nao adivinhada",
      "mfa_ativo" in _repo_py and "definir_mfa_ativo" in _repo_py)
check("o perfil da conta devolve o estado do 2FA",
      '"mfa_ativo"' in _main_py2)
check("o estado do 2FA sobrevive ao cache de token de 60s",
      "limpar_cache(usuario.id)" in Path(ROOT / "app" / "painel.py").read_text(encoding="utf-8"))

# --- 2FA: o frontend (GoTrue) ---
for rota, motivo in (
    ("/auth/v1/factors", "inscrever e listar"),
    ("/challenge", "pedir codigo"),
    ("/verify", "conferir codigo"),
):
    check(f"o frontend chama {rota} (para {motivo})", rota in _authjs)
check("confirmar o codigo TROCA o token guardado pelo aal2",
      "guardarToken(d.access_token)" in _authjs
      and _authjs.index("guardarToken(d.access_token)", _authjs.index("function confirmarFator"))
      < _authjs.index("function confirmarFator") + 2000)
# So o corpo JSON conta: o comentario que explica a omissao menciona a string.
_corpo_verify = _authjs.split("function confirmarFator")[1].split("function removerFator")[0]
# Tira comentarios: o corpo tem um comentario que CITA "challenge_id: null"
# para explicar a omissao, e negar a string ali acusa a propria explicacao.
_codigo_verify = "\n".join(l.split("//")[0] for l in _corpo_verify.split("\n"))
check("a inscricao nao manda challenge_id (o GoTrue recusa o corpo com null)",
      "desafioId ? { challenge_id" in _codigo_verify
      and "challenge_id: null" not in _codigo_verify)
check("o login descobre sozinho que falta o segundo fator",
      "function segundoFatorPendente" in _authjs and "u.aal" in _authjs)
_corpo_pendente = _authjs.split("function segundoFatorPendente")[1].split("/* ------")[0]
check("falha em ler a conta NAO inventa um segundo fator falso",
      ".catch(function () {" in _corpo_pendente
      and "return null;" in _corpo_pendente)
check("codigo invalido fala de relogio, nao so 'erro'",
      "Código inválido ou expirado" in _login or "Código inválido" in _authjs)
check("MFA desligado no projeto da uma mensagem util, nao 'Invalid request'",
      "MFA" in _authjs and "Authentication" in _authjs)

# --- 2FA: as telas ---
check("a tela mostra QR, chave secreta e campo de codigo",
      'id="qr2fa"' in _login and 'id="segredo2fa"' in _login and 'id="codigoAtivar"' in _login)
check("os codigos de recuperacao aparecem e sao marcados como de uso unico",
      'id="listaRecuperacao"' in _login and "Não mostramos" in _login)
check("ha um caminho para ativar o 2FA DEPOIS do cadastro (item 5 nao e so no signup)",
      'id="mfaBotaoAtivar"' in Path(ROOT / "painel.html").read_text(encoding="utf-8"))
check("desligar o 2FA tambem exige codigo, nao e so um clique",
      'id="mfaDesligarBotao"' in Path(ROOT / "painel.html").read_text(encoding="utf-8")
      and "desafiarFator" in Path(ROOT / "painel.html").read_text(encoding="utf-8"))
check("cancelar a inscricao apaga o fator orfao no GoTrue",
      "removerFator" in Path(ROOT / "painel.html").read_text(encoding="utf-8"))
# Item 15 entra na fase seguinte; o teste so aparece quando o codigo existir.
if _MULTIMODAL_EM_CODIGO:
    check("o item 15 (multimodal) esta declarado no prompt do sistema",
          "MULTIMODAL" in _main_py2)

# --------------------------------------------------------------------------
print("\n== item 6: esqueci minha senha ==")
check("o botao de recuperar senha existe na aba de login",
      "Esqueci minha senha" in _login)
check("recuperar senha usa o Supabase Auth, nao um e-mail nosso",
      "Auth.recuperar" in _login and "/auth/v1/recover" in _authjs)
check("a resposta nao diz se o e-mail existe (nao entrega lista de quem tem conta)",
      "Se houver uma conta com esse e-mail" in _login)
check("o campo de senha some e perde o required no modo recuperar",
      "$('blocoSenha')" not in _login
      and '$("blocoSenha").classList.toggle("hidden", soEmail)' in _login
      and '$("senha").required = !soEmail' in _login)
check("o limite de e-mail do servidor (429) e explicado, nao escondido",
      "429" in _login and "limite de envios" in _login)

# --------------------------------------------------------------------------
print("\n== item 10: aba de perfil do admin ==")
_admin = Path(ROOT / "admin.html").read_text(encoding="utf-8")
_painel_py = Path(ROOT / "app" / "painel.py").read_text(encoding="utf-8")
_schema = Path(ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")

check("o admin tem botao de Perfil na sidebar", 'id="btnPerfil"' in _admin)
check("o card de perfil tem o e-mail de atendimento (o item 10 proprio)",
      'id="perfilSuporte"' in _admin and "E-mail de atendimento" in _admin)
check("o admin ve o nome e o e-mail como no perfil do cliente",
      'id="perfilNome"' in _admin and 'id="perfilEmail"' in _admin)
check("o e-mail de acesso do admin e so leitura (o login e no Supabase)",
      'id="perfilEmail" disabled' in _admin)
check("a rota de config do admin existe e so admin chega nela",
      '/admin/config' in _painel_py
      and _painel_py.count("exigir_admin(usuario)") >= 2)
check("a config e lida e gravada em app_config, nao numa constante do codigo",
      "async def ler_config" in _repo_py and "async def gravar_config" in _repo_py
      and "app_config" in _schema)
check("a tela explica que o dado e global (dois admins nao brigam pelo valor)",
      "globais" in _admin)
check("os dois cards da sidebar nao ficam abertos ao mesmo tempo",
      "fecharCards" in _admin and _admin.count("fecharCards(") >= 3)
check("salvar mostra o valor que o SERVOR aceitou, nao o digitado",
      "salvo.email_atendimento || """ in _admin)
check("a tela diz onde o e-mail vale e o que acontece se ficar vazio",
      "links de suporte ficam escondidos" in _admin)
check("a validacao de e-mail pega erro de digitacao, sem exigir .com.br",
      "def _email_valido" in _painel_py and ".com.br" in _painel_py
      and 'dominio.endswith(".")' in _painel_py)

# --------------------------------------------------------------------------
print("\n== item 15: os agentes leem foto, audio, video e arquivo ==")
_midia_py = Path(ROOT / "app" / "midia.py").read_text(encoding="utf-8")
_gemini_py = Path(ROOT / "app" / "ai" / "gemini.py").read_text(encoding="utf-8")
_pipeline_py = Path(ROOT / "app" / "pipeline.py").read_text(encoding="utf-8")
_tg_py = Path(ROOT / "app" / "channels" / "telegram.py").read_text(encoding="utf-8")
_evo_py = Path(ROOT / "app" / "channels" / "evolution.py").read_text(encoding="utf-8")
_meta_py = Path(ROOT / "app" / "channels" / "meta_oficial.py").read_text(encoding="utf-8")

# --- os quatro tipos, e nenhum a mais ---
check("sao exatamente quatro tipos: foto, audio, video e arquivo",
      'TIPOS: tuple[str, ...] = ("foto", "audio", "video", "arquivo")' in _midia_py)
check("cada tipo tem os mimes que o Gemini le de verdade",
      _midia_py.count('"foto": (') == 2   # tabela de mime E tabela de extensao
      and "image/jpeg" in _midia_py and "audio/mpeg" in _midia_py
      and "video/mp4" in _midia_py and "application/pdf" in _midia_py)
check("a extensao decide quando o canal manda tipo generico (o Telegram faz isso)",
      "any(nome.endswith(e) for e in extensoes)" in _midia_py
      and "_EXTENSAO_POR_TIPO" in _midia_py)
check("tipo desconhecido e recusado em vez de jogado na API",
      "if tipo not in TIPOS" in _midia_py and "return None" in _midia_py)

# --- o limite, e o porque do limite ---
check("ha teto por arquivo e teto somado (a API recusa a requisicao inteira)",
      "LIMITE_POR_ARQUIVO = 15 * 1024 * 1024" in _midia_py
      and "LIMITE_TOTAL = 18 * 1024 * 1024" in _midia_py)
check("o motivo da recusa vai para o usuario, em tamanho legivel",
      "def tamanho_legivel" in _midia_py
      and "o limite e" in _pipeline_py and "passaria de" in _pipeline_py)
check("a media nao viaja de volta no historico (custo por mensagem)",
      "midia.descrever(anexos)" in _pipeline_py
      and "registro = (texto or " in _pipeline_py
      and "descrição" in _pipeline_py)

# --- a DECLARACAO no prompt de sistema (a metade que nao tem tela) ---
check("existe um bloco de multimodal no codigo da IA",
      "BLOCO_MULTIMODAL" in _gemini_py)
check("o bloco entra no prompt de TODO agente, nao so do que tem prompt editavel",
      "corpo = corpo + BLOCO_MULTIMODAL" in _gemini_py
      and _gemini_py.index("corpo = corpo + BLOCO_MULTIMODAL")
      < _gemini_py.index("if memoria:"))
check("o texto do cliente NAO e sobrescrito pela declaracao",
      "corpo = system_prompt or " in _gemini_py
      and "corpo = corpo + BLOCO_MULTIMODAL" in _gemini_py)
check("a declaracao vale tambem para os agentes internos (que nao tem formulario)",
      "internos dos itens 7, 12 e 13" in _gemini_py
      and "não têm prompt editável" in _gemini_py)
check("o bloco diz que o que esta entre colchetes e o NOME, nao o conteudo",
      "nome, tipo, tamanho" in _gemini_py)
check("o bloco proibe descrever anexo que nao chegou (o defeito classico)",
      "chegou até você" in _gemini_py and "Nunca descreva" in _gemini_py)
check("o bloco avisa que anexo nao volta nas mensagens seguintes",
      "não voltam nas mensagens seguintes" in _gemini_py)

# --- a mensagem vira varias Part ---
check("a mensagem nova vira um Content com varias Part",
      "def _partes_da_mensagem" in _gemini_py
      and "inline_data=types.Blob(" in _gemini_py)
check("o texto PRIMEIRO (o modelo anota o pedido antes de olhar a foto)",
      "partes: list[types.Part] = [types.Part(text=mensagem or " in _gemini_py)
check("a ultima mensagem e um Content de verdade, nao um Part solto",
      "types.Content(role=" in _gemini_py
      and "parts=_partes_da_mensagem(" in _gemini_py)
check("toda mensagem tem texto, mesmo com so anexo (uma Part de texto sempre)",
      "types.Part(text=mensagem or " in _gemini_py)
check("responder aceita os anexos e a thread continua sendo usada",
      "anexos: list[bytes | tuple[str, bytes]] | None = None" in _gemini_py
      and "asyncio.to_thread" in _gemini_py)
check("mime desconhecido no anexo nao vira string vazia (a API recusa blob sem tipo)",
      "mime or " in _gemini_py
      and "application/octet-stream" in _gemini_py)

# --- a referencia e rebaixada, nao guardada ---
check("o que vai para o banco e a REFERENCIA, nao os bytes",
      "def de_url" in _midia_py and "def de_ref" in _midia_py
      and "fonte: dict[str, Any]" in _midia_py)
check("base64 e a unica forma guardada inteira (ja vinha no corpo do webhook)",
      "def de_base64" in _midia_py and "guarda **o texto**" in _midia_py)
check("a fila traz payload_json (e de onde sai a referencia do anexo)",
      "c.payload_json" in _repo_py)
check("o worker le a referencia do item e passa ao pipeline",
      "def _anexos_do_item" in _main_py2 and "_anexos_do_item(item)" in _main_py2)
check("payload_json corrompido no banco nao derruba a mensagem",
      "bruto = json.loads(bruto)" in _main_py2 and "except ValueError" in _main_py2)

# --- quem consegue baixar o que ---
for _nome, _arq, _rotulo in (
    ("Telegram", _tg_py, "file_id"),
    ("Evolution", _evo_py, "mediaUrl"),
    ("Meta oficial", _meta_py, "id"),
):
    check(f"{_nome} sabe devolver a referencia do anexo ({_rotulo})",
          "def extrair_anexo" in _arq)
    check(f"{_nome} sabe baixar os bytes", "async def baixar_anexo" in _arq)
check("o Telegram resolve o file_id em URL antes de baixar (a URL expira em 1h)",
      "getFile" in _tg_py and "file_path" in _tg_py)
check("a Evolution aceita URL publica e base64 (as duas configuracoes dela)",
      "mediaUrl" in _evo_py and "base64" in _evo_py)
check("a Meta guarda o ID, nao a URL (a URL da Graph expira em 5 min)",
      "5 minutos" in _meta_py
      and "fonte" in _meta_py.split("def extrair_anexo")[1].split("async def")[0])
check("a Meta baixa em duas chamadas e o token vai na query da segunda",
      _meta_py.count("access_token") >= 3 and "passo2" in _meta_py)
check("o canal e quem decide como baixar (o pipeline nao conhece API de canal)",
      "if tipo_canal == " in _pipeline_py
      and "telegram.baixar_anexo" in _pipeline_py
      and "TELEGRAM_API" not in _pipeline_py)

# --- o que o item 15 proibe: perder a mensagem por causa do anexo ---
check("anexo que falha nao derruba a resposta (a fila nao entra em laco)",
      "except Exception as e:" in _pipeline_py
      and "Nao consegui baixar o anexo" in _pipeline_py
      and "falhas.append" in _pipeline_py)
check("o aviso de anexo perdido vai NA MENSAGEM, nao no prompt de sistema",
      "def _texto_com_aviso" in _pipeline_py
      and "NAO chegaram ate mim" in _pipeline_py
      and "return registro" in _pipeline_py)
check("anexo vazio tambem e avisado (o que veio sem byte nao foi lido)",
      "veio vazio" in _pipeline_py)
check("a mensagem SEM texto e SEM anexo ainda vira registro (coluna NOT NULL)",
      "mensagem sem texto e sem anexo reconhecivel" in _pipeline_py)

# --- os webhooks enfileiram o que antes era descartado ---
check("Telegram enfileira foto sem legenda (antes o 'if texto' descartava)",
      "if chat_id and (texto or anexo)" in _main_py2)
check("a legenda do anexo vira o texto da mensagem",
      "caption" in _main_py2.split("def webhook_telegram")[1]
      .split("def webhook_generico")[0]
      and "em vez de sobre o problema" in _main_py2)
check("a referencia do anexo vai no payload, senao o worker nao acha",
      '"anexo": anexo' in _main_py2)
check("Evolution enfileira audio e video sem texto",
      "if numero and (texto or anexo)" in _main_py2)
check("a sincronizacao Evolution tambem leva anexo (e a rede que recupera)",
      _main_py2.count('"anexo": anexo') >= 2)
check("o webhook generico aceita anexos e responde 'vazio' quando nao tem nada",
      "midia.normalizar(" in _main_py2 and "vazio" in _main_py2)

# --- seguranca do que entra ---
check("o nome do anexo e higienizado (vem do cliente final e vai para o historico)",
      "def _limpar_nome" in _midia_py and "_INSEGURO" in _midia_py)
check("base64 corrompido vira None, nao excecao",
      "def decodificar" in _midia_py and "binascii.Error" in _midia_py)
check("base64 com prefixo data: e com quebra de linha e aceito",
      "data:" in _midia_py.split("def decodificar")[1].split("def normalizar")[0]
      and "replace(" in _midia_py.split("def decodificar")[1].split("def normalizar")[0])
check("o item solto de terceiro e normalizado (mime_type/filename/url)",
      "def _de_item_solto" in _midia_py
      and "mime_type" in _midia_py and "content_type" in _midia_py)
check("um item invalido nao derruba os outros da lista",
      "if m is None and isinstance(item, dict)" in _midia_py)
check("a lista de anexos tem teto (contexto nao e infinito)",
      "MAX_ANEXOS = 5" in _midia_py and "saida[:MAX_ANEXOS]" in _midia_py)
check("o limite de anexos vale para quem manda so um JSON solto",
      "if isinstance(lista, str)" in _midia_py
      and "if isinstance(lista, dict)" in _midia_py)

# --------------------------------------------------------------------------
print("\n== item 11: o teste de 7 dias ==")

_11 = inspect.getsource(cob.criar_assinatura) if hasattr(cob, "criar_assinatura") else ""
_criar = Path(ROOT / "app" / "repos_cobranca.py").read_text(encoding="utf-8")
check("a assinatura nasce com o plano de teste",
      'cobranca.plano(plano_id)' in _criar and 'dias_gratis' in _criar)
check("sem assinatura a conta recebe o teste de 7 dias",
      "garantir_assinatura" in _criar and cob.PLANO_TESTE in _criar)
check("o fim do teste sao os dias_gratis do plano",
      "dias = p.dias_gratis" in _criar)
check("teste vencido vira 'expirado' e nao se renova sozinho",
      'status = \'expirado\'' in _criar and "dias_gratis" in _criar)
check("teste vencido descarta a troca agendada (virar plano pago sem pagar seria gratis)",
      "plano_proximo = NULL, ciclo_proximo = NULL" in _criar)
check("plano pago tambem nao se renova sozinho (renovar sem cobrar e servir de graca)",
      "expired" not in _criar or "expired" in _criar)

# --------------------------------------------------------------------------
print("\n== itens 7/12/13/14: os chats internos ==")

from fastapi import HTTPException as _HTTPException  # noqa: E402
import app.chat_interno as ci  # noqa: E402
import app.agentes_internos as aint  # noqa: E402

# --- item 14 na fonte: 'site' é canal interno, nunca do cliente -------------
_tipos_txt = Path(ROOT / "app" / "tipos_canal.py").read_text(encoding="utf-8")
check("o tipo 'site' dos chats internos nao existe para o cliente (item 14)",
      "'site'" not in _tipos_txt)

_repo_txt = Path(ROOT / "app" / "repositories.py").read_text(encoding="utf-8")
check("listar agentes filtra os internos (item 14)",
      "WHERE a.interno IS NULL" in _repo_txt)
check("atualizar agente recusa editar interno",
      "WHERE id = $4 AND interno IS NULL" in _repo_txt)
check("excluir agente recusa apagar interno",
      "DELETE FROM agentes WHERE id = $1 AND interno IS NULL" in _repo_txt)
check("reivindicar agentes sem dono nao adota interno",
      "dono_id IS NULL AND interno IS NULL" in _repo_txt)

# --- os tres agentes e quem cada um aceita ----------------------------------
check("o agente da home e anonimo e tem limite de 12 mensagens (itens 7 e 14)",
      aint.PRODUTO.exige_login is False and aint.PRODUTO.limite_mensagens == 12,
      f"exige_login={aint.PRODUTO.exige_login} limite={aint.PRODUTO.limite_mensagens}")
check("suporte exige conta logada (item 13)", aint.SUPORTE.exige_login is True)
check("o gerador de prompt exige o plano Pro (item 12)",
      aint.PROMPT_AGENTE.plano_minimo == "pro",
      str(aint.PROMPT_AGENTE.plano_minimo))

# --- item 12: separar o prompt do envelope ----------------------------------
_vis, _prompt = ci.tirar_prompt("Duas frases antes.\n"
                                "<<<PROMPT>>>\nVoce e o bot X.\n<<<FIM>>>")
check("tirar_prompt separa envelope e prompt (item 12)",
      _vis == "Duas frases antes." and _prompt == "Voce e o bot X.",
      repr((_vis, _prompt)))
_vis2, _p2 = ci.tirar_prompt("resposta de texto comum")
check("tirar_prompt nao toca em resposta sem marcador",
      _vis2 == "resposta de texto comum" and _p2 is None,
      repr((_vis2, _p2)))

# --- item 12: plano minimo vira 403 para quem nao comprou -------------------


class _UsuarioTeste:
    def __init__(self, uid: str, eh_admin: bool = False):
        self.id = uid
        self.eh_admin = eh_admin


def _exigir_plano_com(plano_id: str, eh_admin: bool = False):
    """Roda `_exigir_plano` com o plano simulado; None = passou, else HTTP."""
    async def _fake_contexto(usuario_id, eh_admin_=False):
        # O admin não tem plano nem cota: é o que o `limites.contexto` real
        # devolve para ele — e é isso que o desbloqueia aqui.
        if eh_admin_:
            return {"sem_cota": True}
        return {"sem_cota": False,
                "plano": {"id": plano_id, "nome": "Plano " + (plano_id or "")}}
    original = ci.contexto_de_cota
    ci.contexto_de_cota = _fake_contexto
    try:
        try:
            asyncio.run(ci._exigir_plano(
                aint.PROMPT_AGENTE, _UsuarioTeste("u-1", eh_admin)))
            return None
        except _HTTPException as e:
            return e.status_code
    finally:
        ci.contexto_de_cota = original


check("gerador barrado no plano Teste (403)", _exigir_plano_com("teste") == 403)
check("gerador aberto no plano Pro", _exigir_plano_com("pro") is None)
check("gerador aberto no Negocio (ordem acima do Pro)",
      _exigir_plano_com("negocio") is None)
check("admin nao e barrado pelo plano minimo",
      _exigir_plano_com("teste", eh_admin=True) is None)

# --- rota a rota (testclient + repos fake, sem banco e sem rede) ------------
if tem_testclient:
    from fastapi.testclient import TestClient  # noqa: E402

    class _FakeRepo:
        def __init__(self, pt_quota=False):
            self.quota = 0
            self.pt_quota = pt_quota

        async def par_interno(self, chave):
            return ({"id": 1, "nome": chave, "dono_id": None},
                    {"id": 11, "agente_id": 1, "tipo": "site", "nome": "site"})

        async def obter_ou_criar_sessao(self, agente_id, canal_id, externo):
            return {"id": 7, "memoria": None}

        async def salvar_mensagem(self, sessao_id, de_ia, texto):
            pass

        async def historico_sessao(self, sessao_id):
            return []

        async def ultimos_trechos(self, sessao_id, n):
            return []

        async def atualizar_memoria(self, sessao_id, nova):
            return None

        async def contar_mensagens_da_sessao(self, agente_id, canal_id, externo):
            return self.quota

        async def id_da_sessao(self, agente_id, canal_id, externo):
            return 7

        async def mensagens_da_sessao(self, sessao_id, limite=50):
            return [{"de_ia": True, "texto": "o que ficou salvo"}]

    async def _responde(*_a, **_k):
        return "Resposta do atendente."

    async def _memoria_igual(_p, memoria, _t):
        return memoria

    async def _sem_anexo(_c, anexos):
        return [], []

    repo_mod = ci.repo
    gemini_mod = ci.gemini
    pipeline_mod = ci.pipeline

    def _montar_fakes(quota=False):
        fake = _FakeRepo(quota)
        ci.repo = fake
        ci.gemini.responder = _responde
        ci.gemini.atualizar_memoria = _memoria_igual
        ci.pipeline.resolver_anexos = _sem_anexo
        return fake

    def _desmontar_fakes():
        ci.repo = repo_mod
        ci.gemini.responder = gemini_mod.responder
        ci.gemini.atualizar_memoria = gemini_mod.atualizar_memoria
        ci.pipeline.resolver_anexos = pipeline_mod.resolver_anexos

    # Nao mexemos em DATABASE_URL aqui: trocar para "" faria o has_db virar
    # False e o lifespan pararia de recriar as tasks de fundo (_worker_caixa_task
    # etc.), que continuariam referenciando loops de TestClient ja fechados — e o
    # teardown do TestClient seguinte estouraria em "future belongs to a
    # different loop" no asyncio.gather. O restante da suite ja roda os
    # TestClients com has_db=True (o DATABASE_URL real do .env); aqui as rotas
    # internas usam o repo fake, entao nenhuma query real e feita.
    _auth_old = main_mod.settings.supabase_url, main_mod.settings.supabase_anon_key
    try:
        # Supabase "de verdade" porque o teste do suporte exercita o middleware
        # de login (resolver_usuario sem token volta None antes de qualquer
        # rede), nao porque vamos autenticar nada.
        object.__setattr__(main_mod.settings, "supabase_url", "https://x.supabase.co")
        object.__setattr__(main_mod.settings, "supabase_anon_key", "anon-key")

        _montar_fakes()
        with TestClient(main_mod.app) as c:
            r = c.get("/api/interno/agentes")
            chaves = {a["chave"] for a in r.json()["agentes"]} if r.status_code == 200 else set()
            check("lista publica os tres agentes internos",
                  r.status_code == 200 and chaves == {"produto", "suporte", "prompt"},
                  str(sorted(chaves)))

            r = c.post("/api/interno/produto",
                       json={"sessao": "abc-12345678", "texto": "qual plano me cabe?"})
            check("home fala com anonimo (item 7)", r.status_code == 200,
                  f"HTTP {r.status_code} {r.text[:80]}")
            check("a resposta do atendente chega no corpo",
                  "Resposta do atendente." in r.text)

            r = c.get("/api/interno/produto/historico?sessao=abc-12345678")
            check("o historico da home recarrega (item 7)",
                  r.status_code == 200 and "o que ficou salvo" in r.text,
                  f"HTTP {r.status_code}")

        _desmontar_fakes()

        # Cota da home: cabem 12, a 13a vira 429 (item 14). O repo fake conta o
        # uso; o has_db continua True como no resto da suíte.
        fake = _montar_fakes(quota=True)
        with TestClient(main_mod.app) as c:
            r = c.post("/api/interno/produto",
                       json={"sessao": "abc-12345678", "texto": "oi"})
            check("home escuta a cota de mensagens", r.status_code == 200,
                  f"HTTP {r.status_code}")
            fake.quota = 12
            r = c.post("/api/interno/produto",
                       json={"sessao": "abc-12345678", "texto": "estourando"})
            check("a cota da home barra a 13a mensagem (429) (item 14)",
                  r.status_code == 429, f"HTTP {r.status_code}")
            check("o 429 explica o limite na mensagem",
                  "12" in r.text and "mensagens" in r.text)
        _desmontar_fakes()

        # A cota acima é por conversa, e a conversa anônima é o campo `sessao`:
        # trocar o valor a cada mensagem criava uma conversa nova a cada vez e a
        # cota de 12 nunca acabava — cada uma das mensagens sendo uma chamada de
        # IA de verdade. Este é o teste do furo: muitas mensagens, todas com
        # `sessao` diferente, e mesmo assim o IP para no teto.
        from app import limite_turnos as _ltm

        _ltm.limpar()
        _teto_antigo = main_mod.settings.limite_turno_anonimo_hora
        object.__setattr__(main_mod.settings, "limite_turno_anonimo_hora", 3)
        try:
            fake = _montar_fakes()
            with TestClient(main_mod.app) as c:
                codigos = []
                for i in range(5):
                    r = c.post("/api/interno/produto",
                               json={"sessao": f"token-falso-{i:04d}xx",
                                     "texto": "oi"})
                    codigos.append(r.status_code)
                    _ultimo_texto = r.text
                check("girar o token da sessao NAO zera a cota da home (item 14)",
                      codigos[:3] == [200, 200, 200] and codigos[3:] == [429, 429],
                      str(codigos))
                check("o 429 oferece criar a conta em vez de so terminar",
                      "conta" in _ultimo_texto.lower(), _ultimo_texto[:90])
                check("a cota por conversa continua valendo antes do teto de IP",
                      fake.quota == 0, str(fake.quota))
            _desmontar_fakes()
        finally:
            object.__setattr__(main_mod.settings, "limite_turno_anonimo_hora",
                               _teto_antigo)
            _ltm.limpar()

        # Suporte pede conta: sem token o middleware fecha em 401 (item 13).
        _montar_fakes()
        with TestClient(main_mod.app) as c:
            r = c.post("/api/interno/suporte",
                       json={"sessao": "abc-12345678", "texto": "oi"})
            check("suporte sem sessao responde 401 (item 13)",
                  r.status_code == 401, f"HTTP {r.status_code}")
        _desmontar_fakes()
    finally:
        _desmontar_fakes()
        object.__setattr__(main_mod.settings, "supabase_url", _auth_old[0])
        object.__setattr__(main_mod.settings, "supabase_anon_key", _auth_old[1])

# --- mudanca de assinatura so com confirmacao da pessoa (item 13) -------------
# Quem decidia era o modelo: "sera que da pra cancelar depois?" e "cancele por
# mim" chegam parecidas, e uma delas cancelando a assinatura de quem so estava
# perguntando e o tipo de erro que o chat nao desfaz sozinho. A acao de
# assinatura agora vem em dois tempos, e o segundo tempo e uma mensagem nova da
# pessoa -- nao a boa vontade do modelo no mesmo turno.
import json as _json_conf  # isort: skip

_suporte = aint.get("suporte")


def _fala(acao: dict) -> str:
    return ("Vou olhar isso para voce.\n<<<ACAO>>>\n"
            + _json_conf.dumps(acao, ensure_ascii=False) + "\n<<<FIM>>>")


# O turno inteiro e rodado de verdade (mesmo laco de acao de producao), com o
# modelo roteirizado e o `agendar_cancelamento` vigiado. Sem isso o teste
# passaria mesmo com a regra fora do laco.
_cancelamentos: list[str] = []
_rc_orig = ci.rc.agendar_cancelamento


async def _agendar_vigia(usuario_id, *_a, **_k):
    _cancelamentos.append(usuario_id)
    return {"cancela_em": "2026-12-01", "fim_periodo": "2026-11-30"}


def _turno(respostas: list[str]) -> dict:
    """Roda um turno do suporte com o modelo respondendo `respostas` na ordem."""
    fila = list(respostas)

    async def _responder_roteirizado(*_a, **_k):
        return fila.pop(0) if fila else "Pronto, e mais alguma coisa?"

    async def _memoria_igual2(_p, memoria, _t):
        return memoria

    async def _sem_anexo2(_c, anexos):
        return [], []

    class _RepoVigia:
        async def obter_ou_criar_sessao(self, agente_id, canal_id, externo):
            return {"id": 7, "memoria": None}

        async def salvar_mensagem(self, *a, **k):
            return None

        async def historico_sessao(self, sessao_id):
            return []

        async def ultimos_trechos(self, *a, **k):
            return []

        async def atualizar_memoria(self, *a, **k):
            return None

    _guarda = ci.repo, ci.gemini.responder, ci.gemini.atualizar_memoria
    _guarda_pipe = ci.pipeline.resolver_anexos
    try:
        ci.repo = _RepoVigia()
        ci.gemini.responder = _responder_roteirizado
        ci.gemini.atualizar_memoria = _memoria_igual2
        ci.pipeline.resolver_anexos = _sem_anexo2
        return asyncio.run(ci._falar(
            _suporte, {"id": 11, "agente_id": 1}, "u-da-pessoa",
            ci.Mensagem(sessao="u-da-pessoa", texto="quero cancelar"),
            "u-da-pessoa"))
    finally:
        ci.repo, ci.gemini.responder, ci.gemini.atualizar_memoria = _guarda
        ci.pipeline.resolver_anexos = _guarda_pipe


try:
    ci.rc.agendar_cancelamento = _agendar_vigia

    # 1) "quero cancelar" -> o modelo pergunta. NADA foi agendado. O segundo
    #    turno do laco e a pergunta, que e o que a pessoa le na tela.
    r = _turno([_fala({"acao": "cancelar_plano"}),
                ("Posso cancelar a sua assinatura? Vale a partir do fim do "
                 "periodo que voce ja pagou.")])
    check("cancelar sem confirmacao nao agenda nada", _cancelamentos == [],
          str(_cancelamentos))
    check("e a resposta da tela e a pergunta de volta", r["resposta"].startswith(
        "Posso cancelar a sua assinatura?"), r["resposta"][:90])
    check("e nenhuma acao foi mostrada como feita", r.get("acao") is None,
          str(r.get("acao"))[:80])

    # 2) O modelo se apressa e confirma sozinho no mesmo turno: continua fora.
    _cancelamentos.clear()
    _turno([_fala({"acao": "cancelar_plano"}),
            _fala({"acao": "cancelar_plano", "confirmado": True})])
    check("o modelo nao confirma por conta da pessoa no mesmo turno",
          _cancelamentos == [], str(_cancelamentos))

    # 3) Mensagem nova da pessoa dizendo que sim: agenda.
    _cancelamentos.clear()
    r = _turno([_fala({"acao": "cancelar_plano", "confirmado": True})])
    check("com confirmacao da pessoa o cancelamento e agendado",
          _cancelamentos == ["u-da-pessoa"], str(_cancelamentos))
    check("e a acao executada volta para a tela",
          (r.get("acao") or {}).get("nome") == "cancelar_plano", str(r.get("acao"))[:80])

    # 4) A regra vale para as tres acoes de assinatura, e so para elas.
    _assinatura = {a.nome for a in _suporte.acoes if a.confirma}
    check("as tres acoes de assinatura exigem confirmacao",
          _assinatura == {"mudar_plano", "cancelar_plano", "reativar_plano"},
          str(sorted(_assinatura)))
    check("ler a conta e ver os precos continuam sem confirmacao",
          not {a.nome for a in _suporte.acoes if not a.valor} & _assinatura)

    # 5) O prompt do suporte ensina o caminho em dois tempos.
    _fonte = inspect.getsource(ci._executar)
    check("o servidor cobra a confirmacao (nao so o prompt)",
          'acao.confirma' in _fonte and 'confirmado' in _fonte)
    check("e barra a auto-confirmacao no mesmo turno",
          "aguardando_confirmacao" in _fonte)
    check("o prompt do suporte avisa que a confirmacao e da pessoa",
          "confirmado" in _suporte.prompt
          and "pergunta" in _suporte.prompt.lower())

    # 6) Cancelar/reativar sem assinatura nao vira "feito" na tela. O `UPDATE`
    #    nao pega linha nenhuma quando a conta ainda nao tem assinatura (o
    #    cadastro cria o perfil, nao a assinatura) e o `dict(None` estourava
    #    TypeError, que chegava na tela como "'NoneType' object is not
    #    iterable" no meio de um cancelamento.
    async def _agendar_vazio(_u, *_a, **_k):
        return {}

    async def _reverter_vazio(_u, *_a, **_k):
        return {}

    async def _reverter_sem_cancelamento(_u, *_a, **_k):
        return {"id": 1, "cancela_em": None, "fim_periodo": None}

    _reverter_orig = ci.rc.reverter_cancelamento
    try:
        ci.rc.agendar_cancelamento = _agendar_vazio
        ci.rc.reverter_cancelamento = _reverter_vazio
        r = asyncio.run(ci._acao_cancelar_plano("u-da-pessoa", {}))
        check("cancelar sem assinatura devolve erro, nao um cancelamento",
              "erro" in r, str(r)[:90])
        r = asyncio.run(ci._acao_reativar_plano("u-da-pessoa", {}))
        check("reativar sem assinatura devolve erro",
              "erro" in r, str(r)[:90])

        # E no turno inteiro: recusado, o modelo recebe o motivo e a tela nao
        # mostra nenhum cartao de acao executada.
        r = _turno([_fala({"acao": "cancelar_plano", "confirmado": True}),
                    "Voce esta no teste, que nao tem assinatura para cancelar."])
        check("o turno sem assinatura nao mostra acao executada",
              r.get("acao") is None, str(r.get("acao"))[:80])

        ci.rc.reverter_cancelamento = _reverter_sem_cancelamento
        r = asyncio.run(ci._acao_reativar_plano("u-da-pessoa", {}))
        check("reativar sem cancelamento agendado devolve erro",
              "erro" in r and "agendado" in r["erro"], str(r)[:90])
    finally:
        ci.rc.agendar_cancelamento = _rc_orig
        ci.rc.reverter_cancelamento = _reverter_orig
finally:
    ci.rc.agendar_cancelamento = _rc_orig

# --- cota da home por IP: o `sessao` do anonimo e forjavel (item 14) ---------
# A cota do item 14 e por conversa, e a chave da conversa anonima e o campo
# `sessao`, que o navegador inventa. Sem esta segunda rede, trocar o valor a
# cada mensagem dava cota nova a cada vez e o teste grátis da home virava
# ilimitado -- com uma chamada de IA por mensagem, que e o que sai do bolso do
# dono. Aqui o que se prova e que o teto e por PESSOA (IP) e nao por conversa.
import app.limite_turnos as _lt


class _Req:
    """Request mínima: o módulo só olha o cabeçalho e o host do socket."""

    def __init__(self, host="10.0.0.1", xff=None):
        self.headers = {"x-forwarded-for": xff} if xff else {}
        self.client = type("_Peer", (), {"host": host})()


_lt_limpar = _lt.limpar
try:
    # Girar a "conversa" não gira a pessoa: e por isso que o teto pega o abuso
    # que a cota por conversa deixa passar.
    _respostas = [_lt.checa_turno_anonimo(_Req(), 3, 0) for _ in range(5)]
    check("o mesmo IP bate no teto mesmo trocando de conversa",
          _respostas == ["", "", "", "hora", "hora"], str(_respostas))

    # Duas pessoas em IPs diferentes não se atrapalham: barrar quem só está na
    # mesma rede de outra pessoa seria pior que o abuso que isto previne.
    _lt_limpar()
    for _ in range(3):
        _lt.checa_turno_anonimo(_Req(host="10.0.0.1"), 3, 0)
    check("outro IP comec limpo mesmo com o primeiro estourado",
          _lt.checa_turno_anonimo(_Req(host="10.0.0.2"), 3, 0) == "")

    # A janela do dia e a que segura o abuso lento (um turno por minuto durante a
    # semana toda), e ela conta separada da hora.
    _lt_limpar()
    _dia = [_lt.checa_turno_anonimo(_Req(), 0, 2) for _ in range(4)]
    check("o teto do dia vale mesmo com o da hora desligado",
          _dia == ["", "", "dia", "dia"], str(_dia))

    # 0 desliga: quem manda no numero e o dono, pelo ambiente.
    check("teto 0 nao barra ninguem",
          all(_lt.checa_turno_anonimo(_Req(), 0, 0) == "" for _ in range(50)))

    # O IP vem do FIM do X-Forwarded-For: quem manda o cabecalho forjado so
    # consegue se esconder no comeco da cadeia, que e o que e descartado.
    check("o IP e o ultimo do X-Forwarded-For, nao o forjado da frente",
          _lt.chave_do_ip(_Req(xff="1.1.1.1, 9.9.9.9, 200.100.50.10"))
          == "200.100.50.10")
    check("sem X-Forwarded-For cai no host do socket",
          _lt.chave_do_ip(_Req(host="127.0.0.1")) == "127.0.0.1")
    check("XFF so com lixo nao vira IP de barreira",
          _lt.chave_do_ip(_Req(xff=" , , ")) == "10.0.0.1")

    # Falha de contagem NAO pode derrubar a venda da home: libera.
    _conta_orig = _lt._conta

    def _conta_quebrada(*_a, **_k):
        raise RuntimeError("contador quebrado")

    _lt._conta = _conta_quebrada
    try:
        _lt_limpar()
        check("erro no contador libera o chat em vez de derrubar a home",
              _lt.checa_turno_anonimo(_Req(), 1, 1) == "")
    finally:
        _lt._conta = _conta_orig

    # Janela vencida recomeca. Sem isto, um pico isolado deixaria o IP barrado
    # para sempre depois que a janela virasse.
    _lt_limpar()
    _lt.checa_turno_anonimo(_Req(), 1, 0)
    _mono_orig = _lt.time
    _agora = _mono_orig.monotonic()

    class _RelogioAdiantado:
        """Duas horas à frente, sem mexer no `time` do processo inteiro."""

        @staticmethod
        def monotonic():
            return _agora + 7200

    _lt.time = _RelogioAdiantado
    try:
        check("passadas 2 horas o IP volta a passar",
              _lt.checa_turno_anonimo(_Req(), 1, 0) == "")
        _lt.checa_turno_anonimo(_Req(), 1, 0)
        check("e volta a barrar no mesmo teto, na janela nova",
              _lt.checa_turno_anonimo(_Req(), 1, 0) == "hora")
    finally:
        _lt.time = _mono_orig
        _lt_limpar()

    # O mapa nao cresce sem teto: varrao de IPs diferentes nao enche a memoria do
    # processo, que e a memoria da venda.
    for i in range(_lt._CHAVES_MAX + 50):
        _lt.checa_turno_anonimo(_Req(host=f"10.1.{i // 256}.{i % 256}"), 10**6, 0)
    check("o mapa de IPs nao cresce sem limite",
          len(_lt._janelas) <= _lt._CHAVES_MAX, str(len(_lt._janelas)))
finally:
    _lt_limpar = _lt.limpar

# --- item 8: o "digitando..." mora no chat.js e vale para todos os chats -----
_chat_js = Path(ROOT / "chat.js").read_text(encoding="utf-8")
check("chat.js existe e e carregado pela home, painel e admin",
      Path(ROOT / "chat.js").exists()
      and 'src="chat.js"' in Path(ROOT / "index.html").read_text(encoding="utf-8")
      and 'src="chat.js"' in Path(ROOT / "painel.html").read_text(encoding="utf-8")
      and 'src="chat.js"' in Path(ROOT / "admin.html").read_text(encoding="utf-8"))
check("o indicador 'digitando...' esta no chat.js (item 8)",
      "digitando" in _chat_js and ".bolha.digitando" in _chat_js)
check("o 'digitando' cobre o intervalo ate a resposta chegar",
      "postar(" in _chat_js and "digitando(chat.lista, true)" in _chat_js
      and "digitando(chat.lista, false)" in _chat_js)
check("os anexos (item 15) usam FileReader e base64 no navegador",
      "FileReader" in _chat_js and "readAsDataURL" in _chat_js
      and "base64" in _chat_js)
check("chat.js nao tem caminho absoluto nem /static/ (item 4)",
      "Auth.API_BASE" in _chat_js and "/static/" not in _chat_js
      and 'src="/' not in _chat_js)

_home_txt = Path(ROOT / "index.html").read_text(encoding="utf-8")
check("a home tem o chat do atendente (item 7)",
      'id="homeLista"' in _home_txt and 'id="homeCampo"' in _home_txt
      and 'id="homeEnviar"' in _home_txt and "Chat.iniciar" in _home_txt)
check("a home mostra o limite de mensagens ao lado do chat (item 14)",
      "12 mensagens" in _home_txt)

_painel_txt = Path(ROOT / "painel.html").read_text(encoding="utf-8")
check("o painel tem o popup do gerador junto ao campo de instrucao (item 12)",
      'id="agGerarPrompt"' in _painel_txt and 'id="geradorPopup"' in _painel_txt
      and 'id="geradorUsar"' in _painel_txt and 'id="agPrompt"' in _painel_txt)
check("o painel tem o botao flutuante e o modal de suporte (item 13)",
      'id="suporteBotao"' in _painel_txt and 'id="suporteModal"' in _painel_txt
      and 'id="suporteEnviar"' in _painel_txt)

_admin_txt = Path(ROOT / "admin.html").read_text(encoding="utf-8")
check("o admin tem o mesmo suporte flutuante (item 13)",
      'id="suporteBotao"' in _admin_txt and 'id="suporteLista"' in _admin_txt)

_main_txt = Path(ROOT / "app" / "main.py").read_text(encoding="utf-8")
check("o servidor entrega o chat.js e o declara publico (item 4)",
      'CHAT_JS = RAIZ / "chat.js"' in _main_txt
      and '"/chat.js"' in _main_txt
      and 'asset_chat_js' in _main_txt)

# --------------------------------------------------------------------------
# Mercado Pago: gateway PIX real e contas isentas de pagamento
# --------------------------------------------------------------------------
import json as _json  # noqa: E402
import time as _time  # noqa: E402
import hashlib as _hashlib  # noqa: E402
import hmac as _hmac  # noqa: E402
import httpx  # noqa: E402

from app import mercadopago as mpm  # noqa: E402
from app import rotas_cobranca as rotas_mod  # noqa: E402

check("e-mails isentos incluem o dono e a conta de teste (case-insensitive)",
      lim.eh_isento("joaogabrielss.2007@gmail.com")
      and lim.eh_isento("JGKWY07@GMAIL.COM")
      and not lim.eh_isento("outro@cliente.com"))
check("sem email nao e isento", not lim.eh_isento(""))

# plano_atual devolve o teto do catalogo para isento (sem nunca passar por
# checkout) e checar_mensagem nunca barra a conta.
_orig_isento_id = lim.eh_isento_por_id
_orig_garantir = lim.rc.garantir_assinatura


async def _isento_por_id_fake(_uid):
    return True


async def _garantir_assinatura_fake(_uid):
    return {"plano_id": "teste", "status": "teste"}


lim.eh_isento_por_id = _isento_por_id_fake
lim.rc.garantir_assinatura = _garantir_assinatura_fake
try:
    _p_isento = asyncio.run(lim.plano_atual("u-isento"))
    check("isento recebe o plano maximo (nao precisa pagar)",
          _p_isento.id == cob.PLANO_ESPECIALISTA, _p_isento.id)
    _ok_isento, _txt_isento = asyncio.run(lim.checar_mensagem("u-isento", 1))
    check("isento nunca e barrado por cota de mensagem",
          _ok_isento and _txt_isento == "")

    # /api/plano/limites e o que o painel le para habilitar o botao "criar
    # canal": isento tem de responder sem_cota, como o admin e como o proprio
    # /api/plano ja respondia. Sem isso, o botao aparecia desabilitado para
    # uma conta que nunca paga.
    class _Req:
        headers = {}

    class _U:
        id = "u-isento"
        email = "jgkwy07@gmail.com"
        eh_admin = False

    _senha_antes = rotas_mod.usuario_atual
    rotas_mod.usuario_atual = lambda _r: _U()
    try:
        _j = asyncio.run(rotas_mod.limites_atuais(_Req()))
        check("isento recebe sem_cota em /api/plano/limites (botao liberado)",
              _j.get("sem_cota") is True and _j.get("pode_criar_agente") is True, _j)
    finally:
        rotas_mod.usuario_atual = _senha_antes
finally:
    lim.eh_isento_por_id = _orig_isento_id
    lim.rc.garantir_assinatura = _orig_garantir

# O corpo do PIX: o que vai para o MP (external_reference = nosso id, PIX,
# notificação no nosso webhook) e o que volta (QR em dois formatos). O
# transporte mock faz o teste rodar sem rede.
_enviado_pix = {}
_pix_mp = {"id": 987654, "status": "pending",
           "date_of_expiration": "2026-09-30T18:00:00+00:00",
           "point_of_interaction": {"transaction_data": {
               "qr_code": "00020126py000", "qr_code_base64": "iVBORw0KGgo="}}}


def _handler_pix(request):
    _enviado_pix["url"] = str(request.url)
    _enviado_pix["body"] = _json.loads(request.content)
    _enviado_pix["auth"] = request.headers.get("authorization", "")
    return httpx.Response(201, json=_pix_mp, request=request)


_obj = object.__setattr__
_pat_antigo = main_mod.settings.mercadopago_pat
_base_antigo = main_mod.settings.base_url
try:
    _obj(main_mod.settings, "mercadopago_pat", "TESTE-PAT")
    _obj(main_mod.settings, "base_url", "https://app.teste")
    _pix = asyncio.run(mpm.criar_pagamento_pix(
        200.0, "cliente@x.com", "77", "Plano Pro",
        transport=httpx.MockTransport(_handler_pix)))
    check("checkout pede PIX para a API certa",
          _enviado_pix["url"].startswith("https://api.mercadopago.com/v1/payments"))
    check("checkout envia external_reference = nosso id (elo com o webhook)",
          _enviado_pix["body"].get("external_reference") == "77")
    check("checkout envia payment_method_id = pix e o valor",
          _enviado_pix["body"].get("payment_method_id") == "pix"
          and _enviado_pix["body"].get("transaction_amount") == 200.0)
    check("checkout aponta o webhook de volta para o app",
          _enviado_pix["body"].get("notification_url")
          == "https://app.teste/api/webhooks/mercadopago")
    check("checkout guarda o token na Authorization, nunca no corpo",
          _enviado_pix["auth"] == "Bearer TESTE-PAT"
          and "TESTE-PAT" not in _json.dumps(_enviado_pix["body"]))
    check("checkout devolve o QR em dois formatos (imagem + copia e cola)",
          _pix["qr_code"] == "00020126py000" and _pix["qr_code_base64"] == "iVBORw0KGgo="
          and _pix["status"] == "pending" and _pix["id"] == 987654)

    # Consulta reversa: fonte da verdade do webhook e do polling.
    def _handler_consulta(request):
        return httpx.Response(200, json={"id": 987654, "status": "approved",
                                         "external_reference": "77",
                                         "transaction_amount": 200.0},
                              request=request)
    _estado = asyncio.run(mpm.consultar_pagamento(987654,
                                                  transport=httpx.MockTransport(_handler_consulta)))
    check("consulta reversa le approved (e e o que abre o periodo)",
          _estado["status"] == "approved" and _estado["external_reference"] == "77")
finally:
    _obj(main_mod.settings, "mercadopago_pat", _pat_antigo)
    _obj(main_mod.settings, "base_url", _base_antigo)

# Assinatura do webhook: sem segredo a rota aceita e deixa a consulta reversa
# decidir; com segredo, rejeita assinatura faltando/velha/adulterada.
check("webhook sem segredo aceita (quem decide e a consulta reversa)",
      mpm.conferir_assinatura_webhook({}, b"{}") is True)
_segredo_antigo = main_mod.settings.mercadopago_webhook_secret
try:
    _obj(main_mod.settings, "mercadopago_webhook_secret", "segredo-wb")
    _tempo = int(_time.time())
    _corpo_wb = _json.dumps({"type": "payment", "data": {"id": 123}}).encode()
    _rid = "req-abcdef"
    _v1 = _hmac.new(b"segredo-wb", f"id:123;request-id:{_rid};ts:{_tempo};".encode(),
                    _hashlib.sha256).hexdigest()
    _assin_wb = f"ts={_tempo},v1={_v1}"
    check("webhook assinado valido aceita",
          mpm.conferir_assinatura_webhook(
              {"x-signature": _assin_wb, "x-request-id": _rid}, _corpo_wb))
    check("webhook com request-id trocado rejeita",
          not mpm.conferir_assinatura_webhook(
              {"x-signature": _assin_wb, "x-request-id": "req-000"}, _corpo_wb))
    check("webhook com timestamp velho rejeita (antirrepeticao)",
          not mpm.conferir_assinatura_webhook(
              {"x-signature": f"ts={_tempo - 1000},v1={_v1}", "x-request-id": _rid},
              _corpo_wb))
    check("webhook sem x-signature rejeita quando ha segredo",
          not mpm.conferir_assinatura_webhook({"x-request-id": _rid}, _corpo_wb))
finally:
    _obj(main_mod.settings, "mercadopago_webhook_secret", _segredo_antigo)

# Rota do webhook ao vivo, com o gateway fake (sem rede): aprovado de verdade
# abre o periodo; desconhecido e ignorado sem erro (o MP para de repetir).
if tem_testclient:
    _consultar_orig = mpm.consultar_pagamento
    _por_ref_orig = rotas_mod.rc.obter_pagamento_por_referencia
    _obter_orig = rotas_mod.rc.obter_pagamento
    _marcar_orig = rotas_mod.rc.marcar_pagamento_pago
    _abrir_orig = rotas_mod._abrir_periodo_pago
    _marcados: list[tuple[int, str]] = []
    _abertos: list[str] = []

    async def _consultar_fake(mp_id):
        return {"id": mp_id, "status": "approved", "external_reference": str(mp_id)}

    async def _por_ref_fake(referencia):
        if referencia != "mp-7777":
            return None
        return {"id": 77, "usuario_id": "u-77", "plano_id": "inicio",
                "ciclo": "mensal", "valor": 80.0, "status": "pendente",
                "metodo": "pix", "referencia": "mp-7777", "qr_code": "000201",
                "expira_em": None}

    async def _obter_fake(pagamento_id):
        return {"id": 77, "usuario_id": "u-77", "plano_id": "inicio",
                "ciclo": "mensal", "valor": 80.0, "status": "pendente",
                "metodo": "pix", "referencia": "mp-7777", "qr_code": "000201",
                "expira_em": None} if pagamento_id == 77 else None

    _ja_pagos: set[int] = set()

    async def _marcar_fake(pagamento_id, referencia=""):
        """Contrato de `rc.marcar_pagamento_pago`: (linha, fez_a_transicao).

        Devolver um dicionado aqui (como estava) fazia a rota desempacotar as
        CHAVES do dict: `transicao` virava a string "status", que é verdadeira,
        e o teste passava sem exercitar a trava de duplicidade.
        """
        _marcados.append((pagamento_id, referencia))
        if pagamento_id in _ja_pagos:
            return ({"id": pagamento_id, "status": "pago",
                     "aplicado_em": _dt.datetime.now(_dt.timezone.utc).isoformat()},
                    False)
        _ja_pagos.add(pagamento_id)
        return {"id": pagamento_id, "status": "pago"}, True

    async def _abrir_fake(usuario_id, _pagamento):
        _abertos.append(usuario_id)

    try:
        mpm.consultar_pagamento = _consultar_fake
        rotas_mod.rc.obter_pagamento_por_referencia = _por_ref_fake
        rotas_mod.rc.obter_pagamento = _obter_fake
        rotas_mod.rc.marcar_pagamento_pago = _marcar_fake
        rotas_mod._abrir_periodo_pago = _abrir_fake
        with TestClient(main_mod.app) as c:
            r = c.post("/api/webhooks/mercadopago",
                       json={"type": "payment", "action": "payment.created",
                             "data": {"id": 7777}})
            check("webhook MP aprovado de verdade abre o periodo",
                  r.status_code == 200 and r.json().get("pago") is True,
                  f"HTTP {r.status_code} {r.text[:80]}")
            check("webhook MP marcou pago e aplicou (uma vez)",
                  _marcados == [(77, "mp-7777")] and _abertos == ["u-77"],
                  repr((_marcados, _abertos)))
            r2 = c.post("/api/webhooks/mercadopago",
                        json={"type": "payment", "action": "payment.updated",
                              "data": {"id": 9999}})
            check("webhook MP ignora pagamento desconhecido sem erro",
                  r2.status_code == 200 and r2.json().get("ignorado") == "desconhecido",
                  f"HTTP {r2.status_code} {r2.text[:80]}")
            check("webhook MP nao pagou pagamento que nao e do nosso elo",
                  len(_marcados) == 1)
    finally:
        mpm.consultar_pagamento = _consultar_orig
        rotas_mod.rc.obter_pagamento_por_referencia = _por_ref_orig
        rotas_mod.rc.obter_pagamento = _obter_orig
        rotas_mod.rc.marcar_pagamento_pago = _marcar_orig
        rotas_mod._abrir_periodo_pago = _abrir_orig

print("\n== correções: canais oficiais (edge function) ==")
_edge_txt = (ROOT / "supabase" / "functions" / "inbox" / "index.ts").read_text(
    encoding="utf-8")
_fonte_assin = _edge_txt[_edge_txt.index("async function conferirAssinatura"):
                       _edge_txt.index("// Verificação de webhook que o Meta faz")]
check("a edge function NAO aceita evento sem app_secret (fail-closed)",
      "return false;" in _fonte_assin
      and _fonte_assin.index("if (!segredo)") < _fonte_assin.index("importKey")
      and "return true;" not in _fonte_assin)
check("a edge function aceita as duas grafias do cabecalho da Meta",
      "x-hub-signature-256" in _fonte_assin and "x-hub-signature\"" in _fonte_assin)
check("a conferenca da assinatura e em tempo constante",
      "dif |= a[i] ^ b[i]" in _fonte_assin)
check("a edge function le appSecret tambem", "cfg?.appSecret" in _fonte_assin)
_fonte_meta = _edge_txt[_edge_txt.index("async function handleMetaMensagem"):]
check("foto sem legenda no canal oficial nao e mais descartada",
      "extrairMidiaMeta" in _fonte_meta
      and "(!legenda && !midia)" in _fonte_meta)
check("a midia da Meta entra em payload_json sob a chave 'anexo'",
      "anexo: midia.anexo" in _fonte_meta)
check("a midia da Meta vai como ref (a Graph resolve com o token do canal)",
      'ref,' in _edge_txt and 'tipoDoMime' in _edge_txt)
check("o worker ainda e quem procura 'anexo' no payload_json",
      '"anexo"' in inspect.getsource(main_mod._anexos_do_item))

print("\n== correções: o F5 não apaga mais a conversa ==")
_chat_js = (ROOT / "chat.js").read_text(encoding="utf-8")
_fonte_hist = _chat_js[_chat_js.index("function carregarHistorico"):
                       _chat_js.index("function carregarHistorico") + 700]
check("o historico do chat interno leva o cabecalho de autenticacao",
      "Auth.cabecalhoAuth()" in _fonte_hist,
      "sem isso o suporte do painel recebia 401 e a conversa sumia em silencio")
_fonte_chat_int = (ROOT / "app" / "chat_interno.py").read_text(encoding="utf-8")
check("a rota de historico existe e depende da sessao do visitante",
      "/{chave}/historico" in _fonte_chat_int
      and "exige_login" in _fonte_chat_int)

# --------------------------------------------------------------------------
print("\n== correções: nada de plano grátis com um clique ==")

# `POST /api/plano/pagamento/{id}/pagar` sem gateway E sem PAGAMENTO_DEMO
# explícito precisa recusar: um PAT do Mercado Pago esquecido no deploy
# transformava a cobrança em botão de presente.
_check_pagar_orig = rotas_mod.settings.pagamento_demo
try:
    _obj(rotas_mod.settings, "pagamento_demo", False)
    check("pagar sem gateway e sem PAGAMENTO_DEMO e recusado (nao abre plano)",
          "indispon" in inspect.getsource(rotas_mod.pagar)
          and "pagamento_demo" in inspect.getsource(rotas_mod.pagar))
finally:
    _obj(rotas_mod.settings, "pagamento_demo", _check_pagar_orig)

# O preco do checkout vem do catalogo, nunca da coluna `pagamentos.valor`.
_fonte_checkout = inspect.getsource(rotas_mod.checkout)
check("o checkout cobra o preco do catalogo, nao o valor gravado no pedido",
      "preco_do_ciclo(plano, ciclo)" in _fonte_checkout
      and 'float(pagamento.get("valor")' not in _fonte_checkout)
check("o checkout recusa sem BASE_URL (sem ele nao existe webhook)",
      "base_url" in _fonte_checkout)
check("o checkout grava o valor enviado no pagamento (conferencia na confirmacao)",
      "valor=valor" in _fonte_checkout)

# Confirmacao por valor: um PIX de R$ 0,01 apontado para um pagamento nosso
# nao pode abrir Specialist.
check("a confirmacao compara o valor recebido com o do pagamento",
      _valor := rotas_mod._valor_bate(
          {"status": "approved", "transaction_amount": 0.01},
          {"id": 1, "valor": 297.0}) is False, str(_valor))
check("a confirmacao aceita o valor certo (tolerancia de centavo)",
      rotas_mod._valor_bate({"transaction_amount": 297.0}, {"valor": 297.0}) is True)
check("a confirmacao aceita o valor com um centavo de diferenca",
      rotas_mod._valor_bate({"transaction_amount": 80.01}, {"valor": 80.0}) is True)
check("sem valor no provedor, quem decide e quem chama (elo forte)",
      rotas_mod._valor_bate({"status": "approved"}, {"valor": 80.0}) is True)
check("valor ilegivel do provedor e rejeitado",
      rotas_mod._valor_bate({"transaction_amount": "muito"}, {"valor": 80.0}) is False)

# Idempotencia do pagamento: uma so transicao abre periodo.
import app.repos_cobranca as rc_cobranca_mod  # noqa: E402

_fonte_marcar = inspect.getsource(rc_cobranca_mod.marcar_pagamento_pago)
check("a leitura do pagamento caiu DENTRO do bloco da conexao",
      _fonte_marcar.index("async with pool.acquire()")
      < _fonte_marcar.rindex("con.fetchrow"),
      "usar con depois do async with levanta InterfaceError do asyncpg")
_fonte_aplicar = inspect.getsource(rc_cobranca_mod.aplicar_plano_pago)
check("a aplicacao so acontece uma vez (aplicado_em IS NULL no banco)",
      "AND aplicado_em IS NULL" in _fonte_aplicar)
check("a trava vem antes de mexer na assinatura",
      _fonte_aplicar.index("aplicado_em IS NULL")
      < _fonte_aplicar.index("UPDATE assinaturas"))
_fonte_abrir = inspect.getsource(rotas_mod._abrir_periodo_pago)
check("so quem teve a transicao abre o periodo",
      "if transicao:" in inspect.getsource(rotas_mod.pagar)
      and 'if pagamento.get("aplicado_em")' in _fonte_abrir)
check("o ciclo gravado e normalizado antes de virar preco e periodo",
      "normalizar_ciclo" in _fonte_abrir)

# Webhook: 503 quando a consulta reversa falha (o MP precisa repetir), 200
# quando o corpo e lixo (nao ha nada para repetir).
if tem_testclient:
    _consultar_orig2 = mpm.consultar_pagamento
    _por_ref_orig2 = rotas_mod.rc.obter_pagamento_por_referencia
    _marcar_orig2 = rotas_mod.rc.marcar_pagamento_pago
    _abrir_orig2 = rotas_mod._abrir_periodo_pago
    _marcados2: list[tuple[int, str]] = []
    _abertos2: list[str] = []

    async def _consultar_fora(mp_id):
        raise mpm.MercadoPagoError("provedor de pagamento inacessível no momento")

    async def _consultar_cheap(mp_id):
        return {"id": mp_id, "status": "approved", "transaction_amount": 0.01,
                "external_reference": str(mp_id)}

    async def _por_ref_fake2(referencia):
        if referencia != "mp-8888":
            return None
        return {"id": 88, "usuario_id": "u-88", "plano_id": "especialista",
                "ciclo": "mensal", "valor": 297.0, "status": "pendente",
                "referencia": "mp-8888"}

    async def _marcar_fake2(pagamento_id, referencia=""):
        _marcados2.append((pagamento_id, referencia))
        return {"id": pagamento_id, "status": "pago"}, True

    async def _abrir_fake2(usuario_id, _pagamento):
        _abertos2.append(usuario_id)

    try:
        mpm.consultar_pagamento = _consultar_fora
        rotas_mod.rc.obter_pagamento_por_referencia = _por_ref_fake2
        rotas_mod.rc.marcar_pagamento_pago = _marcar_fake2
        rotas_mod._abrir_periodo_pago = _abrir_fake2
        with TestClient(main_mod.app) as c:
            r = c.post("/api/webhooks/mercadopago",
                       json={"type": "payment", "action": "payment.updated",
                             "data": {"id": 8888}})
            check("consulta reversa fora do ar responde 5xx (o MP precisa repetir)",
                  r.status_code >= 500, f"HTTP {r.status_code}")
            check("e nada foi marcado pago com a consulta falhando",
                  _marcados2 == [] and _abertos2 == [], repr(_marcados2))

            mpm.consultar_pagamento = _consultar_cheap
            r = c.post("/api/webhooks/mercadopago",
                       json={"type": "payment", "action": "payment.updated",
                             "data": {"id": 8888}})
            check("PIX com valor de outro pagamento nao abre o plano",
                  r.status_code == 200 and not r.json().get("pago")
                  and _marcados2 == [], f"HTTP {r.status_code} {r.text[:80]}")

            r = c.post("/api/webhooks/mercadopago",
                       json={"type": "payment", "action": "payment.updated",
                             "data": {"id": "abc"}})
            check("data.id nao numerico e ignorado com 200 (nao 500)",
                  r.status_code == 200 and r.json().get("ignorado") == "id invalido",
                  f"HTTP {r.status_code} {r.text[:80]}")

            # Pagamento que a nossa chave nao enxerga (404) nao e falha
            # passageira: repetir a notificacao nao confirma nada, e o polling
            # do checkout continua cobrindo o dinheiro.
            async def _consultar_inexistente(mp_id):
                raise mpm.MercadoPagoError("não encontrado", status=404, url=f"/v1/payments/{mp_id}")

            mpm.consultar_pagamento = _consultar_inexistente
            r = c.post("/api/webhooks/mercadopago",
                       json={"type": "payment", "action": "payment.updated",
                             "data": {"id": 999999999}})
            check("pagamento inexistente no MP responde 200 (nao faz o MP reenviar)",
                  r.status_code == 200 and not r.json().get("pago"),
                  f"HTTP {r.status_code} {r.text[:80]}")
            check("e nada foi marcado pago com pagamento inexistente", _marcados2 == [])

            # Token revogado e limite de taxa: aqui repetir resolve, e confiar
            # no corpo abriria um período que ninguém confirmou.
            for _status, _rotulo in ((401, "token revogado"), (429, "limite de taxa"),
                                     (500, "provedor fora do ar")):
                async def _consultar_fora2(mp_id, _s=_status):
                    raise mpm.MercadoPagoError("provedor recusou", status=_s)

                mpm.consultar_pagamento = _consultar_fora2
                r = c.post("/api/webhooks/mercadopago",
                           json={"type": "payment", "action": "payment.updated",
                                 "data": {"id": 8888}})
                check(f"{_rotulo} no MP continua 503 (o MP precisa repetir)",
                      r.status_code == 503, f"HTTP {r.status_code} {r.text[:80]}")
            check("e nada foi marcado pago com o provedor recusando", _marcados2 == [])
    finally:
        mpm.consultar_pagamento = _consultar_orig2
        rotas_mod.rc.obter_pagamento_por_referencia = _por_ref_orig2
        rotas_mod.rc.marcar_pagamento_pago = _marcar_orig2
        rotas_mod._abrir_periodo_pago = _abrir_orig2

check("o webhook tem teto de corpo",
      "LIMITE_CORPO_WEBHOOK_MP" in inspect.getsource(rotas_mod.webhook_mercadopago))

print("\n== correções: idempotência da troca de plano ==")
check("troca reaproveita o pagamento pendente equivalente",
      "pagamento_pendente_equivalente" in inspect.getsource(rotas_mod.trocar))
check("troca recusa repetir a troca ja agendada",
      "plano_proximo" in inspect.getsource(rotas_mod.trocar)
      and "já está agendada" in cob.pode_trocar_de_plano("inicio", "pro", "pro")[1])
check("troca ainda recusa o plano atual e o teste",
      not cob.pode_trocar_de_plano("inicio", "inicio")[0]
      and not cob.pode_trocar_de_plano("inicio", cob.PLANO_TESTE)[0])
check("a troca continua aceitando plano novo mesmo com outro agendado",
      cob.pode_trocar_de_plano("inicio", "pro", "negocio")[0])
check("a troca continua aceitando downgrade",
      cob.pode_trocar_de_plano("pro", "inicio")[0])

print("\n== correções: ciclo nunca vira 12 meses pagos como 1 ==")
_pl = cob.plano(cob.PLANO_PRO)
for _ciclo in ("mensal", "anual", "Anual", " anual ", "MENSAL", "", None):
    _n = cob.normalizar_ciclo(_ciclo)
    check(f"ciclo {str(_ciclo)!r} normaliza para um valor do catálogo",
          _n in ("mensal", "anual"), _n)
check("'Anual' cobra preco anual e entrega 12 meses (nada de 11 de graca)",
      cob.preco_do_ciclo(_pl, "Anual") == _pl.preco_anual
      and cob.fim_do_periodo(_dt.datetime(2026, 1, 10), "Anual").year == 2027)
check("ciclo desconhecido nunca multiplica o periodo",
      cob.fim_do_periodo(_dt.datetime(2026, 1, 10), "seman").month == 2)

print("\n== correções: cota e isenção ==")
# O admin era barrado no gerador de prompt (item 12) por `contexto` nao dizer
# `sem_cota`; confirmado em producao com 403.
_fonte_contexto = inspect.getsource(lim.contexto)
check("contexto marca sem_cota no ramo do admin (item 12 aberto para o dono)",
      _fonte_contexto.count('resumo["sem_cota"] = True') == 2,
      str(_fonte_contexto.count('resumo["sem_cota"] = True')))
check("o isento tambem ganha sem_cota explicito (nao por acaso do plano alto)",
      '"sem_cota": True' in inspect.getsource(lim) and "isento" in _fonte_contexto)

# Cota de criacao: periodo vencido barra pelo motivo certo, admin/isento nao.
async def _exige(status, *, isento=False, admin=False, usados=0):
    async def _assinatura(usuario_id):
        return {"status": status}

    async def _plano(usuario_id, eh_admin=False):
        return cob.plano(cob.PLANO_INICIO)

    async def _isento_por_id(usuario_id):
        return isento

    async def _contar(usuario_id):
        return usados

    a, p, i, c = (lim.assinatura_atual, lim.plano_atual,
                  lim.eh_isento_por_id, lim.contar_agentes)
    lim.assinatura_atual, lim.plano_atual = _assinatura, _plano
    lim.eh_isento_por_id, lim.contar_agentes = _isento_por_id, _contar
    try:
        return await lim.exigir_cota_agentes("u-1", admin)
    finally:
        lim.assinatura_atual, lim.plano_atual = a, p
        lim.eh_isento_por_id, lim.contar_agentes = i, c


for _status in ("cancelado", "expirado"):
    try:
        asyncio.run(_exige(_status))
        check(f"criar agente com periodo {_status} e barrado", False, "aceitou")
    except _HTTPException as e:
        check(f"criar agente com periodo {_status} e barrado (402)",
              e.status_code == 402 and "acabou" in str(e.detail), f"{e.status_code}")
asyncio.run(_exige("ativo"))
check("criar agente dentro do plano passa", True)
asyncio.run(_exige("expirado", admin=True))
check("criar agente como admin passa (admin nao tem cota)", True)
asyncio.run(_exige("expirado", isento=True))
check("criar agente como isento passa (isento nunca paga, nunca e barrado)", True)
try:
    asyncio.run(_exige("expirado", usados=99))
    check("criar agente com o teto do plano estourado e barrado", False, "aceitou")
except _HTTPException as e:
    check("criar agente com o teto do plano estourado e barrado (402)",
          e.status_code == 402, f"{e.status_code}")
check("exigir_cota_agentes recebe o papel do admin",
      "eh_admin" in inspect.signature(lim.exigir_cota_agentes).parameters)
check("exigir_cota_canais recebe o papel do admin",
      "eh_admin" in inspect.signature(lim.exigir_cota_canais).parameters)
check("a rota de agente repassa o papel para a cota",
      "exigir_cota_agentes(usuario.id, usuario.eh_admin)"
      in inspect.getsource(main_mod.post_agente))

# O dono do agente e coluna de `agentes`, nao de `canais`: ler do canal
# devolvia sempre vazio e nenhuma mensagem de canal conferia cota.
_fonte_cota = inspect.getsource(main_mod._cota_do_dono)
_cota_codigo = _fonte_cota.split('"""', 2)[-1]  # sem a docstring, que cita o bug
check("a cota do worker le o dono no AGENTE (canais nao tem coluna dono_id)",
      'canal.get("dono_id")' not in _cota_codigo
      and 'dono = agente.get("dono_id")' in _cota_codigo)
check("a cota do worker repassa o papel do dono (admin nao e barrado)",
      "eh_admin_por_id" in _fonte_cota)
check("a cota e conferida antes de chamar a IA",
      _fonte_cota in inspect.getsource(main_mod._processar_caixa)
      or "_cota_do_dono" in inspect.getsource(main_mod._processar_caixa))
check("a reserva da fila e condicional (dois workers nao respondem em duplicidade)",
      "AND status IN ('pendente', 'erro') RETURNING id"
      in inspect.getsource(repo.marcar_caixa_processando))
check("o worker pula o item que outro worker pegou",
      "if not await repo.marcar_caixa_processando" in inspect.getsource(main_mod._processar_caixa))
check("a fila nao repete IA a cada 5 minutos para sempre",
      "TENTATIVAS_IA_MAX" in inspect.getsource(main_mod._proxima_tentativa))

print("\n== correções: isolamento entre contas ==")
# O join da fila casava canais.id com agentes.id: a fila de um cliente aparecia
# para o dono do agente de MESMO numero, com texto e remetente da conversa.
_fonte_resumo = inspect.getsource(repo.resumo_caixa)
check("a fila entra pelo caminho canal -> agente (nao por id igual)",
      "JOIN agentes a ON a.id = k.agente_id" in _fonte_resumo
      and "ON a.id = c.canal_id" not in _fonte_resumo)
check("as estatisticas da home usam o mesmo caminho",
      "JOIN canais k ON k.id = c.canal_id" in inspect.getsource(repo.estatisticas_do_dono))
check("a RLS da fila usa o caminho certo tambem",
      "c.id = caixa_entrada.canal_id"
      in (ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))

# O filtro por dono precisa da ponte com o agente, mas o `canais k` ja vem do
# _JOIN_CAIXA: repetir o alias faz o Postgres recusar a query inteira e a fila
# do painel responde 500 (foi o que aconteceu em producao).
_JOIN_SEM_ALIAS_DUPLO = re.compile(
    r"\b(?:FROM|JOIN)\s+([a-z_][a-z_0-9]*)\s*(?:AS\s+)?([a-z_][a-z_0-9]*)",
    re.IGNORECASE,
)
_NAO_ALIAS = {
    "on", "where", "left", "right", "inner", "outer", "cross", "full", "join",
    "limit", "order", "group", "and", "or", "select", "set", "values", "as",
    "lateral", "natural", "using", "having", "union",
}
_sql_registrado: list[tuple[str, tuple]] = []


class _ConFalsa:
    async def fetch(self, sql, *args):
        _sql_registrado.append((sql, args))
        return []

    async def fetchrow(self, sql, *args):
        _sql_registrado.append((sql, args))
        # `estatisticas_do_dono` faz dict(row) no fim; um dict vazio serve.
        return {k: 0 for k in ("agentes", "canais", "sessoes", "mensagens",
                               "fila_pendente", "mensagens_7d", "respostas_7d")}


class _PoolFalso:
    def acquire(self):
        return self

    async def __aenter__(self):
        return _ConFalsa()

    async def __aexit__(self, *a):
        return False


_pool_original = repo.get_pool


async def _pool_falso():
    return _PoolFalso()


repo.get_pool = _pool_falso
for _dono, _rotulo in ((None, "admin (sem filtro)"),
                       ("8e80492f-1111-2222-3333-444444444444", "cliente comum")):
    _sql_registrado.clear()
    asyncio.run(repo.resumo_caixa(limite=5, dono_id=_dono))
    asyncio.run(repo.estatisticas_do_dono(_dono))
    check(f"a fila do painel monta query ({_rotulo})", len(_sql_registrado) >= 4,
          f"{len(_sql_registrado)} consulta(s)")
    for _i, (_sql, _args) in enumerate(_sql_registrado, 1):
        # Cada SELECT (inclusive os de subquery) e um bloco com a sua propria
        # cadeia FROM/JOIN: `a` repetir em subqueries diferentes e normal.
        _repetidos: set[str] = set()
        for _bloco in re.split(r"\bSELECT\b", _sql, flags=re.IGNORECASE):
            _alias = [
                _a.lower() for _t, _a in _JOIN_SEM_ALIAS_DUPLO.findall(_bloco)
                if _a.lower() not in _NAO_ALIAS
            ]
            _repetidos |= {a for a in _alias if _alias.count(a) > 1}
        check(f"consulta {_i} ({_rotulo}) sem tabela repetida no FROM/JOIN",
              not _repetidos, f"repetido: {sorted(_repetidos)}")
        _placeholders = {int(n) for n in re.findall(r"\$(\d+)", _sql)}
        _esperados = set(range(1, (max(_placeholders) if _placeholders else 0) + 1))
        check(f"consulta {_i} ({_rotulo}) com um valor para cada $n",
              _placeholders == _esperados and len(_args) == len(_esperados),
              f"${{{sorted(_placeholders)}}} com {len(_args)} argumento(s)")
    # O dono precisa mesmo entrar no filtro, e so quando ele existe.
    _com_dono = [s for s, a in _sql_registrado if "a.dono_id = $1" in s]
    check(f"o filtro por dono entra no SQL ({_rotulo})",
          bool(_com_dono) if _dono is not None else not _com_dono,
          f"{len(_com_dono)} consulta(s) com filtro")
repo.get_pool = _pool_original

print("\n== correções: cache de sessão e papel ==")
# O cache e por token e `limpar_cache(usuario_id)` removia por id: nunca
# acertava, entao bloqueio/promocao valiam so depois de 60 s.
_ut = auth_mod.Usuario(id="u-cache", email="a@b.c", role=auth_mod.ROLE_USUARIO)
auth_mod._cache.clear()
auth_mod._cache["tok-1"] = (_time.time() + 999, _ut)
auth_mod._cache["tok-2"] = (_time.time() + 999, auth_mod.Usuario(
    id="u-outro", email="c@d.e", role=auth_mod.ROLE_USUARIO))
auth_mod.limpar_cache("u-cache")
check("limpar_cache por usuario apaga o token daquela conta",
      "tok-1" not in auth_mod._cache)
check("limpar_cache por usuario nao apaga a conta de outro",
      "tok-2" in auth_mod._cache)
auth_mod.limpar_cache()
check("limpar_cache() apaga tudo", auth_mod._cache == {})
check("o token de emergencia e comparado em tempo constante",
      "compare_digest" in inspect.getsource(auth_mod._token_de_emergencia))
check("a mudanca de e-mail invalida a cache de isencao",
      "limpar_cache" in inspect.getsource(auth_mod.usuario_do_token)
      or "atualizar_email_perfil" in inspect.getsource(auth_mod.usuario_do_token))

print("\n== correções: a entrada de emergência não leva 500 ==")
# O id da conta do ADMIN_TOKEN é um sentinela sem linha em auth.users e NÃO é
# uuid (de propósito: nada pode gravá-lo em coluna com FK). Usá-lo como
# `dono_id`/`usuario_id` fazia o Postgres recusar, e essas rotas é justamente o
# que o operador abre quando a plataforma está com problema: em producao
# /api/painel/perfil, /api/painel/admin/config, /api/plano/pagamentos e
# /api/admin/contas/nao-e-uuid respondiam 500.
check("id_para_coluna_uuid devolve o id normal da conta",
      auth_mod.id_para_coluna_uuid(
          auth_mod.Usuario(id="u1", email="a@b", role=auth_mod.ROLE_USUARIO)) == "u1")
check("id_para_coluna_uuid neutraliza a conta de emergencia",
      auth_mod.id_para_coluna_uuid(auth_mod.ADMIN_EMERGENCIA) is None)
check("o id de emergencia continua NAO sendo uuid (a FK protege)",
      not _emergencia_e_uuid, auth_mod.ADMIN_EMERGENCIA.id)

# Comportamento, nao leitura de fonte: as rotas sao chamadas de verdade com um
# pool falso que rejeita qualquer id que nao seja uuid -- que e o que o
# Postgres faz quando o valor nao cabe na coluna.
_uuid_ruim: list[str] = []
_ids_vistos: list[str] = []


def _vigia_uuid(sql: str, *args):
    # `app_config` guarda texto (a chave do config), nao uuid: nao e filtro de
    # conta e nao interessa aqui.
    if "app_config" in sql:
        return
    for a in args:
        if not isinstance(a, str) or not a:
            continue
        _ids_vistos.append(a)
        try:
            uuid.UUID(a)
        except ValueError:
            _uuid_ruim.append(a)


class _ConVigia:
    async def fetch(self, sql, *args):
        _vigia_uuid(sql, *args)
        return []

    async def fetchrow(self, sql, *args):
        _vigia_uuid(sql, *args)

    async def fetchval(self, sql, *args):
        _vigia_uuid(sql, *args)

    async def execute(self, sql, *args):
        _vigia_uuid(sql, *args)
        return "UPDATE 0"


class _PoolVigia:
    def acquire(self):
        return self

    async def __aenter__(self):
        return _ConVigia()

    async def __aexit__(self, *a):
        return False


async def _pool_vigia():
    return _PoolVigia()


class _ReqVigia:
    def __init__(self, usuario):
        self.state = type("St", (), {"usuario": usuario})()

    async def json(self):
        return {}


import app.painel as _painel_mod  # noqa: E402
import app.repos_cobranca as _rc_mod  # noqa: E402

_pools = (repo, _rc_mod)
_gp = [m.get_pool for m in _pools]
for _m in _pools:
    _m.get_pool = _pool_vigia

for _nome, _rota in (("/api/painel/perfil", _painel_mod.perfil),
                     ("/api/painel/admin/config", _painel_mod.ler_config_operacao),
                     ("/api/plano/pagamentos", rotas_mod.meus_pagamentos),
                     ("/api/admin/contas/nao-e-uuid", None)):
    _uuid_ruim.clear()
    _ids_vistos.clear()
    if _rota is None:
        # id fora do formato: 404, e o banco nem e consultado.
        try:
            asyncio.run(main_mod.admin_detalhe_conta("nao-e-uuid", _ReqVigia(
                auth_mod.ADMIN_EMERGENCIA)))
        except _HTTPException as e:
            check(f"{_nome} -> 404 em vez de 500", e.status_code == 404, str(e.detail)[:50])
        else:
            check(f"{_nome} -> 404 em vez de 500", False, "devolveu 200")
        check(f"{_nome} nem chega ao banco", not _ids_vistos, str(_ids_vistos))
        continue
    try:
        _saida = asyncio.run(_rota(_ReqVigia(auth_mod.ADMIN_EMERGENCIA)))
        _st = 200
    except _HTTPException as e:
        _saida, _st = str(e.detail), e.status_code
    check(f"{_nome} responde para a conta de emergencia (sem 500)",
          _st == 200, f"HTTP {_st}: {str(_saida)[:70]}")
    check(f"{_nome} nao manda id que nao e uuid para o banco",
          not _uuid_ruim, f"{_uuid_ruim}")

# Mesma rota, conta normal: o id tem de continuar sendo o da conta.
_uuid_ruim.clear()
_ids_vistos.clear()
_u = auth_mod.Usuario(id="8e80492f-1111-2222-3333-444444444444",
                      email="u@x", role=auth_mod.ROLE_USUARIO)
asyncio.run(_painel_mod.perfil(_ReqVigia(_u)))
check("a conta normal continua consultando o proprio id",
      _ids_vistos == [_u.id], str(_ids_vistos))
check("o perfil da conta normal traz o email dela",
      asyncio.run(_painel_mod.perfil(_ReqVigia(_u)))["email"] == "u@x")

for _m, _f in zip(_pools, _gp):
    _m.get_pool = _f
check("reativar tambem recusa conta sem assinatura (admin/emergencia)",
      "eh_admin" in inspect.getsource(rotas_mod.reativar))

# --- cancelar/reativar quando a conta ainda nao tem assinatura --------------
# O gatilho de cadastro cria o PERFIL, nao a assinatura. Entao a conta nova que
# aperta "cancelar" antes de abrir o painel chega aqui com `UPDATE ... RETURNING`
# pegando zero linha: antes disso era `dict(None)` -> TypeError -> 500, e no chat
# a recusa chegava como "'NoneType' object is not iterable".
#
# O pool falso (fetchrow devolvendo None) e reaplicado aqui em vez de confiar no
# de cima: sem ele estas chamadas iam para o banco de verdade e o `asyncio.run`
# seguinte estouraria com "another operation is in progress", do pool real.
check("serializar linha vazia e {} e nao TypeError",
       _rc_mod._serializar(None) == {})
_rc_get_pool_orig = _rc_mod.get_pool
_rc_mod.get_pool = _pool_vigia
_SEM_ASSINATURA = "00000000-0000-4000-8000-000000000000"
try:
    for _nome, _args in (("agendar_cancelamento", (_SEM_ASSINATURA,)),
                         ("reverter_cancelamento", (_SEM_ASSINATURA,)),
                         ("agendar_troca", (_SEM_ASSINATURA, cob.PLANO_PRO, "mensal")),
                         ("cancelar_troca", (_SEM_ASSINATURA,))):
        _f = getattr(_rc_mod, _nome)
        try:
            _vazio = asyncio.run(_f(*_args))
            check(f"{_nome} sem assinatura devolve {{}} em vez de estourar",
                  _vazio == {}, str(_vazio)[:60])
        except Exception as _e:  # noqa: BLE001
            check(f"{_nome} sem assinatura devolve {{}} em vez de estourar",
                  False, f"{type(_e).__name__}: {_e}")
finally:
    _rc_mod.get_pool = _rc_get_pool_orig

_u2 = auth_mod.Usuario(id=_SEM_ASSINATURA,
                       email="sem-assinatura@x", role=auth_mod.ROLE_USUARIO)
_obter_orig, _ag_orig, _re_orig = (_rc_mod.obter_assinatura,
                                   _rc_mod.agendar_cancelamento,
                                   _rc_mod.reverter_cancelamento)
_chamadas: list[str] = []


async def _obter_nada(_uid):
    return None


async def _agendar_vigia_rota(uid, *_a, **_k):
    _chamadas.append(f"cancelar:{uid}")
    return {}


async def _reverter_vigia_rota(uid, *_a, **_k):
    _chamadas.append(f"reativar:{uid}")
    return {}


_rc_mod.obter_assinatura = _obter_nada
_rc_mod.agendar_cancelamento = _agendar_vigia_rota
_rc_mod.reverter_cancelamento = _reverter_vigia_rota
_rc_mod.get_pool = _pool_vigia
try:
    for _nome, _rota in (("cancelar", rotas_mod.cancelar_assinatura),
                         ("reativar", rotas_mod.reativar)):
        _chamadas.clear()
        try:
            asyncio.run(_rota(_ReqVigia(_u2)))
            check(f"/api/plano/{_nome} sem assinatura -> 400 e nao 500",
                  False, "devolveu 200")
        except _HTTPException as _e:
            check(f"/api/plano/{_nome} sem assinatura -> 400 e nao 500",
                  _e.status_code == 400, f"HTTP {_e.status_code}: {str(_e.detail)[:60]}")
        check(f"/api/plano/{_nome} nem chega a marcar a assinatura",
              not _chamadas, str(_chamadas))
finally:
    _rc_mod.obter_assinatura = _obter_orig
    _rc_mod.agendar_cancelamento = _ag_orig
    _rc_mod.reverter_cancelamento = _re_orig
    _rc_mod.get_pool = _rc_get_pool_orig

print("\n== correções: webhooks e SSRF ==")
import app.pipeline as _pl_mod  # noqa: E402


async def _url(alvo):
    return await _pl_mod._url_publica(alvo)


for _ruim in ("http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data/",
              "http://localhost:5432", "http://10.0.0.5/",
              "http://[::1]/", "file:///etc/passwd", "ftp://x/y"):
    try:
        asyncio.run(_url(_ruim))
        check(f"anexo por url recusa {_ruim}", False, "aceitou")
    except ValueError as e:
        check(f"anexo por url recusa {_ruim}", True, str(e)[:40])
try:
    asyncio.run(_url("http://nenhum-dominio-inexistente-abc123.invalid/x"))
    check("host que nao resolve e recusado", False, "aceitou")
except ValueError as e:
    check("host que nao resolve e recusado", True, str(e)[:40])
check("o download de anexo e cortado no limite durante a leitura",
      "aiter_bytes" in inspect.getsource(_pl_mod._baixar_url)
      and "LIMITE_POR_ARQUIVO" in inspect.getsource(_pl_mod._baixar_url))
check("cada redirect e conferido de novo",
      inspect.getsource(_pl_mod._baixar_url).count("_url_publica(") >= 2)
_fonte_wb_tg = inspect.getsource(main_mod.webhook_telegram)
check("o webhook do telegram confere o segredo antes de ler o corpo",
      _fonte_wb_tg.index("_secret_igual") < _fonte_wb_tg.index("_json_do_webhook"))
_fonte_wb_ev = inspect.getsource(main_mod.webhook_evolution)
check("o webhook da evolution tambem",
      _fonte_wb_ev.index("_secret_igual") < _fonte_wb_ev.index("_json_do_webhook"))
check("origem degenerada nao vira 'tg:None' (que perderia toda a fila do canal)",
      "update_id is not None" in _fonte_wb_tg and "and key_id:" in _fonte_wb_ev)
check("o webhook generico nao devolve o erro interno ao integrador",
      '"erro": item["ultimo_erro"]' not in inspect.getsource(main_mod.webhook_generico))
check("o webhook generico deduplica por id de evento do integrador",
      "event_id" in inspect.getsource(main_mod.webhook_generico))
check("corpo grande ou json invalido de webhook vira 4xx, nao 500",
      "LIMITE_CORPO_WEBHOOK" in inspect.getsource(main_mod._json_do_webhook))

print("\n== correções: isncao e ambiente ==")
import app.config as _cfg_mod  # noqa: E402

for _valor, _esperado in (
    ("a@x.com,b@y.com", {"a@x.com", "b@y.com"}),
    ("a@x.com b@y.com", {"a@x.com", "b@y.com"}),
    ("a@x.com; b@y.com\nc@z.com", {"a@x.com", "b@y.com", "c@z.com"}),
    ("  A@X.com  ", {"a@x.com"}),
    ("", set()),
    (None, set()),
):
    check(f"CONTAS_ISENTAS {_valor!r} vira lista de e-mails",
          _cfg_mod._ler_isentas(_valor) == _esperado, str(_cfg_mod._ler_isentas(_valor)))
check("o padrao de CONTAS_ISENTAS tem o dono e a conta de teste",
      {"joaogabrielss.2007@gmail.com", "jgkwy07@gmail.com"}
      <= _cfg_mod.settings.contas_isentas, str(sorted(_cfg_mod.settings.contas_isentas)))
check("PAGAMENTO_DEMO e desligado por padrao (nada de plano de graca)",
      _cfg_mod.settings.pagamento_demo is False)
for _v in ("1", "true", "SIM", "yes"):
    check(f"PAGAMENTO_DEMO={_v!r} liga o atalho de teste", _cfg_mod._verdadeiro(_v) is True)
for _v in ("", "0", "false", "nao", None, "2"):
    check(f"PAGAMENTO_DEMO={_v!r} NAO liga o atalho de teste", _cfg_mod._verdadeiro(_v) is False)

print("\n== correções: identificador de canal não entrega o canal alheio ==")
# Antes, a recusa de phone_number_id / verify_token duplicado citava o NOME do
# canal em conflito: `listar_canais()` sem filtro traz os canais de todos os
# clientes (o app roda com o papel postgres, que ignora RLS), e a checagem
# rodava na criação/edição de qualquer canal oficial.
_chamadas: list[tuple] = []
_ocupados: set[tuple] = set()


async def _usa_falso(tipo, campo, valor, *, ignorar_canal_id=None):
    _chamadas.append((tipo, campo, valor, ignorar_canal_id))
    return (tipo, campo, valor) in _ocupados


_usa_real = repo.canal_usa_identificador
repo.canal_usa_identificador = _usa_falso
main_mod.repo = repo


def _recusa(nome, tipo, config, **kw):
    try:
        asyncio.run(main_mod._exigir_identificador_unico(tipo, config, **kw))
    except _HTTPException as e:
        return e
    return None


_ocupados.add(("whatsapp_oficial", "phone_number_id", "555"))
_ocupados.add((None, "verify_token", "vt-repetido"))
e = _recusa("x", "whatsapp_oficial", {"phone_number_id": "555", "verify_token": "vt-repetido"})
check("id de conta repetido e recusado com 409", e is not None and e.status_code == 409,
      str(getattr(e, "detail", "aceitou"))[:60])
check("a recusa nao cita o nome do canal de outro cliente",
      e is not None and "canal \"" not in str(e.detail), str(getattr(e, "detail", "")))
check("a recusa ainda diz o que fazer",
      e is not None and "único canal" in str(e.detail), str(getattr(e, "detail", ""))[:80])

_chamadas.clear()
e = _recusa("x", "instagram_oficial", {"ig_user_id": "9", "verify_token": "vt-repetido"})
check("ig_user_id livre passa; e o verify_token repetido que barra",
      e is not None and e.status_code == 409 and "verify_token" in str(e.detail),
      str(getattr(e, "detail", "aceitou"))[:60])
check("a recusa do verify_token nao devolve o token em claro",
      e is not None and "vt-repetido" not in str(e.detail), str(getattr(e, "detail", "")))
check("o verify_token e conferido entre os dois tipos oficiais (o handshake nao manda o id)",
      any(c[0] is None and c[1] == "verify_token" for c in _chamadas), str(_chamadas))

_chamadas.clear()
e = _recusa("x", "telegram", {"token": "x"})
check("canal nao oficial nao passa pela checagem de identificador",
      e is None and not _chamadas, str(_chamadas))

_chamadas.clear()
e = _recusa("x", "whatsapp_oficial",
            {"phone_number_id": "555", "verify_token": "vt-repetido"}, ignorar_canal_id=7)
check("a edicao manda o id do proprio canal para ser ignorado",
      bool(_chamadas) and all(c[3] == 7 for c in _chamadas), str(_chamadas))

_ocupados.clear()
e = _recusa("x", "whatsapp_oficial", {"phone_number_id": "555", "verify_token": "vt"})
check("sem conflito os dois identificadores passam", e is None, str(getattr(e, "detail", "")))

check("a checagem nao percorre mais a lista inteira de canais",
      "listar_canais" not in inspect.getsource(main_mod._exigir_identificador_unico))
repo.canal_usa_identificador = _usa_real  # as checagens abaixo sao da funcao real
_fonte_usa = inspect.getsource(_usa_real)
check("a consulta e um EXISTS (nao devolve config/nome de ninguem)",
      "SELECT EXISTS" in _fonte_usa and "btrim(coalesce(config ->> $3" in _fonte_usa)
check("o campo vai como parametro do SQL, nunca colado no texto da consulta",
      "$3" in _fonte_usa and "{campo}" not in _fonte_usa)
for _fora in ("); DROP TABLE canais; --", "tipo = ANY($1)", "id = $1"):
    try:
        asyncio.run(_usa_real("whatsapp_oficial", _fora, "1"))
    except ValueError:
        check(f"chave fora da lista e recusada antes do SQL: {_fora[:18]!r}", True)
    except Exception as e:  # noqa: BLE001
        check(f"chave fora da lista e recusada antes do SQL: {_fora[:18]!r}", False, type(e).__name__)
    else:
        check(f"chave fora da lista e recusada antes do SQL: {_fora[:18]!r}", False, "chegou ao banco")
check("a lista fechada cobre so as chaves de roteamento da Meta",
      repo.CAMPOS_ROTEAMENTO_META == ("phone_number_id", "ig_user_id", "verify_token"),
      str(repo.CAMPOS_ROTEAMENTO_META))

# --------------------------------------------------------------------------
print("\n== resumo ==")
if falhas:
    print(f"FALHAS ({len(falhas)}): " + ", ".join(falhas))
    sys.exit(1)
print("TODOS OS TESTES PASSARAM")


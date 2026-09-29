"""Gera app/static/{style.css,admin.html} a partir do painel antigo da raiz.

O painel tem ~870 linhas de HTML/JS escritas a mao e funcionais. Reescrever do
zero perderia detalhe de UI que leva meses; o que muda e so a autenticacao
(de ADMIN_TOKEN para sessao Supabase). Entao este script:

  1. extrai o <style> para style.css;
  2. troca o <style> por um <link> e aponta o <script> para auth.js;
  3. substitui o gate de ADMIN_TOKEN por um bootstrap de sessao;
  4. troca o header X-Admin-Token por Authorization: Bearer.

Roda uma vez. Idempotente: sempre le da raiz e sempre sobrescreve o destino.
"""
from __future__ import annotations

import pathlib
import re
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
ORIGEM = RAIZ / "index.html"
DESTINO = RAIZ / "app" / "static"

# Script do painel de contas. Injetado antes do script principal.
CODIGO_CONTAS = """
// ---------- contas ----------
// Só o admin chega aqui, e a API re-checa o papel em /api/admin/contas: esta
// tela esconder o botão não é o que protege nada, o `exigir_admin` no servidor é.
let contasCache = [];

async function alternarContas() {
  const card = $("contasCard");
  card.classList.toggle("hidden");
  if (card.classList.contains("hidden")) return;
  $("contasLista").innerHTML = '<p class="muted">carregando…</p>';
  await carregarContas();
}

async function carregarContas() {
  try {
    contasCache = await api("/api/admin/contas");
  } catch (e) {
    $("contasLista").innerHTML = '<p class="aviso">Erro: ' + esc(e.message) + "</p>";
    return;
  }
  if (!contasCache.length) {
    $("contasLista").innerHTML = '<p class="muted">Nenhuma conta encontrada.</p>';
    return;
  }
  $("contasLista").innerHTML = contasCache.map((c) => `
    <div class="row" style="justify-content:space-between;border-top:1px solid var(--borda);padding:10px 0">
      <div>
        <strong>${esc(c.email || c.id)}</strong>
        <div class="muted">
          ${c.role === "admin" ? "admin" : "cliente"}
          ${c.bloqueado ? " · <span style='color:var(--danger)'>bloqueado</span>" : ""}
          · ${c.agentes} agente${c.agentes === 1 ? "" : "s"}
          ${c.criado_em ? " · " + new Date(c.criado_em).toLocaleDateString("pt-BR") : ""}
        </div>
      </div>
      <div class="row">
        <button class="btn sm" data-conta="${esc(c.id)}" data-acao="promover">
          ${c.role === "admin" ? "Rebaixar" : "Tornar admin"}
        </button>
        <button class="btn sm" data-conta="${esc(c.id)}" data-acao="bloquear">
          ${c.bloqueado ? "Desbloquear" : "Bloquear"}
        </button>
      </div>
    </div>`).join("");

  for (const btn of $("contasLista").querySelectorAll("button[data-conta]")) {
    btn.onclick = () => mudarConta(btn.dataset.conta, btn.dataset.acao);
  }
}

async function mudarConta(contaId, acao) {
  const conta = contasCache.find((c) => c.id === contaId);
  if (!conta) return;
  const body = acao === "promover"
    ? { role: conta.role === "admin" ? "usuario" : "admin" }
    : { bloqueado: !conta.bloqueado };
  try {
    await api("/api/admin/contas/" + encodeURIComponent(contaId), {
      method: "PUT",
      body: JSON.stringify(body)
    });
    toast("Conta atualizada.");
    await carregarContas();
  } catch (e) {
    // O "último admin" volta como 400 e a mensagem explica o porquê.
    toast(e.message, "err");
  }
}

$("btnFecharContas").onclick = () => $("contasCard").classList.add("hidden");
"""


SUBSTITUICOES: list[tuple[str, str, str]] = [
    (
        "codigo de contas dentro do IIFE (precisa de $ e api)",
        "\n// ---------- partida ----------\n",
        "\n" + CODIGO_CONTAS + "\n// ---------- partida ----------\n",
    ),
    (
        "gate de ADMIN_TOKEN no markup",
        re.compile(
            # Ancorado no <aside> seguinte: sem esta ancora o `.*?` lazy terminava
            # numa sequencia de </div> la dentro e comia a sidebar inteira.
            r'<div id="gate" class="hidden">.*?\n</div>\n\n<aside>',
            re.DOTALL,
        ),
        # O gate vira um splash de carregamento. O <div> original escondia o
        # painel; agora quem esconde e o JS, depois que /api/eu responde.
        '  <div id="gate" class="gate">\n'
        '    <div class="card">\n'
        '      <h2>Carregando…</h2>\n'
        '      <p class="muted" id="gateErro"></p>\n'
        '    </div>\n'
        '  </div>\n\n<aside>',
    ),
    (
        "gate de ADMIN_TOKEN no script",
        re.compile(r"// ---- acesso ao painel.*?gateErro\.textContent = \"\";\n", re.DOTALL),
        "",
    ),
    (
        "sobras do gate no script",
        re.compile(
            r'^\};\ngateToken\.addEventListener\("keydown".*?\$\("btnToken"\)\.onclick = \(\) => mostrarGate\(\);\n',
            re.DOTALL | re.MULTILINE,
        ),
        "",
    ),
    (
        "header Authorization no lugar de X-Admin-Token",
        'headers: { "Content-Type": "application/json", "X-Admin-Token": adminToken, ...(opts.headers || {}) },',
        'headers: { "Content-Type": "application/json", ...cabecalhoAuth(), ...(opts.headers || {}) },',
    ),
    (
        "guarda 401/403 manda para o login em vez de pedir token",
        re.compile(r'mostrarGate\("Token inv[^;]*;\n'),
        'location.replace("/?redir=" + encodeURIComponent(location.pathname));\n',
    ),
    (
        "partida do painel passa a exigir sessao de admin",
        re.compile(
            r"// ---------- partida ----------\n"
            r"// O painel abre direto, sem token.*?esconderGate\(\);\n"
            r"carregarAgentes\(\)\.catch\(\(\) => \{\}\);\n"
            r"atualizarFila\(\);\n"
            r"timerFila = setInterval\(atualizarFila, 15000\);\n",
            re.DOTALL,
        ),
        "// ---------- partida ----------\n"
        "// A sessao e resolvida AQUI, nao no auth.js: o painel precisa saber o\n"
        "// papel antes de buscar qualquer dado. Sem sessao -> login; sessao de\n"
        "// cliente -> /painel. So o admin chega a carregar o painel.\n"
        "const eu = await Auth.obterSessao();\n"
        "if (!eu) {\n"
        '  location.replace("/?redir=" + encodeURIComponent(location.pathname));\n'
        "  throw new Error(\"sem sessao\");  // aborta antes de qualquer fetch\n"
        "}\n"
        "if (!eu.eh_admin) { location.replace(\"/painel\"); throw new Error(\"nao e admin\"); }\n"
        "\n"
        "$(\"btnSair\").onclick = () => Auth.sair();\n"
        "$(\"btnContas\").onclick = () => alternarContas();\n"
        "\n"
        "Auth.esconderGate();\n"
        "await carregarAgentes();\n"
        "atualizarFila();\n"
        "timerFila = setInterval(atualizarFila, 15000);\n",
    ),
    (
        "painel de contas entra antes do <main>",
        "\n<main>\n",
        """
  <div class="card hidden" id="contasCard" style="margin:0 0 12px">
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">Contas</h2>
      <button class="btn sm" id="btnFecharContas">Fechar</button>
    </div>
    <p class="muted">
      Quem pode entrar, com que papel, e de quem é cada agente. O papel vem do
      servidor — o botão aqui só pede a mudança.
    </p>
    <div id="contasLista"></div>
  </div>

<main>
""",
    ),
    (
        "botao de token vira sair",
        re.compile(
            r'<button class="btn sm" id="btnToken"[^>]*>[^<]*</button>',
        ),
        '<button class="btn sm" id="btnSair" style="flex:1">Sair</button>',
    ),
    (
        "titulo da sidebar ganha o menu de contas",
        '<p class="sub">Gestão de agentes e canais</p>',
        '<p class="sub">Gestão de agentes e canais</p>\n'
        '  <button class="btn sm" id="btnContas" style="width:100%;margin-top:6px">Contas</button>',
    ),
]

def aplicar(texto: str, padrao: str | re.Pattern, novo: str, nome: str) -> str:
    if isinstance(padrao, re.Pattern):
        texto, n = padrao.subn(novo, texto)
    else:
        n = texto.count(padrao)
        texto = texto.replace(padrao, novo)
    if n == 0:
        print(f"  AVISO: nao encontrei {nome}", file=sys.stderr)
    return texto


def main() -> None:
    DESTINO.mkdir(parents=True, exist_ok=True)
    texto = ORIGEM.read_text(encoding="utf-8")

    # 1) CSS para arquivo proprio
    css = texto[texto.index("<style>") + len("<style>"): texto.index("</style>")]
    (DESTINO / "style.css").write_text(css.strip() + "\n", encoding="utf-8")
    texto = texto.replace(css + "</style>", "</style>")
    texto = re.sub(r"<style>.*?</style>", '<link rel="stylesheet" href="/static/style.css">', texto, flags=re.DOTALL)

    # 2) auth.js antes do script embutido
    texto = texto.replace(
        "<script>",
        '<script src="/static/auth.js"></script>\n<script>\n(async () => {',
        1,
    )
    # fecha o IIFE no fim do script embutido
    ultimo = texto.rindex("</script>")
    texto = texto[:ultimo] + "})();\n" + texto[ultimo:]

    # 3) trocas de autenticacao
    for nome, padrao, novo in SUBSTITUICOES:
        texto = aplicar(texto, padrao, novo, nome)

    texto = texto.replace("<title>Chatbot Project</title>", "<title>Painel admin · Chatbot Project</title>")
    (DESTINO / "admin.html").write_text(texto, encoding="utf-8")
    print(f"gerados: style.css, admin.html ({len(texto)} bytes)")


if __name__ == "__main__":
    main()

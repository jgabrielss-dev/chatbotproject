"""Mede o layout e o tema das telas no navegador de verdade (CDP do Chrome/Edge).

Por que um script e nao um teste de texto: o item 1 era um bug VISUAL. O
`body.painel { grid-template-columns: 1fr }` nao neutralizava o `display: flex`
do body porque grid e flex sao propriedades diferentes — a declaracao era
inerte, nenhuma checagem de texto acusaria, e a tela saia com topo, conteudo e
botoes deitados de lado. A unica forma de travar isso e perguntar ao
navegador onde cada caixa caiu de verdade.

O item 3 tem a mesma razao: "o escuro e o negativo do claro" so se prova
ligando o navegador em cada preferencia de sistema e comparando o que foi
pintado. Por isso este script mede os DOIS temas, via
`Emulation.setEmulatedMedia`, e nao aceita o que o CSS declara.

    python scripts/medir_layout.py            # so o relatorio
    python scripts/medir_layout.py --falha    # sai != 0 se algo estiver errado

Nao usa rede e nao le nenhum segredo: sobe um servidor local que serve os
arquivos estaticos do projeto e responde as rotas /api/* com JSON de mentira.
A sessao e semeada no localStorage da aba de teste.
"""
from __future__ import annotations

import json
import os
import pathlib
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

RAIZ = pathlib.Path(__file__).resolve().parent.parent

# display do body por tela. So o admin tem sidebar, entao so ele e flex; as
# outras tres sao coluna — e foi exatamente por isso que o painel foi achado.
ESPERA_DISPLAY = {"admin.html": "flex"}
PADRAO_DISPLAY = "block"

TELAS = ["index.html", "login.html", "painel.html", "admin.html"]

NAVEGADORES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


# ==========================================================================
# servidor de mentira


EU_CLIENTE = {
    "id": "00000000-0000-4000-8000-000000000001",
    "email": "cliente@exemplo.com.br",
    "nome": "Cliente de Teste",
    "eh_admin": False,
    "papel": "cliente",
}
EU_ADMIN = {**EU_CLIENTE, "email": "admin@exemplo.com.br", "eh_admin": True, "papel": "admin"}

AGENTES = [
    {"id": f"a{i}", "nome": f"Agente {i}", "prompt": "instrucao", "ativo": True,
     "sessoes": 3 + i, "canais": 1, "criado_em": "2026-01-01T00:00:00Z"}
    for i in range(1, 4)
]
SESSOES = [
    {"id": f"s{i}", "canal": "Telegram", "remetente": f"Cliente {i}",
     "ultima": f"2026-01-0{i}T00:00:00Z", "resumo": "falou sobre orcamento", "n": 4}
    for i in range(1, 4)
]
MENSAGENS = [
    {"de": "user", "texto": "oi, quero um orcamento", "em": "2026-01-01 10:00"},
    {"de": "ai", "texto": "Claro! Me diga o que voce precisa.", "em": "2026-01-01 10:00"},
]
CANAIS = [
    {"id": f"c{i}", "tipo": "telegram", "nome": f"Canal {i}", "ativo": True,
     "agente_id": "a1", "config": {}, "saida": ""}
    for i in range(1, 3)
]
CONTAS = [
    {**EU_CLIENTE, "criado_em": "2026-01-01T00:00:00Z", "bloqueado": False},
    {**EU_ADMIN, "criado_em": "2025-12-01T00:00:00Z", "bloqueado": False},
]
CAIXA = {"contagem": {"ok": 61, "erro": 0, "andamento": 0}, "recentes": [], "problemas": []}

ROTAS = {
    "/api/config": {"supabase_url": "https://exemplo.supabase.co",
                    "supabase_anon_key": "chave-publica-de-mentira",
                    "auth_habilitado": True},
    "/api/painel/resumo": {
        "estatisticas": {"agentes": 3, "canais": 2, "sessoes": 7, "mensagens": 61,
                         "respostas_7d": 58, "fila_pendente": 0},
        "caixa": CAIXA,
    },
    "/api/painel/agentes": AGENTES,
    "/api/painel/canais": CANAIS,
    "/api/painel/canais/tipos": {
        "admin": False,
        "tipos": [
            {"id": "telegram", "nome": "Telegram", "resumo": "mock",
             "oficial": False,
             "campos": [{"chave": "token", "rotulo": "Token", "tipo": "password",
                         "obrigatorio": True, "placeholder": "", "ajuda": ""}],
             "como_usar": ["fale com o BotFather"],
             "depois_de_salvar": "Clique em registrar webhook."},
            {"id": "webhook", "nome": "Webhook (API)", "resumo": "mock",
             "oficial": False, "campos": [], "como_usar": [], "depois_de_salvar": ""},
        ],
    },
    "/api/painel/perfil": {"id": "u1", "nome": "Ana Cliente", "email": "ana@teste",
                           "role": "usuario", "bloqueado": False, "mfa_ativo": False,
                           "criado_em": None},
    "/api/plano": {
        "plano": {"id": "pro", "nome": "Pro", "descricao": "d", "preco_mensal": 200.0,
                  "preco_anual": 1920.0, "max_agentes": 5, "max_canais_por_agente": 4,
                  "max_mensagens_por_agente_mes": 5000, "dias_gratis": 0,
                  "destaque": True, "sem": [], "preco_por_mil_mensagens": 7.96,
                  "preco_por_agente_mes": 40.0, "desconto_anual": 480.0},
        "status": "ativo", "ciclo": "mensal", "inicio_periodo": "2026-09-01T00:00:00+00:00",
        "fim_periodo": "2026-10-01T00:00:00+00:00", "em_teste": False, "dias_restantes": 2,
        "agentes": {"usados": 3, "limite": 5},
        "canais_por_agente": {"limite": 4},
        "consumo": [{"agente_id": 1, "mensagens": 1200, "limite": 5000, "restante": 3800,
                     "excedeu": False},
                    {"agente_id": 2, "mensagens": 5200, "limite": 5000, "restante": 0,
                     "excedeu": True}],
        "consumo_total": 6400, "limite_total": 25000,
        "troca_agendada": None, "cancelamento_agendado": None, "economia_anual": 480.0,
        "catalogo": [
            {"id": "inicio", "nome": "Inicio", "descricao": "", "preco_mensal": 80.0,
             "preco_anual": 768.0, "max_agentes": 2, "max_canais_por_agente": 2,
             "max_mensagens_por_agente_mes": 1000, "dias_gratis": 0, "destaque": False,
             "sem": [], "preco_por_mil_mensagens": 39.5, "preco_por_agente_mes": 40.0,
             "desconto_anual": 192.0},
            {"id": "pro", "nome": "Pro", "descricao": "", "preco_mensal": 200.0,
             "preco_anual": 1920.0, "max_agentes": 5, "max_canais_por_agente": 4,
             "max_mensagens_por_agente_mes": 5000, "dias_gratis": 0, "destaque": True,
             "sem": [], "preco_por_mil_mensagens": 7.96, "preco_por_agente_mes": 40.0,
             "desconto_anual": 480.0},
        ],
        "mensagens_excedentes": [{"ate": 1000, "preco_por_mil": 12.0}],
    },
    "/api/plano/limites": {"sem_cota": False, "pode_criar_agente": True,
                           "agentes_usados": 3, "max_agentes": 5,
                           "max_canais_por_agente": 4,
                           "max_mensagens_por_agente_mes": 5000},
    "/api/plano/pagamentos": [
        {"id": 1, "usuario_id": "u1", "plano_id": "inicio", "ciclo": "mensal",
         "valor": 80.0, "status": "pago", "metodo": "", "referencia": "demo-1",
         "criado_em": "2026-09-01T00:00:00+00:00", "pago_em": "2026-09-01T00:00:00+00:00",
         "aplicado_em": "2026-09-01T00:00:00+00:00"},
        {"id": 2, "usuario_id": "u1", "plano_id": "pro", "ciclo": "anual",
         "valor": 1920.0, "status": "pendente", "metodo": "", "referencia": "",
         "criado_em": "2026-09-20T00:00:00+00:00", "pago_em": None, "aplicado_em": None},
    ],
    "/api/admin/contas": CONTAS,
    "/api/admin/config": {"nome": "Ana Admin", "email": "admin@teste",
                          "email_atendimento": "suporte@empresa.com",
                          "mfa_ativo": False},
    "/api/agentes": AGENTES,
    "/api/canais": CANAIS,
    "/api/caixa": CAIXA,
    "/api/sessoes": SESSOES,
    "/api/mensagens": MENSAGENS,
}

# Token semeado no localStorage. O de admin existe porque o admin.html exige
# papel: com /api/eu respondendo "cliente" ele joga a gente para /painel e a
# medicao viraria do painel sem querer.
TOKEN_ADMIN = "sessao-falsa-admin"
TOKEN_CLIENTE = "sessao-falsa-cliente"


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(RAIZ), **kw)

    def log_message(self, *a):
        pass

    def _json(self, dados, code=200):
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(corpo)

    # No Render o FastAPI serve /painel e /admin sem ".html"; o Pages so tem
    # arquivos. Servir os dois deixa a medicao util para os dois hosts.
    _SEM_EXT = {"admin": "admin.html", "painel": "painel.html",
                "login": "login.html", "": "index.html"}

    def do_GET(self):
        rota = self.path.split("?")[0]
        destino = self._SEM_EXT.get(rota.strip("/"))
        if destino and (RAIZ / destino).exists():
            self.path = "/" + destino
            return super().do_GET()
        if rota == "/api/eu":
            cab = self.headers.get("Authorization", "")
            if TOKEN_ADMIN not in cab and TOKEN_CLIENTE not in cab:
                return self._json({"detail": "sem sessao"}, 401)
            return self._json(EU_ADMIN if TOKEN_ADMIN in cab else EU_CLIENTE)
        if rota in ROTAS:
            return self._json(ROTAS[rota])
        # Rotas com id no caminho. Os ids do mock sao "a1".."a3", entao fixar
        # "/api/painel/agentes/1/canais" em ROTAS nunca bateria -- e o `{}` do
        # fallback abaixo era lido como lista, produzindo "undefined de 4
        # canais" na tela. Foi assim que a checagem achou um bug real.
        m = re.match(r"^/api/painel/agentes/([^/]+)/canais$", rota)
        if m:
            return self._json([c for c in CANAIS if c.get("agente_id") == m.group(1)])
        if rota.startswith("/api/"):
            return self._json({})
        return super().do_GET()

    def do_POST(self):
        # O chat interno (itens 7/12/13) ganha resposta de mentira COM DELAY:
        # e o que faz o "digitando..." (item 8) ficar na tela tempo suficiente
        # para a medicao provar que ele aparece e some quando a resposta chega.
        #
        # Cada agente devolve o que a medicao precisa observar:
        #   * o gerador de prompt (item 12) devolve prompt_gerado, para provar
        #     que o botao "Usar no campo de instrucao" aparece e funciona;
        #   * o suporte (item 13) devolve a acao de mudar de plano, para provar
        #     que as acoes ganham o atalho "Ver meus pagamentos" no painel;
        #   * os demais devolvem resposta comum.
        rota = self.path.split("?")[0]
        if rota.startswith("/api/interno/"):
            time.sleep(0.35)
            if rota.startswith("/api/interno/prompt"):
                return self._json({"resposta": "Prompt montado.",
                                   "prompt_gerado": "Voce e um vendedor de pizza.",
                                   "acao": None})
            if rota.startswith("/api/interno/suporte"):
                return self._json({"resposta": "Resposta do atendente.",
                                   "acao": {"nome": "mudar_plano", "dados": {}}})
            return self._json({"resposta": "Resposta do atendente.", "acao": None})
        self._json({"ok": True})

    def do_PUT(self):
        """O painel usa PUT para o perfil e para o 2FA (item 5). Sem este metodo
        o mock devolvia 501 e a tela acusava um erro que so existe no teste."""
        self._json({"ok": True})

    def do_DELETE(self):
        self._json({"ok": True})


def _porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def subir_servidor() -> tuple[ThreadingHTTPServer, int]:
    porta = _porta_livre()
    srv = ThreadingHTTPServer(("127.0.0.1", porta), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, porta


# ==========================================================================
# WebSocket minimo, para nao depender de nenhum pacote externo


class _Ws:
    """Cliente de WebSocket do tamanho do necessario: so texto, so do cliente
    para o servidor, e so o que o CDP usa."""

    def __init__(self, url: str):
        from urllib.parse import urlparse
        u = urlparse(url)
        self._sock = socket.create_connection((u.hostname, u.port or 80), timeout=20)
        self._buf = bytearray()
        self._msg: "queue.Queue[str]" = queue.Queue()
        self._sock.sendall((
            f"GET {u.path} HTTP/1.1\r\n"
            f"Host: {u.hostname}:{u.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: AAAAAAAAAAAAAAAAAAAAAA==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        while b"\r\n\r\n" not in self._buf:
            self._buf += self._sock.recv(4096)
        if b" 101 " not in self._buf.split(b"\r\n", 1)[0]:
            raise RuntimeError("handshake do WebSocket recusado")
        self._buf = self._buf.split(b"\r\n\r\n", 1)[1]
        threading.Thread(target=self._ler, daemon=True).start()

    def _ler(self):
        while True:
            try:
                self._msg.put(self._quadro().decode("utf-8"))
            except Exception:
                return

    def _exatamente(self, n: int) -> bytes:
        while len(self._buf) < n:
            pedaco = self._sock.recv(65536)
            if not pedaco:
                raise RuntimeError("WebSocket fechado pelo navegador")
            self._buf += pedaco
        saida, self._buf = bytes(self._buf[:n]), self._buf[n:]
        return saida

    def _quadro(self) -> bytes:
        b0, b1 = self._exatamente(2)
        opcode = b0 & 0x0F
        n = b1 & 0x7F
        if n == 126:
            n = int.from_bytes(self._exatamente(2), "big")
        elif n == 127:
            n = int.from_bytes(self._exatamente(8), "big")
        dados = self._exatamente(n)
        if opcode == 0x8:  # close
            raise RuntimeError("WebSocket fechado pelo navegador")
        if opcode != 0x1:
            raise RuntimeError(f"opcode {opcode} nao suportado (esperado texto)")
        return dados

    def send(self, texto: str) -> None:
        dados = texto.encode("utf-8")
        mascara = os.urandom(4)
        n = len(dados)
        cab = bytes([0x81])
        if n < 126:
            cab += bytes([0x80 | n])
        elif n < 1 << 16:
            cab += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            cab += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        self._sock.sendall(cab + mascara
                           + bytes(b ^ mascara[i % 4] for i, b in enumerate(dados)))

    def receive(self, timeout: float = 20.0) -> str:
        return self._msg.get(timeout=timeout)

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


class _Cdp:
    """Fala CDP por cima do _Ws, com uma thread por request."""

    def __init__(self, ws_url: str):
        self._ws = _Ws(ws_url)
        self._id = 0
        self._respostas: dict[int, dict] = {}
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            try:
                m = json.loads(self._ws.receive())
            except Exception:
                return
            if m.get("id") in self._respostas:
                self._respostas[m["id"]] = m

    def send(self, metodo: str, params: dict | None = None) -> dict:
        self._id += 1
        alvo = self._id
        self._respostas[alvo] = {}
        self._ws.send(json.dumps({"id": alvo, "method": metodo, "params": params or {}}))
        fim = time.time() + 25
        while time.time() < fim:
            r = self._respostas.get(alvo)
            if r:
                self._respostas.pop(alvo, None)
                if "error" in r:
                    raise RuntimeError(f"{metodo}: {r['error']}")
                return r.get("result", {})
            time.sleep(0.01)
        raise RuntimeError(f"timeout esperando {metodo}")

    def avaliar(self, expr: str):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            det = r["exceptionDetails"]
            raise RuntimeError(det.get("text", "erro") + " " +
                               str(det.get("exception", {}).get("description", "")))
        return r.get("result", {}).get("value")

    def close(self):
        self._ws.close()


def _http_json(porta: int, caminho: str, metodo: str = "GET") -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{porta}{caminho}", method=metodo)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ==========================================================================
# a medicao


SONDAGEM = r"""
(() => {
  const cs = getComputedStyle(document.body);
  const campo = document.querySelector(
    'input[type=text], input[type=email], input[type=password], textarea, select');
  const botao = document.querySelector('.btn, .btn.primary');
  const filhos = [...document.querySelectorAll('body > *')]
    .map((e) => {
      const b = e.getBoundingClientRect();
      return {tag: e.tagName.toLowerCase() + (e.id ? '#' + e.id : ''),
              x: Math.round(b.x), y: Math.round(b.y),
              w: Math.round(b.width), h: Math.round(b.height)};
    })
    .filter((b) => b.w > 0);
  return {
    display: cs.display, dir: cs.flexDirection, colorScheme: cs.colorScheme,
    fundo: cs.backgroundColor, texto: cs.color,
    campo: campo ? getComputedStyle(campo).backgroundColor : null,
    botao: botao ? [getComputedStyle(botao).backgroundColor,
                    getComputedStyle(botao).color] : null,
    filhos: filhos,
  };
})()
"""


def _rgb(css: str | None) -> list[int] | None:
    if not css:
        return None
    nums = [int(x) for x in re.findall(r"\d+", css)]
    return nums[:3] if len(nums) >= 3 else None


def _negativo(claro: list[int] | None, escuro: list[int] | None) -> bool:
    return bool(claro and escuro and all(255 - a == b for a, b in zip(claro, escuro)))


def _lum(c: list[int]) -> float:
    return (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) / 255


def _medir_tela(base: str, tela: str, porta_debug: int) -> list[str]:
    problemas: list[str] = []
    alvo = _http_json(porta_debug, "/json/new?about:blank", metodo="PUT")
    cdp = _Cdp(alvo["webSocketDebuggerUrl"])
    try:
        cdp.send("Runtime.enable")
        cdp.send("Page.enable")
        cdp.send("Log.enable")
        # A sessao e semeada ANTES de qualquer script da pagina rodar. Se
        # esperassemos a pagina carregar para semear, o auth.js do
        # carregamento anterior ja teria lido o token velho e redirecionado —
        # foi assim que a primeira versao mediu a tela errada e passou.
        token = (TOKEN_ADMIN if "admin" in tela
                 else TOKEN_CLIENTE if "painel" in tela else None)
        if token:
            fonte = (f'try {{ localStorage.setItem("chatbotproject.usuario",'
                     f' {json.dumps(token)}); }} catch (e) {{}}')
        else:
            fonte = 'try { localStorage.removeItem("chatbotproject.usuario"); } catch (e) {}'
        cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": fonte})
        cdp.send("Page.navigate", {"url": f"{base}/{tela}"})
        time.sleep(2.5)  # deixa /api/eu responder e o gate sair de cena

        onde = cdp.avaliar("location.pathname")
        if not onde.endswith(tela):
            print(f"--- {tela}: caiu em {onde}  <-- medindo a tela errada!")
            problemas.append(f"{tela}: o navegador caiu em {onde}")
            return problemas
        print(f"--- {tela}")

        por_tema: dict[str, dict] = {}
        for tema in ("light", "dark"):
            cdp.send("Emulation.setEmulatedMedia",
                     {"features": [{"name": "prefers-color-scheme", "value": tema}]})
            time.sleep(0.3)
            m = cdp.avaliar(SONDAGEM)
            por_tema[tema] = m
            print(f"    {tema:5} display={m['display']:5} scheme={m['colorScheme']:5} "
                  f"fundo={m['fundo']:18} texto={m['texto']:18} campo={m['campo']} "
                  f"botao={m['botao']}")
            if m["colorScheme"] != tema:
                problemas.append(
                    f"{tela}/{tema}: o navegador ficou com color-scheme {m['colorScheme']}")
            if m["botao"]:
                fb, ft = _rgb(m["botao"][0]), _rgb(m["botao"][1])
                if fb and ft and abs(_lum(fb) - _lum(ft)) < 0.45:
                    problemas.append(f"{tela}/{tema}: botao sem contraste {m['botao']}")
        cdp.send("Emulation.setEmulatedMedia", {"features": []})

        # "O escuro e o negativo do claro" e uma afirmacao sobre o PAR de temas.
        # Conferir 255 - fundo == texto DENTRO de um tema reprovaria um tema
        # correto: fundo branco com texto quase preto e o par de fundo preto
        # com texto quase branco. O que tem de valer e a complementaridade.
        for campo in ("fundo", "texto"):
            if not _negativo(_rgb(por_tema["light"][campo]), _rgb(por_tema["dark"][campo])):
                problemas.append(
                    f"{tela}: {campo} nao e negativo entre os temas "
                    f"(claro {por_tema['light'][campo]} -> escuro {por_tema['dark'][campo]})")
        if por_tema["light"]["campo"] and por_tema["light"]["campo"] == por_tema["dark"]["campo"]:
            problemas.append(f"{tela}: campo de formulario com a MESMA cor nos dois temas")

        # Item 1: o body so pode ser flex na tela que tem sidebar.
        esperado = ESPERA_DISPLAY.get(tela, PADRAO_DISPLAY)
        if por_tema["light"]["display"] != esperado:
            problemas.append(
                f"{tela}: body display={por_tema['light']['display']}, "
                f"esperado {esperado} (o flex do painel/admin nao foi neutralizado)")

        if tela == "index.html":
            problemas += _conferir_chat_da_home(cdp)

        if tela == "painel.html":
            problemas += _conferir_abas_do_painel(cdp)
            problemas += _conferir_gerador_e_suporte_painel(cdp)

        if tela == "admin.html":
            problemas += _conferir_perfil_do_admin(cdp)
            problemas += _conferir_suporte_admin(cdp)

    finally:
        cdp.close()
    return problemas


def _conferir_perfil_do_admin(cdp: _Cdp) -> list[str]:
    """Aba de perfil do admin (item 10).

    O admin.html não tem abas de verdade: tem botões na sidebar que abrem cards
    inline. Então "abriu a aba" aqui significa "o card deixou de estar
    escondido" — e o defeito que a medicao procura é o de dois cards abertos ao
    mesmo tempo, que a pessoa não vê como defeito: ela só acha a tela confusa.
    """
    problemas: list[str] = []

    def estado(card: str) -> dict:
        return cdp.avaliar(
            "(() => { const c = document.getElementById(%s);"
            " return { escondido: c.classList.contains('hidden'),"
            " altura: c.getBoundingClientRect().height };"
            " })()" % json.dumps(card))

    antes = {c: estado(c) for c in ("contasCard", "perfilCard")}

    cdp.avaliar("document.getElementById('btnPerfil').click(); true")
    time.sleep(0.9)
    depois = {c: estado(c) for c in ("contasCard", "perfilCard")}

    if depois["perfilCard"]["escondido"]:
        problemas.append("admin: o botao Perfil nao abriu o card de perfil")
    if (depois["perfilCard"]["altura"] or 0) < 40:
        problemas.append("admin: o card de perfil abriu com altura "
                         f"{depois['perfilCard']['altura']}")
    if not depois["contasCard"]["escondido"]:
        problemas.append("admin: o card de contas continuou aberto junto com o "
                         "de perfil (sidebar com dois cards empilhados)")

    # O item 10 e sobre EDITAR o e-mail de atendimento: se o campo vier vazio
    # mesmo com o mock respondendo, o valor nao esta chegando na tela.
    campos = cdp.avaliar("""(() => {
        const s = document.getElementById("perfilSuporte");
        const nome = document.getElementById("perfilNome");
        const aviso = document.getElementById("perfilAviso");
        return {
          suporte: s ? s.value : null,
          nome: nome ? nome.value : null,
          emailTravado: !!document.getElementById("perfilEmail").disabled,
          aviso: aviso ? aviso.innerText.trim() : "",
          temRotulo: /atendimento/i.test(
            document.getElementById("perfilCard").innerText),
        };
    })()""")
    if campos.get("suporte") != "suporte@empresa.com":
        problemas.append("admin/perfil: o e-mail de atendimento nao veio do "
                         f"servidor (veio {campos.get('suporte')!r})")
    if not campos.get("nome"):
        problemas.append("admin/perfil: o nome do admin nao veio do servidor")
    if not campos.get("emailTravado"):
        problemas.append("admin/perfil: o e-mail de acesso esta editavel (o login "
                         "e no Supabase; este campo e so leitura)")
    if not campos.get("temRotulo"):
        problemas.append("admin/perfil: a tela nao diz que o campo e de atendimento")
    if "carregando" in (campos.get("aviso") or ""):
        problemas.append("admin/perfil: o aviso ficou em 'carregando'")
    print(f"    perfil suporte={campos.get('suporte')!r} nome={campos.get('nome')!r} "
          f"altura={depois['perfilCard']['altura']:.0f}px")

    # E vice-versa: reabrir as contas tem que fechar o perfil.
    cdp.avaliar("document.getElementById('btnContas').click(); true")
    time.sleep(0.9)
    volta = estado("perfilCard")
    if not volta["escondido"]:
        problemas.append("admin: abrir Contas nao fechou o card de perfil")
    if estado("contasCard")["escondido"]:
        problemas.append("admin: o botao Contas nao abriu o card de contas")

    # Fecha tudo, para a medicao de cor seguinte comecar na tela limpa.
    cdp.avaliar("document.getElementById('btnFecharContas').click(); true")
    time.sleep(0.3)
    if not (antes["perfilCard"]["escondido"] and estado("perfilCard")["escondido"]):
        problemas.append("admin: o card de perfil nao voltou a ficar escondido ao fechar")
    return problemas


# Abas que dependem de JS assincrono: a secao fica escondida (display:none) e
# so ganha conteudo quando o fetch volta. Sem clicar nelas, um `carregando...`
# esquecido no HTML passaria a medicao, porque a tela "parece" certa.
ABAS_PAINEL = ("visao", "agentes", "conversas", "canais", "conta")


def _conferir_abas_do_painel(cdp: _Cdp) -> list[str]:
    """Clica cada aba do painel e olha o que sobrou na secao.

    Quatro defeitos que so aparecem aqui, e que a medicao de cor nao pegaria:

      * secao que continua com o texto "carregando..." porque o fetch falhou;
      * "undefined"/"NaN" na tela, sintoma de campo faltando no JSON da API;
      * abas empilhadas: `display:none` que deixou de valer e todas as secoes
        visiveis ao mesmo tempo (a diferenca entre "parece pronta" e "pronta");
      * botao de acao que nunca habilitou porque o limite veio faltando.
    """
    problemas: list[str] = []
    alturas: dict[str, float] = {}
    for aba in ABAS_PAINEL:
        alvos = json.dumps('[data-tela="' + aba + '"]')
        cdp.avaliar(f"document.querySelector({alvos}).click(); true")
        time.sleep(0.9)
        estado = cdp.avaliar("""(() => {
            const s = document.getElementById("t-" + %s);
            if (!s) return {erro: "secao t-%s nao existe"};
            const texto = s.innerText || "";
            return {
              on: s.classList.contains("on"),
              esperando: texto.includes("carregando"),
              undef: texto.includes("undefined") || texto.includes("NaN"),
              altura: s.getBoundingClientRect().height,
            };
        })()""" % (json.dumps(aba), aba))
        alturas[aba] = estado.get("altura") or 0
        if estado.get("erro"):
            problemas.append(f"painel: {estado['erro']}")
            continue
        if not estado.get("on"):
            problemas.append(f"painel/aba {aba}: a secao nao ficou marcada como visivel")
        if estado.get("esperando"):
            problemas.append(f"painel/aba {aba}: ficou escrito 'carregando' (fetch nao voltou)")
        if estado.get("undef"):
            problemas.append(f"painel/aba {aba}: apareceu undefined/NaN na tela")
        if (estado.get("altura") or 0) < 5:
            problemas.append(f"painel/aba {aba}: secao visivel mas com altura {estado.get('altura')}")
            continue
        # Quem confere "so tem uma aba aberta" tem que olhar ALTURA, nao
        # `display`: um section com display:none esconde os filhos tambem, mas
        # o getComputedStyle do filho continua devolvendo "block". A primeira
        # versao desta funcao media display e acusava as cinco abas de visiveis
        # ao mesmo tempo, o que e mentira -- cada uma foi medida no seu momento.
        irmas = cdp.avaliar(
            "(() => [...document.querySelectorAll('.telas > section')]"
            ".filter(x => x.id !== 't-' + %s && x.getBoundingClientRect().height > 0)"
            ".map(x => x.id))()" % json.dumps(aba))
        if irmas:
            problemas.append(
                f"painel/aba {aba}: as outras secoes continuaram na tela ({', '.join(irmas)})")
    print(f"    abas  {' '.join(f'{a}={alturas[a]:.0f}px' for a in ABAS_PAINEL)}")


    # A aba Conta e a que o item 9 acrescenta; ela precisa mostrar os tres
    # limites do plano e o consumo por agente, senao a regra existe no banco e
    # ninguem ve.
    conta = cdp.avaliar("""(() => {
        const t = document.getElementById("t-conta").innerText;
        return {
          plano: document.querySelectorAll("#planoGrade .plano").length,
          barras: document.querySelectorAll("#planoUso .barra i").length,
          temPreco: /R\\$/.test(t),
          temAgentes: /agente/i.test(t),
          temCanais: /canal/i.test(t),
          temMensagens: /mensagens/i.test(t),
        };
    })()""")
    if (conta.get("plano") or 0) < 1:
        problemas.append("painel/aba conta: nenhum plano desenhado na grade")
    if (conta.get("barras") or 0) < 1:
        problemas.append("painel/aba conta: nenhuma barra de uso por agente")
    for chave, rotulo in (("temPreco", "preco"), ("temAgentes", "limite de agentes"),
                          ("temCanais", "limite de canais"), ("temMensagens", "limite de mensagens")):
        if not conta.get(chave):
            problemas.append(f"painel/aba conta: faltou o {rotulo} na tela")
    print(f"    conta planos={conta.get('plano')} barras={conta.get('barras')} "
          f"preco={conta.get('temPreco')}")

    # Item 5 na aba Conta: ligar o 2FA depois do cadastro. Sem este bloco, quem
    # cria a conta para testar nunca chega a ativar, e o item fica meio feito.
    seguranca = cdp.avaliar("""(() => {
        const t = document.getElementById("mfaEstado");
        return {
          estado: t ? t.innerText.trim() : "",
          temAtivar: !!document.getElementById("mfaBotaoAtivar"),
          temDesligar: !!document.getElementById("mfaBotaoDesligar"),
          vazio: t ? t.innerText.trim().length < 10 : true,
        };
    })()""")
    if seguranca.get("vazio"):
        problemas.append("painel/aba conta: a secao de seguranca ficou vazia ou em "
                         "'carregando'")
    if not (seguranca.get("temAtivar") or seguranca.get("temDesligar")):
        problemas.append("painel/aba conta: nenhum botao de ativar/desligar o 2FA "
                         "(o mfa_ativo do perfil nao chegou na tela)")
    # O estado e lido em Python, nao com regex de JavaScript: essa linha nasceu
    # como `/re/i` dentro de um arquivo .py e o SyntaxError so apareceria na hora
    # de rodar a medicao.
    if not re.search(r"desligada|ativa", seguranca.get("estado") or "", re.I):
        problemas.append("painel/aba conta: o estado do 2FA nao diz se esta ativo")
    print(f"    2fa  {seguranca.get('estado', '')[:48]!r} "
          f"ativar={seguranca.get('temAtivar')} desligar={seguranca.get('temDesligar')}")

    # Clicar em "Ativar" tem que trazer QR, segredo e campo de codigo: e o que
    # distingue "o botao existe" de "o fluxo funciona".
    cdp.avaliar("""(() => {
        const b = document.getElementById("mfaBotaoAtivar");
        if (b) b.click();
        return true;
    })()""")
    time.sleep(0.8)
    inscricao = cdp.avaliar("""(() => {
        const a = document.getElementById("mfaAtivar");
        return {
          visivel: a ? !a.classList.contains("hidden") : false,
          temQr: !!document.getElementById("mfaQr"),
          temSegredo: !!document.getElementById("mfaSegredo"),
          temCodigo: !!document.getElementById("mfaCodigo"),
          temCancelar: !!document.getElementById("mfaCancelar"),
        };
    })()""")
    if inscricao.get("visivel"):
        # So reprove se abriu VAZIO. O QR vem do GoTrue, que o mock nao tem:
        # o que se pode exigir aqui e que os campos existem.
        for chave, rotulo in (("temQr", "QR"), ("temSegredo", "chave secreta"),
                              ("temCodigo", "campo de codigo"), ("temCancelar", "cancelar")):
            if not inscricao.get(chave):
                problemas.append(f"painel/2fa: a inscricao abriu sem {rotulo}")
    print(f"    2fa inscricao visivel={inscricao.get('visivel')} qr={inscricao.get('temQr')} "
          f"codigo={inscricao.get('temCodigo')}")

    # A aba Canais e o item 2: precisa listar os canais do agente escolhido e
    # oferecer o formulario (que so existe depois de /api/painel/canais/tipos).
    canais = cdp.avaliar("""(() => {
        document.querySelector('[data-tela="canais"]').click();
        return true;
    })()""")
    time.sleep(0.6)
    c = cdp.avaliar("""(() => {
        const b = document.getElementById("btnNovoCanal");
        return {
          temAgente: !!document.getElementById("selCanalAgente").value,
          uso: document.getElementById("usoCanais").innerText,
          canais: document.querySelectorAll("#canais .sessao").length,
          editaveis: document.querySelectorAll("[data-editar-canal]").length,
          deletaveis: document.querySelectorAll("[data-excluir-canal]").length,
          botaoDesligado: b ? b.disabled : true,
        };
    })()""")
    if not c.get("temAgente"):
        problemas.append("painel/aba canais: o seletor de agente ficou vazio")
    if "de" not in (c.get("uso") or ""):
        problemas.append(f"painel/aba canais: o contador de uso nao diz 'N de M' "
                         f"(veio '{c.get('uso')}')")
    if (c.get("canais") or 0) < 1:
        problemas.append("painel/aba canais: nenhum canal do agente apareceu")
    if (c.get("editaveis") or 0) < 1 or (c.get("deletaveis") or 0) < 1:
        problemas.append("painel/aba canais: faltou o botao de editar ou o de excluir")
    if c.get("botaoDesligado"):
        problemas.append("painel/aba canais: 'novo canal' desabilitado com cota sobrando")
    print(f"    canais uso='{c.get('uso')}' lista={c.get('canais')} "
          f"editar={c.get('editaveis')} excluir={c.get('deletaveis')}")

    # Abrir o formulario e ver os campos do servidor aparecerem: e o que prova
    # que a lista de tipos veio do backend e nao de uma constante na tela.
    cdp.avaliar("document.getElementById('btnNovoCanal').click(); true")
    time.sleep(0.9)
    form = cdp.avaliar("""(() => {
        const f = document.getElementById("canalForm");
        return {
          visivel: !f.classList.contains("hidden"),
          tipos: f.querySelectorAll("#cTipo option").length,
          campos: f.querySelectorAll("[data-config]").length,
          passo: /Passo a passo/.test(f.innerText),
        };
    })()""")
    if not form.get("visivel"):
        problemas.append("painel/aba canais: o formulario de canal nao abriu")
    if (form.get("tipos") or 0) < 1:
        problemas.append("painel/aba canais: o seletor de tipo veio vazio do servidor")
    if (form.get("campos") or 0) < 1:
        problemas.append("painel/aba canais: o formulario nao tem os campos do tipo escolhido")
    if not form.get("passo"):
        problemas.append("painel/aba canais: faltou o passo a passo de como obter a credencial")
    print(f"    form tipos={form.get('tipos')} campos={form.get('campos')} "
          f"passo={form.get('passo')}")
    return problemas


def _falar_no_chat(cdp: _Cdp, lista_id: str, campo_id: str, enviar_id: str,
                   texto: str) -> tuple[bool, dict]:
    """Envia uma mensagem no chat e observa o item 8: o "digitando..." aparece
    enquanto o mock demora a responder e some quando a resposta volta."""
    cdp.avaliar("(() => {"
                "  const c = document.getElementById(" + json.dumps(campo_id) + ");"
                "  c.value = " + json.dumps(texto) + ";"
                "  document.getElementById(" + json.dumps(enviar_id) + ").click();"
                "  return true;"
                "})()")
    # O mock responde com 0.35s de atraso: neste instante o "digitando..." tem
    # que estar na tela (item 8 é o intervalo até a resposta chegar).
    time.sleep(0.18)
    digitando = cdp.avaliar(
        "!!document.querySelector('#' + " + json.dumps(lista_id) + " + ' .bolha.digitando')")
    time.sleep(0.6)  # a resposta (0.35s) já voltou
    estado = cdp.avaliar(
        "(() => { const l = document.getElementById(" + json.dumps(lista_id) + ");"
        "  return { digitando: !!l.querySelector('.bolha.digitando'),"
        "           texto: l.innerText }; })()")
    return bool(digitando), estado


def _conferir_chat_da_home(cdp: _Cdp) -> list[str]:
    """Chat do atendente na home (itens 7, 8, 14, 15).

    O que só o navegador mostra: o chat existe, o botão de anexo está visível,
    a nota do limite de 12 mensagens está na tela, e o "digitando..." (item 8)
    aparece e some. Enviar a mensagem passa pelo mock com atraso, igual ao
    servidor de verdade.
    """
    problemas: list[str] = []
    estrutura = cdp.avaliar("""(() => {
        const chat = document.getElementById("homeChat");
        const b = document.getElementById("homeAnexar");
        return {
          chat: !!chat,
          lista: !!document.getElementById("homeLista"),
          campo: !!document.getElementById("homeCampo"),
          enviar: !!document.getElementById("homeEnviar"),
          anexar: !!document.getElementById("homeAnexar"),
          arquivo: !!document.getElementById("homeArquivo"),
          altura: chat ? chat.getBoundingClientRect().height : 0,
          botaoVisivel: b ? b.getBoundingClientRect().height > 0 : false,
        };
    })()""")
    for chave, rotulo in (("chat", "o cartao de chat"),
                          ("lista", "a lista de mensagens"),
                          ("campo", "o campo de texto"),
                          ("enviar", "o botao Enviar"),
                          ("anexar", "o botao de anexo"),
                          ("arquivo", "o input de arquivo")):
        if not estrutura.get(chave):
            problemas.append(f"home/item 7: faltou {rotulo}")
    if (estrutura.get("altura") or 0) < 270:
        problemas.append(f"home/item 7: chat com altura {estrutura.get('altura')}")
    if not estrutura.get("botaoVisivel"):
        problemas.append("home/item 15: o botao de anexo nao esta visivel")

    limite = cdp.avaliar(
        "(() => document.querySelector('.demo-chat .limite')"
        " ? document.querySelector('.demo-chat .limite').innerText : '')()")
    if "12" not in (limite or "") or "mensagens" not in (limite or "").lower():
        problemas.append(f"home/item 14: a nota do limite nao aparece ({limite!r})")

    digitando, estado = _falar_no_chat(
        cdp, "homeLista", "homeCampo", "homeEnviar", "qual o plano pro?")
    if not digitando:
        problemas.append("home/item 8: o 'digitando...' nao apareceu ao enviar")
    if estado.get("digitando"):
        problemas.append("home/item 8: o 'digitando...' nao sumiu com a resposta")
    if "Resposta do atendente." not in (estado.get("texto") or ""):
        problemas.append("home/item 7: a resposta do atendente nao chegou ao chat")
    print(f"    home  chat={estrutura.get('chat')} anexar={estrutura.get('botaoVisivel')} "
          f"limite={bool(limite)} digitando={digitando}")
    return problemas


def _conferir_gerador_e_suporte_painel(cdp: _Cdp) -> list[str]:
    """Gerador de prompt (item 12) e suporte flutuante (item 13) no painel.

    O gerador precisa abrir AO LADO do campo de instrução, não por cima dele:
    a media query de <=1100px centraliza o popup em telas pequenas, então esta
    medição roda com o viewport largo (1440px) para conferir a posição lateral.
    """
    problemas: list[str] = []
    # Viewport largo: é o caso onde o popup lateral (item 12) vale.
    cdp.send("Emulation.setDeviceMetricsOverride",
             {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    time.sleep(0.4)
    try:
        # Abre o formulário de agente, onde moram o campo e o botão do gerador.
        cdp.avaliar("document.getElementById('btnNovoAgente').click(); true")
        time.sleep(0.9)
        form = cdp.avaliar("""(() => {
            const f = document.getElementById("agenteForm");
            const campo = document.getElementById("agPrompt");
            const botao = document.getElementById("agGerarPrompt");
            if (!f || !campo || !botao) return {ok: false};
            const cr = campo.getBoundingClientRect();
            const br = botao.getBoundingClientRect();
            return {
              ok: true,
              aberto: !f.classList.contains("hidden"),
              junto: Math.abs(br.top - cr.top) < (cr.height + br.height),
            };
        })()""")
        if not form.get("ok"):
            problemas.append("painel/item 12: faltou agenteForm, agPrompt ou agGerarPrompt")
        elif not form.get("aberto"):
            problemas.append("painel/item 12: o formulario de agente nao abriu")
        elif not form.get("junto"):
            problemas.append("painel/item 12: o botao de gerar nao fica junto do campo")

        # Abre o popup e confere que fica AO LADO do campo, não cobrindo ele.
        cdp.avaliar("document.getElementById('agGerarPrompt').click(); true")
        time.sleep(0.5)
        popup = cdp.avaliar("""(() => {
            const p = document.getElementById("geradorPopup");
            const campo = document.getElementById("agPrompt");
            const wrap = document.querySelector(".agente-form-wrap");
            if (!p || !campo || !wrap) return {ok: false};
            const pr = p.getBoundingClientRect();
            const cr = campo.getBoundingClientRect();
            const wr = wrap.getBoundingClientRect();
            return {
              ok: true,
              aberto: !p.classList.contains("hidden"),
              aoLado: pr.right <= wr.left + 2,
              cobre: !(pr.right <= cr.left || pr.left >= cr.right),
              lista: !!document.getElementById("geradorLista"),
              popCampo: !!document.getElementById("geradorCampo"),
              popEnviar: !!document.getElementById("geradorEnviar"),
            };
        })()""")
        if not popup.get("ok"):
            problemas.append("painel/item 12: o popup do gerador nao existe")
        else:
            if not popup.get("aberto"):
                problemas.append("painel/item 12: o popup do gerador nao abriu")
            if not popup.get("aoLado"):
                problemas.append("painel/item 12: o popup nao abriu ao lado do campo de instrucao")
            if popup.get("cobre"):
                problemas.append("painel/item 12: o popup cobre o campo de instrucao")
            for chave, rotulo in (("lista", "lista"), ("popCampo", "campo"),
                                  ("popEnviar", "botao Enviar")):
                if not popup.get(chave):
                    problemas.append(f"painel/item 12: o popup abriu sem {rotulo}")

        # Fala com o gerador: a resposta vem com prompt_gerado e o botão
        # "Usar no campo de instrução" aparece.
        digitando, estado = _falar_no_chat(
            cdp, "geradorLista", "geradorCampo", "geradorEnviar",
            "um vendedor de pizza pelo whatsapp")
        if not digitando:
            problemas.append("painel/item 8: o 'digitando...' nao apareceu no gerador")
        if "Prompt montado." not in (estado.get("texto") or ""):
            problemas.append("painel/item 12: o gerador nao respondeu (mock prompt)")
        usar = cdp.avaliar("""(() => {
            const b = document.getElementById("geradorUsar");
            if (!b) return {visivel: false};
            if (!b.classList.contains("hidden")) b.click();
            return {
              visivel: !b.classList.contains("hidden"),
              preenchido: (document.getElementById("agPrompt").value || "").length > 0,
            };
        })()""")
        if not usar.get("visivel"):
            problemas.append("painel/item 12: o botao 'Usar no campo' nao apareceu apos gerar")
        if not usar.get("preenchido"):
            problemas.append("painel/item 12: clicar em 'Usar no campo' nao preencheu o prompt")

        # Suporte flutuante (item 13): abre o modal, responde, e a ação
        # "mudar_plano" vira o atalho "Ver meus pagamentos".
        botao = cdp.avaliar("""(() => {
            const b = document.getElementById("suporteBotao");
            return b ? {visivel: b.getBoundingClientRect().height > 0} : {visivel: false};
        })()""")
        if not botao.get("visivel"):
            problemas.append("painel/item 13: o botao flutuante do suporte nao esta visivel")
        cdp.avaliar("document.getElementById('suporteBotao').click(); true")
        time.sleep(0.7)
        modal = cdp.avaliar("""(() => {
            const m = document.getElementById("suporteModal");
            return {
              aberto: m && !m.classList.contains("hidden"),
              lista: !!document.getElementById("suporteLista"),
              campo: !!document.getElementById("suporteCampo"),
              enviar: !!document.getElementById("suporteEnviar"),
              botaoSumiu: document.getElementById("suporteBotao")
                  .classList.contains("hidden"),
            };
        })()""")
        if not modal.get("aberto"):
            problemas.append("painel/item 13: o modal de suporte nao abriu com o botao flutuante")
        for chave, rotulo in (("lista", "lista"), ("campo", "campo"), ("enviar", "Enviar")):
            if not modal.get(chave):
                problemas.append(f"painel/item 13: o suporte abriu sem {rotulo}")
        if not modal.get("botaoSumiu"):
            problemas.append("painel/item 13: o botao flutuante nao sumiu com o modal aberto")

        s_digitando, s_estado = _falar_no_chat(
            cdp, "suporteLista", "suporteCampo", "suporteEnviar", "quero mudar de plano")
        if not s_digitando:
            problemas.append("painel/item 8: o 'digitando...' nao apareceu no suporte")
        atalho = cdp.avaliar("""(() => {
            const b = document.querySelector('#suporteLista .chat-botao.ghost');
            return {
              tem: !!b,
              texto: b ? b.textContent : "",
            };
        })()""")
        if not atalho.get("tem"):
            problemas.append("painel/item 13: a acao de mudar de plano nao virou atalho")
        else:
            cdp.avaliar("document.querySelector('#suporteLista .chat-botao.ghost').click(); true")
            time.sleep(0.6)
            conta = cdp.avaliar(
                "document.getElementById('t-conta').classList.contains('on')")
            suporte_fechado = cdp.avaliar(
                "document.getElementById('suporteModal').classList.contains('hidden')")
            if not conta:
                problemas.append("painel/item 13: 'Ver meus pagamentos' nao abriu a aba Conta")
            if not suporte_fechado:
                problemas.append("painel/item 13: o suporte nao fechou ao ver os pagamentos")
        print(f"    gerador lateral={popup.get('aoLado')} usar={usar.get('visivel')} "
              f"preenchido={usar.get('preenchido')}")
        print(f"    suporte aberto={modal.get('aberto')} atalho={atalho.get('texto')!r} "
              f"conta={conta if atalho.get('tem') else 'n/a'}")
    finally:
        cdp.send("Emulation.clearDeviceMetricsOverride")
        time.sleep(0.3)
    return problemas


def _conferir_suporte_admin(cdp: _Cdp) -> list[str]:
    """Botão flutuante e modal do suporte no admin (item 13) + "digitando" (8)."""
    problemas: list[str] = []
    botao = cdp.avaliar("""(() => {
        const b = document.getElementById("suporteBotao");
        return b ? {visivel: b.getBoundingClientRect().height > 0} : {visivel: false};
    })()""")
    if not botao.get("visivel"):
        problemas.append("admin/item 13: o botao flutuante do suporte nao esta visivel")
    cdp.avaliar("document.getElementById('suporteBotao').click(); true")
    time.sleep(0.7)
    modal = cdp.avaliar("""(() => {
        const m = document.getElementById("suporteModal");
        return {
          aberto: m && !m.classList.contains("hidden"),
          lista: !!document.getElementById("suporteLista"),
          campo: !!document.getElementById("suporteCampo"),
          enviar: !!document.getElementById("suporteEnviar"),
        };
    })()""")
    if not modal.get("aberto"):
        problemas.append("admin/item 13: o modal de suporte nao abriu")
    for chave, rotulo in (("lista", "lista"), ("campo", "campo"), ("enviar", "Enviar")):
        if not modal.get(chave):
            problemas.append(f"admin/item 13: o suporte abriu sem {rotulo}")
    digitando, estado = _falar_no_chat(
        cdp, "suporteLista", "suporteCampo", "suporteEnviar", "preciso de ajuda")
    if not digitando:
        problemas.append("admin/item 8: o 'digitando...' nao apareceu no suporte")
    if "Resposta do atendente." not in (estado.get("texto") or ""):
        problemas.append("admin/item 13: o suporte nao respondeu")
    print(f"    suporte aberto={modal.get('aberto')} digitando={digitando} "
          f"resposta={'Resposta do atendente.' in (estado.get('texto') or '')}")
    return problemas


def medir(navegador: str, base: str, telas: list[str]) -> list[str]:
    porta = _porta_livre()
    perfil = pathlib.Path(tempfile.gettempdir()) / "chatbotproject-medir-layout"
    shutil.rmtree(perfil, ignore_errors=True)
    proc = subprocess.Popen(
        [navegador, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", f"--remote-debugging-port={porta}",
         f"--user-data-dir={perfil}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problemas: list[str] = []
    try:
        versao = None
        for _ in range(60):
            try:
                versao = _http_json(porta, "/json/version")
                break
            except Exception:
                time.sleep(0.25)
        if not versao:
            raise SystemExit(f"{navegador} nao abriu a porta de debug {porta}")
        print(f"navegador: {versao.get('Browser', '?')}\n")
        for tela in telas:
            problemas += _medir_tela(base, tela, porta)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(perfil, ignore_errors=True)
    return problemas


def achar_navegador() -> str:
    for p in NAVEGADORES:
        if os.path.exists(p) or shutil.which(p):
            return p
    raise SystemExit("nenhum Chrome/Edge encontrado — instale um dos dois para rodar isso")


def main() -> int:
    falha = "--falha" in sys.argv
    telas = [a for a in sys.argv[1:] if not a.startswith("--")] or TELAS
    srv, porta = subir_servidor()
    print(f"servidor de mentira em http://127.0.0.1:{porta} (raiz {RAIZ})\n")
    try:
        problemas = medir(achar_navegador(), f"http://127.0.0.1:{porta}", telas)
    finally:
        srv.shutdown()
    print()
    if problemas:
        print("PROBLEMAS:")
        for p in problemas:
            print(" - " + p)
        return 1 if falha else 0
    print("LAYOUT E TEMA OK: " + ", ".join(telas))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

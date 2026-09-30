"""Refaz o login exatamente como o navegador faz, com os headers de Origin.

Por que um script e nao o smoke_prod.py: aquele chama a API pela mesma origem
(o httpx nao manda Origin, entao nao ha preflight). O caminho que o usuario
realmente percorre e browser -> Supabase Auth, que sao origens diferentes e
passa por CORS. Se isso falhar, a pagina fica parada sem mensagem util.
"""
from __future__ import annotations

import pathlib
import re
import sys
import time

PROMPT = pathlib.Path(__file__).resolve().parent.parent.parent / "prompt.txt"
BASE = "https://chatbotproject-1-l9zr.onrender.com"
PAGES = "https://jgabrielss-dev.github.io/chatbotproject/"

# O navegador agora abre o site no GITHUB PAGES e fala com a API no Render. Sao
# origens diferentes, entao o CORS que importa e o do Pages, nao o do Render.
# Testar com Origin do Render dava 200 sem provar nada do caminho real.
ORIGEM = PAGES


def _cred(chave: str) -> str:
    m = re.search(rf"{chave}:\s*(\S+)", PROMPT.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"'{chave}' nao encontrado em {PROMPT}")
    return m.group(1)


def main() -> None:
    import httpx

    email, senha = sys.argv[1], sys.argv[2]
    url = _cred("supabase URL").rstrip("/")
    origem = {"Origin": ORIGEM, "Referer": ORIGEM + "/"}
    c = httpx.Client(timeout=180, headers=origem)

    print("1) GET /api/config  (mesma origem, sem preflight)")
    t0 = time.time()
    r = c.get(f"{BASE}/api/config")
    print(f"   {r.status_code} em {time.time() - t0:.1f}s")
    if r.status_code != 200:
        print(r.text[:300])
        sys.exit(1)
    cfg = r.json()
    print(f"   supabase_url={cfg.get('supabase_url')}")
    print(f"   anon_key presente={bool(cfg.get('supabase_anon_key'))}")
    print(f"   auth_habilitado={cfg.get('auth_habilitado')}")

    print("2) POST GoTrue /auth/v1/token  (origem CRUZADA, com preflight)")
    t0 = time.time()
    op = c.options(
        f"{url}/auth/v1/token?grant_type=password",
        headers={
            "Origin": ORIGEM,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,apikey",
        },
    )
    acao = op.headers.get("access-control-allow-origin")
    print(f"   preflight {op.status_code} em {time.time() - t0:.1f}s | ACAO={acao or 'AUSENTE'}")
    if not acao:
        print("   FALHA: o browser bloquearia a chamada por CORS")
        sys.exit(1)

    t0 = time.time()
    r = c.post(
        f"{url}/auth/v1/token?grant_type=password",
        headers={"apikey": cfg["supabase_anon_key"], "Content-Type": "application/json"},
        json={"email": email, "password": senha},
    )
    print(f"   login {r.status_code} em {time.time() - t0:.1f}s | ACAO={r.headers.get('access-control-allow-origin') or 'AUSENTE'}")
    if r.status_code >= 400:
        print("   " + r.text[:300])
        sys.exit(1)
    token = r.json()["access_token"]
    print(f"   access_token {len(token)} chars")

    print("3) GET /api/eu com o Bearer  (mesma origem)")
    t0 = time.time()
    eu = c.get(f"{BASE}/api/eu", headers={"Authorization": f"Bearer {token}"})
    print(f"   {eu.status_code} em {time.time() - t0:.1f}s")
    if eu.status_code != 200:
        print("   " + eu.text[:300])
        sys.exit(1)
    print(f"   {eu.json()}")

    print(f"4) o que o browser ve no GITHUB PAGES ({PAGES})")
    site = httpx.Client(timeout=180)
    for caminho, marcador in (("", 'href="login.html"'),
                              ("login.html", 'id="form"'),
                              ("admin.html", 'src="auth.js"')):
        t0 = time.time()
        r = site.get(PAGES + caminho)
        print(f"   GET {caminho or '/'} -> {r.status_code} em {time.time() - t0:.1f}s, "
              f"{len(r.text)} bytes | contem {marcador!r}: {marcador in r.text}")
    # Os assets tem de ser servidos pelo Pages. Se vierem do Render, o site
    # so funciona por causa de uma segunda fonte de verdade.
    for asset in ("auth.js", "style.css"):
        r = site.get(PAGES + asset)
        print(f"   GET {asset} -> {r.status_code} ({len(r.content)} bytes)")

    print("\nFLUXO DO NAVEGADOR OK")


if __name__ == "__main__":
    main()

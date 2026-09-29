"""Confere se a conta de admin realmente consegue entrar na API do GoTrue.

Importante: o fato de a linha existir em `perfis` com papel admin nao diz que
o login funciona. O GoTrue guarda a senha em `auth.identities`, o hash e o
`email_confirmed_at`, e qualquer um dos tres errado aparece no navegador como
"Invalid login credentials" numa conta que parece existir. Este script faz o
login de verdade e devolve o token, entao o erro aparece aqui com detalhe.

    python scripts/testar_login.py <email> <senha>
"""
from __future__ import annotations

import pathlib
import re
import sys

PROMPT = pathlib.Path(__file__).resolve().parent.parent.parent / "prompt.txt"


def _cred(chave: str) -> str:
    m = re.search(rf"{chave}:\s*(\S+)", PROMPT.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"'{chave}' nao encontrado em {PROMPT}")
    return m.group(1)


def main() -> None:
    import httpx

    email, senha = sys.argv[1], sys.argv[2]
    url = _cred("supabase URL").rstrip("/")
    pk = _cred("supabase PK")

    r = httpx.post(
        f"{url}/auth/v1/token?grant_type=password",
        headers={"apikey": pk, "Content-Type": "application/json"},
        json={"email": email, "password": senha},
        timeout=60,
    )
    print(f"HTTP {r.status_code}")
    if r.status_code >= 400:
        print(r.text[:400])
        sys.exit(1)

    corpo = r.json()
    token = corpo.get("access_token", "")
    print(f"access_token: {len(token)} chars")
    print(f"refresh_token presente: {bool(corpo.get('refresh_token'))}")
    print(f"expira em: {corpo.get('expires_in')}s")

    u = httpx.get(
        f"{url}/auth/v1/user",
        headers={"apikey": pk, "Authorization": f"Bearer {token}"},
        timeout=60,
    ).json()
    print(f"GET /auth/v1/user -> id={u.get('id')} email={u.get('email')}")

    # A propria API do app valida o token e le o papel; e o mesmo caminho que o
    # navegador vai fazer, entao se der 200 aqui o painel abre la.
    import os
    r2 = httpx.get(
        "https://chatbotproject-1-l9zr.onrender.com/api/eu",
        headers={"Authorization": f"Bearer {token}"},
        timeout=90,
    )
    print(f"GET /api/eu em producao -> HTTP {r2.status_code}")
    print("  (200 = token valido e conta nao bloqueada; 503 = app ainda sem"
          " SUPABASE_URL; 401 = token rejeitado; 404 = codigo antigo no ar)")


if __name__ == "__main__":
    main()

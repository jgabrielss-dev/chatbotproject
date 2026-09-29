"""Cria a conta de admin pela API oficial do GoTrue, sem mexer em `auth.users`.

Por que esse caminho e nao outro:
- A Management API nao tem endpoint de criacao de usuario (POST
  /v1/projects/{ref}/auth/users responde 404).
- Inserir direto em `auth.users` + `auth.identities` por SQL funciona, mas
  depende de detalhe interno do GoTrue e, quando erra, o login falha de um
  jeito confuso ("Invalid login credentials") em conta que existe.
- O que a propria Supabase recomenda e `mailer_autoconfirm`: com ele ligado, o
  signup devolve sessao na hora e nenhum e-mail sai. Entao o passo e: liga,
  cadastra pela API publica (chave publishable), e desliga.

O risco da janela: enquanto autoconfirm estiver ligado, alguem que saiba o
e-mail poderia criar conta sem confirmar. Sao poucos segundos, e o script
desliga no `finally` mesmo se o cadastro falhar.

    python scripts/criar_admin.py <email> [<senha>]
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

PROMPT = pathlib.Path(__file__).resolve().parent.parent.parent / "prompt.txt"
CONFIG_AUTH = "https://api.supabase.com/v1/projects/ophhmvnascjpayoxqdkz/config/auth"


def _cred(chave: str) -> str:
    m = re.search(rf"{chave}:\s*(\S+)", PROMPT.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"'{chave}' nao encontrado em {PROMPT}")
    return m.group(1)


def _chave(autorizado: str) -> dict:
    return {"Authorization": f"Bearer {autorizado}", "Content-Type": "application/json"}


def main() -> None:
    import httpx

    email = sys.argv[1] if len(sys.argv) > 1 else "joaogabrielss.2007@gmail.com"
    if len(sys.argv) > 2:
        senha = sys.argv[2]
    else:
        import secrets
        senha = secrets.token_urlsafe(15)

    pat = _cred("supabase PAT")
    url = _cred("supabase URL").rstrip("/")
    pk = _cred("supabase PK")

    print(f"1) ligando mailer_autoconfirm em {url}")
    r = httpx.patch(CONFIG_AUTH, headers=_chave(pat), json={"mailer_autoconfirm": True}, timeout=60)
    r.raise_for_status()

    try:
        print(f"2) cadastrando {email}")
        resp = httpx.post(
            f"{url}/auth/v1/signup",
            headers={"apikey": pk, "Content-Type": "application/json"},
            json={"email": email, "password": senha},
            timeout=60,
        )
        corpo = resp.json()
        if resp.status_code >= 400 or not corpo.get("id"):
            print("   FALHOU:", resp.status_code, json.dumps(corpo)[:300])
            sys.exit(1)
        print(f"   id={corpo['id']}")
        print(f"   confirmada={bool(corpo.get('confirmed_at') or corpo.get('email_confirmed_at'))}")
        print(f"   senha={senha}")
    finally:
        print("3) religando a exigencia de confirmar e-mail")
        httpx.patch(CONFIG_AUTH, headers=_chave(pat), json={"mailer_autoconfirm": False}, timeout=60)
        conf = httpx.get(CONFIG_AUTH, headers={"Authorization": f"Bearer {pat}"}, timeout=60).json()
        print(f"   mailer_autoconfirm={conf.get('mailer_autoconfirm')}")


if __name__ == "__main__":
    main()

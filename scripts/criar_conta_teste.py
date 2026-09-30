"""Cria uma conta de teste oficial no Supabase e devolve um token.

Usada para testar as rotas que exigem login (itens 12 e 13) de ponta a ponta,
com o mesmo caminho que uma pessoa de verdade percorreria. Sem isto, o script
de conversa testaria só o `_falar` direto, e a rota (login obrigatório, plano
mínimo) ficaria sem exercício.

Linha de comando: cria (se precisar) e faz login, imprimindo o access_token.

    python scripts/criar_conta_teste.py <email> <senha>

A conta é criada confirmada via Management API (`email_confirm`), porque o
fluxo de confirmação por e-mail não tem como rodar sozinho num script — e o
que estamos testando aqui é o chat interno, não o fluxo de confirmação.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import httpx

CREDENCIAIS = pathlib.Path(r"E:\chatbots project\credentials.txt")
PROMPT = pathlib.Path(r"E:\chatbots project\prompt.txt")
REF = "ophhmvnascjpayoxqdkz"
USERS = f"https://api.supabase.com/v1/projects/{REF}/users"


def _cred() -> str:
    for arquivo in (CREDENCIAIS, PROMPT):
        if not arquivo.exists():
            continue
        texto = arquivo.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"sbp_[A-Za-z0-9]+", texto):
            return m.group(0)
    raise SystemExit("PAT nao encontrado")


def _anon() -> tuple[str, str]:
    supabase_url = anno = ""
    for arquivo in (CREDENCIAIS, PROMPT):
        if not arquivo.exists():
            continue
        texto = arquivo.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"supabase\s+url:\s*(\S+)", texto, re.I)
        if m:
            supabase_url = m.group(1).rstrip("/")
        m = re.search(r"supabase\s+pk:\s*(\S+)", texto, re.I)
        if m:
            anno = m.group(1)
    if not supabase_url or not anno:
        raise SystemExit("URL ou anon key nao encontrados")
    return supabase_url, anno


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    email, senha = sys.argv[1], sys.argv[2]
    pat = _cred()
    supabase_url, anon = _anon()
    h = {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}

    # 1) Conta confirmada (se já existir, o endpoint devolve a existente com
    #    status 200/409; seguimos de qualquer forma e tentamos o login).
    r = httpx.post(USERS, headers=h, json={
        "email": email, "password": senha, "email_confirm": True}, timeout=60)
    if r.status_code not in (200, 201, 409):
        print(r.text[:1500])
        raise SystemExit(f"criar usuario: HTTP {r.status_code}")

    # 2) Login de verdade pelo GoTrue, como o navegador faria.
    g = httpx.post(
        f"{supabase_url}/auth/v1/token?grant_type=password",
        headers={"apikey": anon, "Content-Type": "application/json"},
        json={"email": email, "password": senha}, timeout=60,
    )
    if not g.is_success:
        print(g.text[:800])
        raise SystemExit(f"login: HTTP {g.status_code}")
    dados = g.json()
    print(dados["access_token"])


if __name__ == "__main__":
    main()
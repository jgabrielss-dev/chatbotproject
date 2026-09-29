"""Aplica uma migration no Supabase usando a Management API.

Por que um script e nao o psql: a string de conexao direta do prompt.txt tem
a senha como placeholder ([YOUR-PASSWORD]), entao nao da para conectar no
Postgres. A Management API resolve, usando o PAT `sbp_...`.

    python scripts/supabase_sql.py "SELECT 1"
    python scripts/supabase_sql.py <arquivo.sql>
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

PROMPT = pathlib.Path(__file__).resolve().parent.parent.parent / "prompt.txt"
REF = "ophhmvnascjpayoxqdkz"
ENDPOINT = f"https://api.supabase.com/v1/projects/{REF}/database/query"


def _cred(chave: str) -> str:
    texto = PROMPT.read_text(encoding="utf-8")
    m = re.search(rf"{chave}:\s*(\S+)", texto)
    if not m:
        raise SystemExit(f"'{chave}' nao encontrado em {PROMPT}")
    return m.group(1)


def sql(consulta: str) -> object:
    """Executa SQL e devolve o resultado cru da API."""
    import httpx

    resp = httpx.post(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {_cred('supabase PAT')}",
            "Content-Type": "application/json",
        },
        content=json.dumps({"query": consulta}),  # content= evita o encode do PowerShell
        timeout=120,
    )
    if resp.status_code >= 400:
        raise SystemExit(f"HTTP {resp.status_code}\n{resp.text[:1500]}")
    return resp.json()


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    alvo = sys.argv[1]
    consulta = (pathlib.Path(alvo).read_text(encoding="utf-8")
                if pathlib.Path(alvo).is_file() else alvo)
    r = sql(consulta)
    if isinstance(r, list) and r:
        for linha in r:
            print(json.dumps(linha, ensure_ascii=False))
    else:
        print("ok:", json.dumps(r, ensure_ascii=False)[:400])


if __name__ == "__main__":
    main()

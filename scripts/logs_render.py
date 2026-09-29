"""Le os logs do Render e mostra as linhas do startup mais recente.

O heartbeat so se confirma no log: o codigo estar la nao prova que a tarefa
esta rodando (ela pode ter morrido no primeiro GET, ou o BASE_URL pode estar
vazio e a tarefa ter saido sem pingar nada). Entao a verificacao util e: o
processo subiu depois do deploy e logou "Heartbeat LIGADO"?

    python scripts/logs_render.py [n_linhas]
"""
from __future__ import annotations

import pathlib
import re
import sys

PROMPT = pathlib.Path(__file__).resolve().parent.parent.parent / "prompt.txt"
SERVICO = "srv-dao2bm740ujc73dku66g"


def _cred(chave: str) -> str:
    m = re.search(rf"{chave}:\s*(\S+)", PROMPT.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"'{chave}' nao encontrado em {PROMPT}")
    return m.group(1)


def main() -> None:
    import httpx

    pat = _cred("render PAT")
    h = {"Authorization": f"Bearer {pat}"}
    limite = int(sys.argv[1]) if len(sys.argv) > 1 else 120

    # A API pública de logs do Render foi descontinuada (404). O que ainda
    # responde é o log do build, e nele sai a saída do Uvicorn — inclusive as
    # linhas de startup do processo, porque o build e o start rodam no mesmo
    # log. Se nem esse responder, o caminho é o dashboard.
    r = httpx.get(
        f"https://api.render.com/v1/services/{SERVICO}/logs",
        headers=h, params={"type": "build", "limit": limite}, timeout=90,
    )
    print(f"HTTP {r.status_code}")
    if r.status_code >= 400:
        print(r.text[:400])
        print("Sem log via API. O startup e o heartbeat precisam ser conferidos "
              "no dashboard do Render, em Logs.")
        sys.exit(1)

    linhas = r.json()
    # O Render devolve do mais novo para o mais antigo.
    for item in linhas:
        texto = (item.get("text") or "").strip()
        if not texto:
            continue
        marca = ""
        if re.search(r"heartbeat", texto, re.I):
            marca = "  <<< HEARTBEAT"
        elif re.search(r"keepalive|Application startup|Uvicorn running",
                       texto, re.I):
            marca = "  <<< startup"
        print(f"{texto}{marca}")


if __name__ == "__main__":
    main()

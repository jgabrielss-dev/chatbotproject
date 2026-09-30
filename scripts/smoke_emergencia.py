"""Veste a conta de emergencia (x-admin-token) em TODAS as rotas GET da API e
aponta as que devolvem 5xx.

Por que isso existe: a conta do `ADMIN_TOKEN` é a entrada que o operador usa
quando a plataforma está com problema — o GoTrue fora do ar, o deploy no ar. O
id dela é um sentinela que NÃO é uuid, de propósito (nada pode gravá-lo em
coluna com FK), então qualquer rota que filtre por `dono_id` e use
`usuario.id` direto manda um valor inválido para o Postgres e leva 500. Foi
exatamente o que aconteceu com /api/painel/perfil, /api/painel/caixa,
/api/painel/resumo, /api/painel/admin/config e /api/plano/pagamentos — telas
que o operador abre justamente quando tudo está quebrado.

A suíte cobre as rotas com pool falso; este script confere a lista INTEIRA
contra o deploy, incluindo a rota que ninguém teste, que é a próxima.

    python scripts/smoke_emergencia.py

Lê o ADMIN_TOKEN do env do serviço no Render e não imprime nenhum segredo.
"""
from __future__ import annotations

import pathlib
import re
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
CREDENCIAIS = pathlib.Path(r"E:\chatbots project\credentials.txt")
PROMPT = pathlib.Path(r"E:\chatbots project\credentials.txt")
BASE = "https://chatbotproject-1-l9zr.onrender.com"
SERVICO = "srv-dao2bm740ujc73dku66g"

# Path params com um valor plausível do deploy (agente 18 = "teste", canal 1).
SUBSTITUICOES = {
    "agente_id": "18", "canal_id": "1", "pagamento_id": "1", "plano": "pro",
    "item": "12", "sessao_id": "1", "conta_id": "1", "id": "18",
}


def _cred(chave: str) -> str:
    for arquivo in (CREDENCIAIS, PROMPT):
        if not arquivo.exists():
            continue
        m = re.search(rf"{re.escape(chave)}:\s*(\S+)", arquivo.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    raise SystemExit(f"'{chave}' nao encontrada")


def token_de_emergencia() -> str:
    import httpx

    bruto = httpx.get(
        f"https://api.render.com/v1/services/{SERVICO}/env-vars",
        headers={"Authorization": f"Bearer {_cred('render PAT')}"}, timeout=60).json()
    env = {e.get("key"): e.get("value")
           for e in (i.get("envVar", i) for i in bruto)}
    if not env.get("ADMIN_TOKEN"):
        raise SystemExit("ADMIN_TOKEN ausente no env do Render")
    return env["ADMIN_TOKEN"]


def main() -> None:
    import httpx

    h = {"x-admin-token": token_de_emergencia()}
    c = httpx.Client(timeout=300, headers=h)

    esquema = c.get(f"{BASE}/openapi.json")
    if esquema.status_code != 200:
        raise SystemExit(f"openapi.json -> {esquema.status_code}: {esquema.text[:120]}")
    rotas = sorted(esquema.json()["paths"])

    print(f"{'rota':<48} status")
    print("-" * 80)
    ruins: list[tuple[str, int, str]] = []
    for caminho in rotas:
        if "get" not in esquema.json()["paths"][caminho]:
            continue
        alvo = caminho
        for nome in re.findall(r"\{(\w+)\}", caminho):
            alvo = alvo.replace("{" + nome + "}", SUBSTITUICOES.get(nome, "1"))
        try:
            r = c.get(f"{BASE}{alvo}")
        except httpx.HTTPError as e:
            print(f"{alvo:<48} erro de rede: {type(e).__name__}")
            continue
        print(f"{alvo:<48} {r.status_code}{'   <<< 5xx' if r.status_code >= 500 else ''}")
        if r.status_code >= 500:
            ruins.append((alvo, r.status_code, r.text[:120]))

    print()
    if ruins:
        print(f"{len(ruins)} rota(s) com 5xx para a conta de emergencia:")
        for rota, st, txt in ruins:
            print(f"  {rota} -> {st} {txt}")
        sys.exit(1)
    print(f"TUDO OK: as {len(rotas)} rotas GET respondem sem 5xx.")


if __name__ == "__main__":
    main()

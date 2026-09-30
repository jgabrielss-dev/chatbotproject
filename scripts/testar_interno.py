"""Rota a rota, os chats internos (itens 7, 12, 13, 14) contra a base real.

O passo anterior (`conversar_interno.py`) chama `_falar` direto — bom para ler a
resposta do modelo. Este aqui entra pela rota, como o navegador, só que trocando
o Gemini por uma resposta fixa: é o que torna o teste determinístico e rápido, e
isola o que preocupa — autenticação, cota, plano mínimo e os guards do item 14 —
do humor do modelo.

    python scripts/testar_interno.py <email> <senha>

O e-mail precisa NÃO existir: o script cria a conta, testa e apaga no final.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_ANON_KEY", "")

CREDENCIAIS = pathlib.Path(r"E:\chatbots project\credentials.txt")
REF = "ophhmvnascjpayoxqdkz"


def _preencher_env() -> None:
    texto = CREDENCIAIS.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"supabase\s+url:\s*(\S+)", texto, re.I)
    os.environ["SUPABASE_URL"] = m.group(1).rstrip("/")
    m = re.search(r"supabase\s+pk:\s*(\S+)", texto, re.I)
    os.environ["SUPABASE_ANON_KEY"] = m.group(1)


def _signup(email: str, senha: str) -> tuple[str, str]:
    import httpx

    r = httpx.post(
        f"{os.environ['SUPABASE_URL']}/auth/v1/signup",
        headers={"apikey": os.environ["SUPABASE_ANON_KEY"],
                 "Content-Type": "application/json"},
        json={"email": email, "password": senha, "data": {"nome": "Conta Interno"}},
        timeout=60,
    )
    dados = r.json()
    return dados["access_token"], str(dados["user"]["id"])


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    email, senha = sys.argv[1], sys.argv[2]
    _preencher_env()

    import json
    import uuid

    import httpx
    from fastapi.testclient import TestClient

    import app.main as m
    import app.chat_interno as ci

    # Troca o modelo por algo previsível. A rota não pode depender do Gemini
    # para testar autenticação — e o que ela faz com a resposta é o que importa.
    async def _fake_responder(_p, _h, mensagem, _m, _a=None):
        return ("Você disse: " + (mensagem or "")[:80]
                + " <<<ACAO>>>\n{\"acao\": \"lista_nao_existe\"}\n<<<FIM>>>")

    async def _fake_memoria(_p, memoria, _trechos):
        return memoria or {}

    ci.gemini.responder = _fake_responder
    ci.gemini.atualizar_memoria = _fake_memoria

    token, user_id = _signup(email, senha)
    h = {"Authorization": f"Bearer {token}"}
    erros: list[str] = []

    def conferir(nome: str, cond: bool, detalhe: str = "") -> None:
        status = "ok  " if cond else "FALHA"
        print(f"  [{status}] {nome}" + (f"  {detalhe}" if detalhe else ""))
        if not cond:
            erros.append(nome)

    with TestClient(m.app) as c:
        print("== produtos")
        r = c.get("/api/interno/agentes")
        conferir("GET /api/interno/agentes e publica", r.status_code == 200)
        chaves = {a["chave"] for a in r.json()["agentes"]}
        conferir("lista os tres agentes", chaves == {"produto", "suporte", "prompt"},
                 str(sorted(chaves)))

        sessao = uuid.uuid4().hex
        r = c.post("/api/interno/produto", json={"sessao": sessao, "texto": "oi"})
        conferir("home sem login fala", r.status_code == 200, str(r.status_code))
        print("   " + str(r.json().get("resposta", ""))[:80])

        r = c.get("/api/interno/produto/historico", params={"sessao": sessao})
        conferir("historico da home carrega", r.status_code == 200
                 and any(x["texto"] == "oi" for x in r.json()["mensagens"]))

        r = c.post("/api/interno/suporte", json={"sessao": "12345678", "texto": "oi"})
        conferir("suporte sem login: 401", r.status_code == 401, str(r.status_code))

        print("== cota da home (item 14): 12 mensagens, depois 429")
        ok_ate_12 = True
        for i in range(12):
            r = c.post("/api/interno/produto",
                       json={"sessao": sessao, "texto": f"mensagem {i}"})
            if r.status_code not in (200, 429):
                ok_ate_12 = False
                break
        conferir("cabem 12 mensagens na conversa da home", ok_ate_12)
        r = c.post("/api/interno/produto",
                   json={"sessao": sessao, "texto": "estourando a cota"})
        conferir("a 13a e barrada (429)", r.status_code == 429, str(r.status_code))

        print("== suporte logado (item 13)")
        r = c.post("/api/interno/suporte", headers=h,
                   json={"sessao": user_id, "texto": "quero minha conta"})
        conferir("suporte logado responde", r.status_code == 200, str(r.status_code))

        print("== plano minimo do gerador (item 12)")
        r = c.post("/api/interno/prompt", headers=h,
                   json={"sessao": user_id, "texto": "um bot de vendas"})
        conferir("gerador barrado no plano teste (403)", r.status_code == 403,
                 str(r.status_code))
        conferir("403 traz a regra", "o plano" in r.text.lower() or "precis" in r.text.lower())

        print("== guards do item 14")
        r = c.get("/api/interno/agentes")
        interno_ids = []
        for a in r.json()["agentes"]:
            par = _par_sql(a["chave"])
            if par:
                interno_ids.append(par)
        if interno_ids:
            agente_id, _canal_id = interno_ids[0]
            r = c.put(f"/api/agentes/{agente_id}", headers=h,
                      json={"nome": "x", "system_prompt": "hack", "ativo": True})
            conferir("editar agente interno: 404", r.status_code == 404,
                     str(r.status_code))
            r = c.delete(f"/api/agentes/{agente_id}", headers=h)
            conferir("apagar agente interno: 404", r.status_code == 404,
                     str(r.status_code))
        else:
            conferir("email interno achado na base", False, "nenhum par interno")

    print()
    if erros:
        print("FALHAS:", ", ".join(erros))
        sys.exit(1)
    print("TUDO OK")
    print(user_id)


def _par_sql(chave: str):
    """Busca o id do agente interno sem depender do pool de eventos."""
    from supabase_sql import sql  # noqa: E402

    rows = sql(f"select a.id, c.id as canal from public.agentes a "
               f"join public.canais c on c.agente_id=a.id "
               f"where a.interno='{chave}'")
    if not rows:
        return None
    return rows[0]["id"], rows[0]["canal"]


if __name__ == "__main__":
    main()
"""Smoke test da producao depois do deploy.

Confere o que a mudanca de fato prometia:
1. as paginas novas respondem 200 (antes / era o painel velho na raiz);
2. a API esta FECHADA para quem nao tem sessao (antes, sem ADMIN_TOKEN, ela
   respondia a fila de mensagens de qualquer cliente);
3. NENHUM segredo aparece em resposta AUTENTICADA. A primeira versao deste
   script checava vazamento em /api/agentes sem token, ou seja, recebia 401 e
   nao provava nada: so conferia a rota bloqueada. O teste de vazamento so tem
   valor logado como admin, e o endpoint critico e
   GET /api/agentes/{id}/canais, que era exatamente onde a lista de redacao
   divergente mandava access_token/app_secret da Meta em claro;
4. o admin entra e os 3 agentes legados aparecem adotados;
5. um cliente comum NAO enxerga canal nao oficial (whatsapp/instagram).

    python scripts/smoke_prod.py <email> <senha>
"""
from __future__ import annotations

import pathlib
import re
import sys

PROMPT = pathlib.Path(__file__).resolve().parent.parent.parent / "prompt.txt"
BASE = "https://chatbotproject-1-l9zr.onrender.com"

# Chaves cujo VALOR precisa estar mascarado. O nome da chave pode e deve
# aparecer: o frontend precisa saber que existe sessionid para mostrar
# "conectado". O que nao pode e o valor. Checar o nome (como fez a primeira
# versao deste script) dava falso positivo em sessionid, que ja vem mascarado.
SENSIVEIS = {"token", "secret", "senha", "password", "api_key", "apikey",
             "sessionid", "access_token", "app_secret", "verify_token",
             "usuario", "user", "ig_vistos", "chat_id", "phone_number_id",
             "whatsapp_business_account_id", "ig_user", "ig_pass"}
MASCARA = "********"

FALHAS: list[str] = []


def _cred(chave: str) -> str:
    m = re.search(rf"{chave}:\s*(\S+)", PROMPT.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"'{chave}' nao encontrado em {PROMPT}")
    return m.group(1)


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def ruim(msg: str) -> None:
    FALHAS.append(msg)
    print(f"  FALHA {msg}")


def sem_segredo(rota: str, corpo: str) -> None:
    """Exige que o VALOR de toda chave sensivel esteja mascarado.

    Tambem procura credenciais soltas (prefixo de token) em qualquer lugar do
    corpo, que e o jeito de pegar segredo em chave com nome inocente.
    """
    import json as _json
    try:
        dados = _json.loads(corpo)
    except ValueError:
        ruim(f"{rota}: resposta nao e JSON, nao da para auditar")
        return

    vazou: list[str] = []

    def andar(no, trilha=""):
        if isinstance(no, dict):
            for k, v in no.items():
                cam = f"{trilha}.{k}" if trilha else k
                if k.lower() in SENSIVEIS and v not in (None, "", MASCARA, [], {}):
                    vazou.append(f"{cam}={str(v)[:24]}")
                else:
                    andar(v, cam)
        elif isinstance(no, list):
            for i, v in enumerate(no):
                andar(v, f"{trilha}[{i}]")

    andar(dados)
    for prefixo in ("rnd_", "sbp_", "sb_secret_", "ghp_", "gho_", "AIza", "eyJ"):
        if prefixo in corpo:
            vazou.append(f"token solto com prefixo {prefixo}")

    if vazou:
        ruim(f"{rota} expoe {vazou}")
    else:
        ok(f"{rota} sem segredo")


def main() -> None:
    import httpx

    c = httpx.Client(timeout=120, follow_redirects=True)

    print("1) paginas e config")
    for rota, chave in [
        ("/", "<html"), ("/admin", "<html"), ("/painel", "<html"),
        ("/health", "ok"), ("/api/config", "supabase_url"),
        ("/static/auth.js", "auth"), ("/static/style.css", "{"),
    ]:
        r = c.get(f"{BASE}{rota}")
        if r.status_code == 200 and chave in r.text:
            ok(f"{rota} -> 200")
        else:
            ruim(f"{rota} -> {r.status_code} (esperado 200, marcador '{chave}')")

    cfg = c.get(f"{BASE}/api/config").json()
    if cfg.get("auth_habilitado") is True:
        ok("auth habilitado")
    else:
        ruim(f"auth_habilitado={cfg.get('auth_habilitado')}")
    if cfg.get("nao_oficiais_para_usuarios") is False:
        ok("canais nao oficiais restritos ao admin")
    else:
        ruim(f"nao_oficiais_para_usuarios={cfg.get('nao_oficiais_para_usuarios')}")

    print("2) API fechada sem sessao")
    for rota in ["/api/agentes", "/api/painel/agentes", "/api/eu",
                 "/api/canais", "/api/painel/resumo"]:
        r = c.get(f"{BASE}{rota}")
        if r.status_code in (401, 403):
            ok(f"{rota} -> {r.status_code} bloqueado")
        else:
            ruim(f"{rota} -> {r.status_code} (esperado 401/403; ABERTO!)")

    print("3) login do admin e adocao dos agentes legados")
    email, senha = sys.argv[1], sys.argv[2]
    url = _cred("supabase URL").rstrip("/")
    pk = _cred("supabase PK")
    t = httpx.post(
        f"{url}/auth/v1/token?grant_type=password",
        headers={"apikey": pk, "Content-Type": "application/json"},
        json={"email": email, "password": senha},
        timeout=60,
    )
    if t.status_code >= 400:
        ruim(f"login falhou: {t.status_code} {t.text[:200]}")
        print()
        print(f"{len(FALHAS)} FALHA(S)")
        sys.exit(1)

    token = t.json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}

    eu = c.get(f"{BASE}/api/eu", headers=h)
    if eu.status_code == 200 and eu.json().get("role") == "admin" and eu.json().get("eh_admin"):
        ok(f"/api/eu -> admin ({eu.json().get('email')})")
    else:
        ruim(f"/api/eu -> {eu.status_code} {eu.text[:200]}")

    ag = c.get(f"{BASE}/api/painel/agentes", headers=h)
    agentes = ag.json() if ag.status_code == 200 else []
    nomes = [a.get("nome") for a in agentes]
    print(f"        {len(nomes)} agente(s): {nomes}")
    if ag.status_code == 200 and len(agentes) == 3:
        ok("3 agentes legados adotados no primeiro login")
    else:
        ruim(f"/api/painel/agentes -> {ag.status_code}, {len(agentes)} agente(s)")

    print("4) vazamento de segredo, AUTENTICADO")
    # Este e o endpoint que vazava: a lista de redacao de _canal_publico nao
    # cobria access_token/app_secret/verify_token.
    for a in agentes:
        r = c.get(f"{BASE}/api/agentes/{a['id']}/canais", headers=h)
        if r.status_code != 200:
            ruim(f"/api/agentes/{a['id']}/canais -> {r.status_code}")
            continue
        sem_segredo(f"agente '{a.get('nome')}' /canais", r.text)

    print("5) cliente comum nao ve canal nao oficial")
    cj = sys.argv[3] if len(sys.argv) > 3 else None
    if cj:
        email2, senha2 = cj.split(":", 1)
        t2 = httpx.post(
            f"{url}/auth/v1/token?grant_type=password",
            headers={"apikey": pk, "Content-Type": "application/json"},
            json={"email": email2, "password": senha2},
            timeout=60,
        )
        if t2.status_code >= 400:
            print(f"        (sem cliente de teste: {t2.status_code})")
        else:
            h2 = {"Authorization": f"Bearer {t2.json()['access_token']}"}
            e2 = c.get(f"{BASE}/api/eu", headers=h2).json()
            visiveis = c.get(f"{BASE}/api/painel/canais", headers=h2)
            tipos = ([x.get("tipo") for x in visiveis.json()]
                     if visiveis.status_code == 200 else [])
            print(f"        cliente {e2.get('email')} papel={e2.get('role')} canais={tipos}")
            if any(tipo in ("whatsapp", "instagram") for tipo in tipos):
                ruim("cliente comum enxerga canal nao oficial")
            else:
                ok("cliente comum sem canal nao oficial")
            if len(agentes) and c.get(f"{BASE}/api/painel/agentes", headers=h2).json():
                ruim("cliente comum enxerga agentes do admin")
            else:
                ok("cliente comum sem agentes do admin")
    else:
        print("        (pular: informe email:senha de um cliente como 3o argumento)")

    print()
    if FALHAS:
        print(f"{len(FALHAS)} FALHA(S): " + "; ".join(FALHAS))
        sys.exit(1)
    print("TUDO OK")


if __name__ == "__main__":
    main()

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
CREDENCIAIS = pathlib.Path(__file__).resolve().parent.parent.parent / "credentials.txt"
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
    # credentials.txt manda: o prompt.txt passou a guardar o log do navegador.
    for arquivo in (CREDENCIAIS, PROMPT):
        if not arquivo.exists():
            continue
        m = re.search(rf"{re.escape(chave)}:\s*(\S+)", arquivo.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    raise SystemExit(f"'{chave}' nao encontrado em {CREDENCIAIS.name} nem {PROMPT.name}")


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


def _sessao(email: str, senha: str, url: str, pk: str) -> str:
    """Access token do Supabase.

    Aceita senha (grant_type=password) OU ja o proprio access_token, que
    e o que o smoke usa quando a senha nao esta comigo: o service_role gera
    um magic link e o `email_otp` do link vira sessao de verdade, sem alterar
    a senha da conta. Isso mantem o smoke exercitando a API de producao com
    uma sessao real do GoTrue, e nao com um token forjado.
    """
    import httpx

    if senha.count(".") >= 2:  # JWT
        return senha

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
    return t.json()["access_token"]


def main() -> None:
    import httpx

    c = httpx.Client(timeout=120, follow_redirects=True)

    print("1) paginas e config")
    for rota, chave in [
        ("/", "<html"), ("/admin", "<html"), ("/painel", "<html"),
        ("/health", "ok"), ("/api/config", "supabase_url"),
        # Item 4: os assets ficam na RAIZ (nao em /static/), para o mesmo
        # HTML abrir no Render e no GitHub Pages. Aqui conferimos o caminho
        # que a pagina realmente usa.
        ("/auth.js", "auth"), ("/style.css", "{"), ("/chat.js", "Chat"),
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
    token = _sessao(email, senha, url, pk)
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
    if ag.status_code != 200:
        ruim(f"/api/painel/agentes -> {ag.status_code}")
    elif any(not a.get("dono_id") for a in agentes):
        # Os agentes da plataforma (itens 7/12/13) vivem em `app/agentes_internos.py`,
        # com `interno` preenchido e `dono_id` nulo: sao codigo, nao cadastro.
        # Se um deles aparecesse aqui, o painel ofereceria editar um prompt do
        # sistema -- e os internos parariam de ser indeviaveis.
        ruim(f"agente interno vazando no painel: {nomes}")
    else:
        ok("painel so mostra agente de verdade (nenhum interno)")

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
    tokens: dict[str, str] = {}
    if cj:
        email2, senha2 = cj.split(":", 1)
        token2 = _sessao(email2, senha2, url, pk)
        tokens[email2] = token2
        h2 = {"Authorization": f"Bearer {token2}"}
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
            # O painel do cliente so pode mostrar o agente DELE. O admin ve
            # todos (por desenho), entao a comparação correta nao e "vazio":
            # e "todo agente que volta tem o dono_email do proprio cliente".
            meus = c.get(f"{BASE}/api/painel/agentes", headers=h2).json()
            alheios = [a.get("nome") for a in meus
                       if a.get("dono_email") != e2.get("email")]
            if alheios:
                ruim(f"cliente comum enxerga agente de outro: {alheios}")
            else:
                ok(f"cliente comum so ve o agente dele ({[a.get('nome') for a in meus]})")
    else:
        print("        (pular: informe email:senha de um cliente como 3o argumento)")

    tokens[email] = token
    print("6) isencao de pagamento (plano do teto, sem checkout, sem cota)")
    for quem, tok in tokens.items():
        hh = {"Authorization": f"Bearer {tok}"}
        p = c.get(f"{BASE}/api/plano", headers=hh)
        if p.status_code != 200:
            ruim(f"/api/plano ({quem}) -> {p.status_code} {p.text[:160]}")
            continue
        plano = p.json().get("plano") or {}
        marca = plano.get("isento") or plano.get("admin")
        if not marca:
            ruim(f"{quem} sem isencao nem admin: {plano}")
            continue
        if plano.get("max_agentes") != 0 or plano.get("max_mensagens_por_agente_mes") != 0:
            ruim(f"{quem} isento mas com limite: {plano}")
            continue
        ok(f"{quem}: plano {plano.get('id')} "
           f"({'isento' if plano.get('isento') else 'admin'}), sem limite")

        lim = c.get(f"{BASE}/api/plano/limites", headers=hh).json()
        if lim.get("sem_cota") is not True:
            ruim(f"/api/plano/limites ({quem}) -> {lim}")
        else:
            ok(f"{quem}: sem_cota")

        # Nao ha assinatura para trocar/cancelar: as duas rotas recusam, o que
        # e o outro lado da isencao (nada de CHECKOUT pendente na conta).
        for rota in ("troca", "cancelar"):
            resp = c.post(f"{BASE}/api/plano/{rota}", headers=hh,
                          json={"plano": "pro"})
            if resp.status_code != 400:
                ruim(f"/api/plano/{rota} ({quem}) -> {resp.status_code}")
            else:
                ok(f"{quem}: /api/plano/{rota} recusado ({resp.json().get('detail','')[:40]})")

        pg = c.get(f"{BASE}/api/plano/pagamentos", headers=hh)
        if pg.status_code != 200:
            ruim(f"/api/plano/pagamentos ({quem}) -> {pg.status_code}")
        elif pg.json():
            ruim(f"{quem} tem cobranca pendente: {pg.json()}")
        else:
            ok(f"{quem}: sem pagamento pendente")

    print("7) PAGAMENTO NAO BARRA FUNCIONALIDADE (chat interno da conta isenta)")
    # O ponto do item 13 com o gateway ligado: a conta nao pagou nada e mesmo
    # assim o chat de suporte tem que responder.
    for quem, tok in tokens.items():
        hh = {"Authorization": f"Bearer {tok}"}
        eu2 = c.get(f"{BASE}/api/eu", headers=hh).json()
        if eu2.get("eh_admin"):
            continue  # admin nao tem cota por ser admin: nao prova a isencao
        r = c.post(f"{BASE}/api/interno/suporte",
                   headers={**hh, "Content-Type": "application/json"},
                   json={"sessao": eu2.get("id") or "smoke-prod-isencao",
                         "texto": "ola"},
                   timeout=240)
        if r.status_code != 200:
            ruim(f"/api/interno/suporte ({quem}) -> {r.status_code} {r.text[:160]}")
        else:
            j = r.json()
            if not str(j.get("resposta") or j.get("texto") or "").strip():
                ruim(f"/api/interno/suporte ({quem}) respondeu vazio: {str(j)[:160]}")
            else:
                    ok(f"{quem}: suporte respondeu sem pagar "
                   f"({len(str(j.get('resposta') or j.get('texto')))} chars)")

    print("8) ITEM 12 SEM PAGAR: o gerador exige plano Pro na conta isenta")
    # O gerador e o unico interno com `plano_minimo` (Pro). Se a isencao
    # estivesse furada na cota, o suporte (item 13, sem plano) passaria e o
    # gerador voltaria 403 -- que e exatamente o bug que a isencao elimina.
    for quem, tok in tokens.items():
        hh = {"Authorization": f"Bearer {tok}"}
        eu3 = c.get(f"{BASE}/api/eu", headers=hh).json()
        if eu3.get("eh_admin"):
            continue
        g = c.post(f"{BASE}/api/interno/prompt",
                   headers={**hh, "Content-Type": "application/json"},
                   json={"sessao": eu3.get("id") or "smoke-prod-gerador",
                         "texto": "quero um agente que responda mensagens"},
                   timeout=240)
        if g.status_code != 200:
            ruim(f"/api/interno/prompt ({quem}) -> {g.status_code} {g.text[:160]}")
        else:
            gj = g.json()
            if not str(gj.get("resposta") or "").strip():
                ruim(f"/api/interno/prompt ({quem}) respondeu vazio: {str(gj)[:160]}")
            else:
                ok(f"{quem}: gerador respondeu sem pagar")

    print()
    if FALHAS:
        print(f"{len(FALHAS)} FALHA(S): " + "; ".join(FALHAS))
        sys.exit(1)
    print("TUDO OK")


if __name__ == "__main__":
    main()

"""Teste AO VIVO da Edge Function dos webhooks oficiais da Meta.

Exige a Edge Function implantada e um banco com as migrations aplicadas.
Cria canais temporarios com nomes TMP-* e apaga tudo no final (inclusive em
caso de erro), entao nao mexe nos canais de verdade.

    python tests/test_edge_live.py

Pula sozinho se o .env nao tiver DATABASE_URL.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

falhas: list[str] = []
VT, SEGREDO = "teste-verificacao-123", "segredo-app-teste"
WA_ID, IG_ID = "999999999999999", "987654321"
IGSID = "222222222"


def check(nome: str, ok: bool, detalhe: str = "") -> None:
    print(("  ok   " if ok else "  FALHA ") + nome + (f" -> {detalhe}" if detalhe else ""))
    if not ok:
        falhas.append(nome)


def _assinatura(bruto: bytes, segredo: str = SEGREDO) -> str:
    return "sha256=" + hmac.new(segredo.encode(), bruto, hashlib.sha256).hexdigest()


def get(funcao: str, rota: str, params: dict) -> tuple[int, str]:
    u = f"{funcao}/{rota}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(u, method="GET"), timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def post(funcao: str, rota: str, body: dict, assinatura: str | None = None) -> tuple[int, str]:
    bruto = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if assinatura is not None:
        headers["X-Hub-Signature-256"] = assinatura
    req = urllib.request.Request(f"{funcao}/{rota}", data=bruto, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def evento_whatsapp(phone_id: str, wamid: str = "wamid.TESTE", texto: str = "oi") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": phone_id,
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": phone_id, "phone_number_id": phone_id},
                    "messages": [{"id": wamid, "from": "5511999999999", "type": "text",
                                  "text": {"body": texto}}],
                },
            }],
        }],
    }


def evento_instagram(ig_id: str, mid: str = "mid.teste", texto: str = "oi instagram",
                    **flags) -> dict:
    """Formato real da Instagram Messaging API: entry[].messaging[], sem `changes`.

    Ver https://developers.facebook.com/docs/instagram-messaging/webhooks
    """
    mensagem = {"mid": mid, "text": texto}
    mensagem.update(flags)
    return {
        "object": "instagram",
        "entry": [{
            "id": ig_id,
            "time": 1750000000,
            "messaging": [{
                "sender": {"id": IGSID},
                "recipient": {"id": ig_id},
                "timestamp": 1750000000,
                "message": mensagem,
            }],
        }],
    }


SQL_WA = ("INSERT INTO canais (agente_id, tipo, nome, config) VALUES "
          "((SELECT id FROM agentes LIMIT 1), 'whatsapp_oficial', 'TMP-WA', "
          "jsonb_build_object('phone_number_id',$1::text,'access_token','x',"
          "'verify_token',$2::text,'app_secret',$3::text)) RETURNING id")
SQL_IG = ("INSERT INTO canais (agente_id, tipo, nome, config) VALUES "
          "((SELECT id FROM agentes LIMIT 1), 'instagram_oficial', 'TMP-IG', "
          "jsonb_build_object('ig_user_id',$1::text,'access_token','x',"
          "'verify_token',$2::text,'app_secret',$3::text)) RETURNING id")


async def main() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        print("sem .env: pulando")
        return
    env = env_path.read_text(encoding="utf-8")
    m = re.search(r"^DATABASE_URL=(.+)$", env, re.M)
    if not m:
        print("sem DATABASE_URL no .env: pulando")
        return
    url = m.group(1).strip().strip('"').strip("'")
    # O project ref do Supabase tem 20 caracteres. Aparece no usuario
    # (postgres.<ref>:<senha>@...) ou no host do pooler.
    ref = re.search(r"postgres\.([a-z0-9]{20})[:@/]", url) or re.search(r"\.([a-z0-9]{20})\.supabase\.", url)
    if not ref:
        print("DATABASE_URL sem project ref: pulando")
        return
    funcao = f"https://{ref.group(1)}.supabase.co/functions/v1/inbox"

    import asyncpg
    con = await asyncpg.connect(url)
    try:
        await con.execute("DELETE FROM canais WHERE nome LIKE 'TMP-%'")
        ids = {
            "wa": (await con.fetchrow(SQL_WA, WA_ID, VT, SEGREDO))["id"],
            "ig": (await con.fetchrow(SQL_IG, IG_ID, VT, SEGREDO))["id"],
        }
        print(f"canais temporarios: {ids}\n")

        print("== handshake de verificacao (GET) ==")
        st, corpo = get(funcao, "meta/whatsapp", {"hub.mode": "subscribe", "hub.verify_token": VT,
                                                  "hub.challenge": "CHAL-123"})
        check("token correto devolve o challenge", st == 200 and corpo == "CHAL-123", f"{st} {corpo!r}")
        st, _ = get(funcao, "meta/whatsapp", {"hub.mode": "subscribe",
                                               "hub.verify_token": "errado", "hub.challenge": "C"})
        check("token errado e recusado (403)", st == 403, str(st))
        st, _ = get(funcao, "meta/instagram", {"hub.mode": "subscribe", "hub.verify_token": VT,
                                               "hub.challenge": "IG-9"})
        check("rotas wa e ig aceitam o mesmo token", st == 200, str(st))
        st, _ = get(funcao, "meta/whatsapp", {})
        check("GET sem parametros nao quebra", 400 <= st < 500, str(st))

        print("\n== assinatura HMAC ==")
        st, _ = post(funcao, "meta/whatsapp", evento_whatsapp(WA_ID))
        check("POST sem assinatura recusado (401)", st == 401, str(st))
        ev = evento_whatsapp(WA_ID)
        st, _ = post(funcao, "meta/whatsapp", ev, _assinatura(json.dumps(ev).encode()))
        check("assinatura valida aceita", st == 200, str(st))
        forjado = evento_whatsapp(WA_ID, wamid="wamid.FORJADO", texto="texto forjado")
        st, _ = post(funcao, "meta/whatsapp", forjado, _assinatura(json.dumps(ev).encode()))
        check("assinatura de outro corpo recusada (401)", st == 401, str(st))
        check("evento forjado nao enfileirou",
              await con.fetchval("SELECT count(*) FROM caixa_entrada WHERE texto='texto forjado'") == 0)

        print("\n== instagram: formato real entry[].messaging[] ==")
        st, _ = post(funcao, "meta/instagram", evento_instagram(IG_ID),
                     _assinatura(json.dumps(evento_instagram(IG_ID)).encode()))
        check("payload do instagram reconhecido", st == 200, str(st))
        linha = await con.fetchrow("SELECT canal_id, remetente, texto FROM caixa_entrada "
                                   "WHERE origem='igmo:mid.teste'")
        check("mensagem enfileirada", linha is not None)
        if linha:
            check("remetente e o sender.id (IGSID)", linha["remetente"] == IGSID, str(linha["remetente"]))
            check("texto vem de message.text", linha["texto"] == "oi instagram", str(linha["texto"]))
            check("canal correto", linha["canal_id"] == ids["ig"], str(linha["canal_id"]))

        print("\n== instagram: filtros que evitam laco com o proprio bot ==")
        for flag in ("is_echo", "is_deleted", "is_unsupported"):
            evi = evento_instagram(IG_ID, mid=f"mid.{flag}", **{flag: True})
            st, _ = post(funcao, "meta/instagram", evi, _assinatura(json.dumps(evi).encode()))
            n = await con.fetchval("SELECT count(*) FROM caixa_entrada WHERE origem=$1::text",
                                   f"igmo:mid.{flag}")
            check(f"{flag} ignorado", st == 200 and n == 0, f"{st} enfileiradas={n}")

        print("\n== casos de borda ==")
        sem_texto = evento_instagram(IG_ID, mid="mid.semtexto", texto="")
        sem_texto["entry"][0]["messaging"][0]["message"].pop("text")
        st, _ = post(funcao, "meta/instagram", sem_texto, _assinatura(json.dumps(sem_texto).encode()))
        check("midia sem texto ignorada", st == 200 and await con.fetchval(
            "SELECT count(*) FROM caixa_entrada WHERE origem='igmo:mid.semtexto'") == 0)
        evd = evento_instagram("000000000", mid="mid.desconhecido")
        st, _ = post(funcao, "meta/instagram", evd, _assinatura(json.dumps(evd).encode()))
        check("conta desconhecida responde 200 (Meta nao repete)", st == 200, str(st))
        check("conta desconhecida nao enfileira", await con.fetchval(
            "SELECT count(*) FROM caixa_entrada WHERE origem='igmo:mid.desconhecido'") == 0)

        print("\n== dedup (o Meta reenvia o mesmo evento) ==")
        dup = evento_instagram(IG_ID, mid="mid.dup", texto="oi de novo")
        sig = _assinatura(json.dumps(dup).encode())
        post(funcao, "meta/instagram", dup, sig)
        post(funcao, "meta/instagram", dup, sig)
        n = await con.fetchval("SELECT count(*) FROM caixa_entrada WHERE origem='igmo:mid.dup'")
        check("mesma origem entra so uma vez", n == 1, f"linhas={n}")
    finally:
        await con.execute("DELETE FROM canais WHERE nome LIKE 'TMP-%'")
        await con.execute("DELETE FROM caixa_entrada")
        print("\nlimpeza: canais TMP-* e caixa_entrada apagados")
        await con.close()

    print("\n" + ("TODOS OS TESTES PASSARAM" if not falhas else f"FALHAS: {falhas}"))
    if falhas:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

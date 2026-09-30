"""Fumaca contra o banco real, usando as mesmas credenciais da app.

As migrations foram aplicadas pela Management API (o `postgres` do prompt.txt
tem a senha como placeholder), entao o risco agora e o oposto: a revogacao do
`anon` e a RLS terem quebrado quem a app realmente usa. A app conecta com
`postgres.<ref>` pelo pooler, e nao com service_role nem com anon, entao ela
precisa continuar enxergando e escrevendo exatamente como antes.

    python scripts/smoke_db.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(RAIZ / ".env")


async def main() -> None:
    from app.database import close_pool, get_pool

    pool = await get_pool()
    falhas = []

    async def checa(nome: str, sql: str, esperado=None):
        try:
            async with pool.acquire() as con:
                v = await con.fetchval(sql)
            ok = True if esperado is None else v == esperado
            print(f"  {'ok   ' if ok else 'FALHA'} {nome}: {v}")
            if not ok:
                falhas.append(nome)
        except Exception as e:
            print(f"  FALHA {nome}: {type(e).__name__}: {e}")
            falhas.append(nome)

    # As contagens abaixo sao APENAS informativas (`esperado=None`). Elas
    # estavam fixadas em 3/4/61/1 e o banco real já tem outros números, então
    # a fumaça acusava FALHA em tudo que estava certo e o sinal real — "a app
    # não enxerga mais a tabela" — se perdia no barulho. O que é-structure
    # continua com valor esperado exato, porque esse não muda com o uso.
    print("\n== a app ainda le os dados que ja existem (contagem atual) ==")
    await checa("agentes", "SELECT count(*) FROM agentes")
    await checa("canais", "SELECT count(*) FROM canais")
    await checa("mensagens", "SELECT count(*) FROM mensagens")

    print("\n== as tabelas novas existem e respondem ==")
    await checa("perfis", "SELECT count(*) FROM perfis")
    await checa("perfis tem o dono da conta do Auth",
                "SELECT count(*) FROM perfis p JOIN auth.users u ON u.id = p.id")
    await checa("app_config legivel", "SELECT count(*) FROM app_config WHERE chave='admin_emails'")
    await checa("dono_id existe em agentes",
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='agentes' AND column_name='dono_id'", 1)

    print("\n== a app ignora RLS (postgres tem BYPASSRLS) ==")
    await checa("consegue ler another's dado: agentes sem dono",
                "SELECT count(*) FROM agentes WHERE dono_id IS NULL")
    await checa("consegue ler perfis de todos", "SELECT count(*) FROM perfis")

    print("\n== escrita funciona (INSERT + UPDATE + rollback) ==")
    # O teste de escrita vai em `agentes` porque e onde a app passa a gravar o
    # dono_id. Tentar inserir em `perfis` com um uuid inventado era um teste
    # errado: a FK para auth.users rejeita, e com razao.
    dono = await (await get_pool()).fetchval("SELECT id FROM perfis LIMIT 1")
    try:
        async with pool.acquire() as con:
            async with con.transaction():
                novo = await con.fetchval(
                    "INSERT INTO agentes (nome, system_prompt, dono_id) "
                    "VALUES ('__fumaca__', 'x', $1) RETURNING id",
                    dono,
                )
                dono_voltou = await con.fetchval(
                    "SELECT dono_id FROM agentes WHERE id = $1", novo)
                ok = dono_voltou == dono
                print(f"  {'ok   ' if ok else 'FALHA'} INSERT em agentes com dono_id: {dono_voltou}")
                if not ok:
                    falhas.append("insert dono_id")
                # A FK tem de recusar um dono que nao existe.
                try:
                    await con.execute(
                        "UPDATE agentes SET dono_id = gen_random_uuid() WHERE id = $1", novo)
                    print("  FALHA dono invalido foi aceito")
                    falhas.append("fk nao protege")
                except Exception:
                    print("  ok   FK recusa dono inexistente")
                raise Exception("rollback proposital")  # desfaz o teste
    except Exception as e:
        if "rollback proposital" not in str(e):
            print(f"  FALHA transacao: {e}")
            falhas.append("transacao")

    await close_pool()
    print("\n" + ("FALHAS: " + ", ".join(falhas) if falhas else "TUDO OK"))
    sys.exit(1 if falhas else 0)


if __name__ == "__main__":
    asyncio.run(main())

"""Remove contas de teste do GoTrue pelo caminho que funciona aqui.

A API de admin do GoTrue (`/auth/v1/admin/users`) devolve 403 com o PAT: ela
exige a chave `service_role`, que o projeto nao tem guardada. O que existe e o
PAT da Management API, que roda SQL. Por isso a remocao e feita em SQL, e nao
pela API.

Ordem que importa: `public.perfis` guarda o user id na coluna `id` e nao tem
FK para `auth.users`, entao some primeiro. O resto (`identities`, `sessions`,
`refresh_tokens`) cai por cascata ao apagar a linha em `auth.users`.

O script primeiro mostra o estado, so apaga os e-mails passados na linha de
comando e mostra o resultado. Nao existe "apaga tudo" acidental.

    python scripts/remover_conta.py diag.final.temp@gmail.com
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from supabase_sql import sql  # noqa: E402


def listar() -> list[dict]:
    return sql(
        "select u.email, u.id, coalesce(p.role, 'user') as role "
        "from auth.users u left join public.perfis p on p.id = u.id "
        "order by u.created_at"
    )


def main() -> None:
    alvos = sys.argv[1:]
    if not alvos:
        raise SystemExit(__doc__)

    print("antes:")
    for u in listar():
        print(f"  {u['email']:<42} {u['role']}")

    for email in alvos:
        # Sem o id na URL o e-mail basta, mas conferimos o id para nao apagar
        # outra conta com nome parecido.
        alvo = next((u for u in listar() if u["email"] == email), None)
        if alvo is None:
            print(f"\n  {email}: nao existe, nada a fazer")
            continue
        sql(f"delete from public.perfis where id = '{alvo['id']}'")
        sql(f"delete from auth.users where id = '{alvo['id']}' and email = '{email}'")
        print(f"\n  {email}: removida (id={alvo['id']})")

    print("\ndepois:")
    for u in listar():
        print(f"  {u['email']:<42} {u['role']}")


if __name__ == "__main__":
    main()

"""Cria (ou reaproveita) os agentes internos no banco e confere.

Roda o mesmo UPSERT do boot (`repo.garantir_agentes_internos`) para nao esperar
o proximo deploy, e mostra o que ficou gravado — inclusive o `system_prompt`,
para dar para conferir que a linha tem o mesmo prompt que o codigo (e que,
mesmo se divergisse, quem manda no chat continua sendo o codigo).

    python scripts/criar_internos.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import agentes_internos as ai  # noqa: E402
from app import repositories as repo  # noqa: E402
from app.database import close_pool  # noqa: E402


async def main() -> None:
    pares = await repo.garantir_agentes_internos([
        (a.chave, a.nome, a.prompt) for a in ai.AGENTES.values()
    ])
    for par in pares:
        agente, canal = par["agente"], par["canal"]
        igual = agente["system_prompt"] == ai.get(agente["interno"]).prompt
        print(f"{agente['interno']:>8}  agente={agente['id']} canal={canal['id']}"
              f"  prompt igual ao codigo: {igual}")
    # Confere de novo: o segundo UPSERT tem que devolver os mesmos ids, e nao
    # criar um agente novo. E o que garante que o historico da home nao se perde
    # a cada deploy.
    antes = [(p["agente"]["id"], p["canal"]["id"]) for p in pares]
    depois = await repo.garantir_agentes_internos([
        (a.chave, a.nome, a.prompt) for a in ai.AGENTES.values()
    ])
    depois_ids = [(p["agente"]["id"], p["canal"]["id"]) for p in depois]
    print("idempotente:", antes == depois_ids, antes)
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

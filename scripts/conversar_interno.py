"""Fala com os agentes internos de verdade, para ver a resposta inteira.

Nao e teste: e o jeito de ler o que o modelo respondeu com os olhos antes de
colocar na tela. Cada pergunta vai com a acao que se espera ver, e o script
imprime o texto, a acao executada e o historico que ficou no banco.

    python scripts/conversar_interno.py produto
    python scripts/conversar_interno.py suporte "quando acaba meu plano?"
    python scripts/conversar_interno.py prompt "quero um bot de vendas de pizza"
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import agentes_internos as ai  # noqa: E402
from app.chat_interno import _falar  # noqa: E402
from app import repositories as repo  # noqa: E402
from app.database import close_pool  # noqa: E402


async def main() -> None:
    chave = sys.argv[1] if len(sys.argv) > 1 else "produto"
    falas = sys.argv[2:] or ["oi"]
    agente = ai.get(chave)
    if agente is None:
        raise SystemExit(f"agente desconhecido: {chave}")

    # Quem esta falando. Com `USUARIO_ID` no ambiente, o script fala como essa
    # conta (o caso do suporte e do gerador); sem ele, como um visitante
    # anonimo, que e o caso da home.
    import os

    usuario_id = os.environ.get("USUARIO_ID") or None
    externo = f"script-{uuid.uuid4()}" if not usuario_id else usuario_id
    _agente_row, canal = await repo.par_interno(chave)
    print(f"== {agente.nome} (canal {canal['id']}, sessao {externo[:16]}…)\n")

    for fala in falas:
        corpo = _corpo(fala)
        r = await _falar(agente, canal, externo, corpo, usuario_id)
        print(f"voce: {fala}")
        print(f"IA:   {r['resposta']}")
        if r.get("prompt_gerado"):
            print(f"prompt_gerado: {r['prompt_gerado']}")
        if r["acao"]:
            print(f"acao: {r['acao']['nome']} -> {r['acao']['dados']}")
        print()

    await close_pool()


def _corpo(texto: str):
    from app.chat_interno import Mensagem

    return Mensagem(sessao=uuid.uuid4().hex, texto=texto)


if __name__ == "__main__":
    asyncio.run(main())

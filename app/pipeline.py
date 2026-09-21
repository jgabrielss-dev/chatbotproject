from __future__ import annotations

import logging

from app import repositories as repo
from app.ai import gemini

log = logging.getLogger("pipeline")


async def processar_mensagem(
    agente: dict,
    canal: dict,
    usuario_externo: str,
    texto: str,
) -> str:
    """Fluxo completo: sessão -> salvar user -> Gemini(hist+memoria) -> salvar IA -> atualizar memória."""
    sessao = await repo.obter_ou_criar_sessao(agente["id"], canal["id"], usuario_externo)
    await repo.salvar_mensagem(sessao["id"], False, texto)

    historico = await repo.historico_sessao(sessao["id"])
    memoria = gemini.ler_memoria(sessao.get("memoria"))

    resposta = gemini.responder(agente["system_prompt"], historico, texto, memoria)
    await repo.salvar_mensagem(sessao["id"], True, resposta)

    trechos = await repo.ultimos_trechos(sessao["id"], 6)
    nova_memoria = gemini.atualizar_memoria(agente["system_prompt"], memoria, trechos)
    if nova_memoria != memoria:
        await repo.atualizar_memoria(sessao["id"], nova_memoria)

    return resposta
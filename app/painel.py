"""Rotas do menu do cliente (`/painel`).

Fica separado do `main.py` de propósito: estas rotas são todas de leitura de um
tenant só, enquanto as de `main.py` são as de administração (e de webhook). A
separação deixa óbvio onde a sessão é exigida e onde a posse é conferida.

Todas passam por `usuario_atual` (que o middleware já populou) e repassam o
`dono_id` para o repositório. Nada aqui aceita um `dono_id` vindo do cliente:
o dono é sempre a conta logada, e o admin — que enxerga tudo — é o único caso
em que ele é `None`.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app import repositories as repo
from app.auth import usuario_atual

log = logging.getLogger("painel")

router = APIRouter(prefix="/api/painel", tags=["painel"])


def _dono(usuario) -> str | None:
    """Admin vê a plataforma inteira; o resto vê só a própria conta."""
    return None if usuario.eh_admin else usuario.id


async def _agente_permitido(agente_id: int, usuario) -> dict:
    agente = await repo.obter_agente(agente_id, _dono(usuario))
    if not agente:
        # 404 em vez de 403: confirmar "existe, mas é de outro" já entrega
        # informação sobre a conta de terceiro.
        raise HTTPException(404, "Agente não encontrado.")
    return agente


@router.get("/resumo")
async def resumo(request: Request):
    """Números da home: agentes, canais, sessões, mensagens e fila."""
    usuario = usuario_atual(request)
    return {
        "estatisticas": await repo.estatisticas_do_dono(_dono(usuario)),
        "caixa": await repo.resumo_caixa(limite=5, dono_id=_dono(usuario)),
    }


@router.get("/agentes")
async def agentes(request: Request):
    return await repo.listar_agentes(_dono(usuario_atual(request)))


@router.get("/agentes/{agente_id}")
async def agente(agente_id: int, request: Request):
    """Detalhe do agente com os canais: é o que o menu do cliente monta."""
    usuario = usuario_atual(request)
    dado = await _agente_permitido(agente_id, usuario)
    canais = await repo.listar_canais(agente_id)
    dado["canais"] = [repo.canal_publico(c) for c in canais]
    return dado


@router.get("/agentes/{agente_id}/sessoes")
async def sessoes(agente_id: int, request: Request):
    """Conversas do agente, com a memória que a IA extraiu de cada cliente.

    É o dado que diferencia este SaaS de um cadastro de bot: o dono vê o que o
    bot aprendeu sobre quem falou com ele.
    """
    usuario = usuario_atual(request)
    await _agente_permitido(agente_id, usuario)
    return await repo.listar_sessoes(agente_id)


@router.get("/sessoes/{sessao_id}")
async def sessao(sessao_id: int, request: Request):
    """Uma conversa inteira."""
    usuario = usuario_atual(request)
    s = await repo.obter_sessao_do_dono(sessao_id, _dono(usuario))
    if not s:
        raise HTTPException(404, "Sessão não encontrada.")
    s["mensagens"] = await repo.mensagens_da_sessao(sessao_id)
    return s


@router.get("/canais")
async def canais(request: Request):
    """Todos os canais do tenant, para a visão de overview."""
    return [repo.canal_publico(c)
            for c in await repo.listar_canais_do_dono(_dono(usuario_atual(request)))]


@router.get("/caixa")
async def caixa(request: Request):
    """Fila de mensagens do tenant (pendências e contadores)."""
    return await repo.resumo_caixa(dono_id=_dono(usuario_atual(request)))

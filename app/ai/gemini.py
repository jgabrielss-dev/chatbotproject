from __future__ import annotations

import json
import logging
from typing import Any

from google import genai
from google.genai import types

from app.config import settings

log = logging.getLogger("gemini")

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client

_PROMPT_MEMORIA = """Você é um sistema de memória de um assistente. Mantenha o perfil do usuário
num JSON. Regras:
- Comece SEMPRE do JSON atual fornecido e devolva uma versão possível de evolução.
- Adicione/atualize campos apenas com informações novas e relevantes que apareçam nas últimas mensagens.
- Não invente informações. Apague campos que ficaram incorretos.
- Responda EXCLUSIVAMENTE com o JSON. Sem explicações fora dele.

Perfil atual:
{memoria}

Últimas mensagens:
{mensagens}"""


def _montar_historico(historico: list[dict]) -> list[types.Content]:
    return [
        types.Content(
            role="user" if not m["de_ia"] else "model",
            parts=[types.Part(text=m["texto"])],
        )
        for m in historico
    ]


def _contexto_prompt(system_prompt: str, memoria: dict) -> str:
    corpo = system_prompt or ""
    if memoria:
        bloco = "\n\nMemória do usuário (dados que você registrou antes; use como informações confirmadas, mas corrija se o usuário contradizer):\n"
        bloco += json.dumps(memoria, ensure_ascii=False, indent=2)
        corpo += bloco
    return corpo


def _gerar(conteudo: str | list[types.Content], instrucao: str | None = None) -> str:
    if not settings.has_gemini:
        raise ValueError("Configure GEMINI_API_KEY no .env para usar a IA.")
    config = types.GenerateContentConfig(system_instruction=instrucao or None)
    resposta = _get_client().models.generate_content(
        model=settings.gemini_model,
        contents=conteudo,
        config=config,
    )
    return resposta.text.strip()


def responder(
    system_prompt: str,
    historico: list[dict],
    mensagem: str,
    memoria: dict | None = None,
) -> str:
    """Envia para o Gemini: system prompt + memória + histórico + nova mensagem."""
    instrucao = _contexto_prompt(system_prompt, memoria or {}) or None
    conteudo = [*_montar_historico(historico), types.Part(text=mensagem)]
    return _gerar(conteudo, instrucao)


def atualizar_memoria(
    system_prompt: str,
    memoria: dict,
    trechos: list[str],
) -> dict:
    """Pede ao Gemini para extrair/atualizar a memória JSONB da sessão."""
    if not settings.has_gemini:
        return memoria
    prompt = _PROMPT_MEMORIA.format(
        memoria=json.dumps(memoria, ensure_ascii=False, indent=2) or "{}",
        mensagens="\n".join(trechos) or "(nenhuma)",
    )
    try:
        resposta = _gerar(prompt)
        inicio = resposta.find("{")
        fim = resposta.rfind("}") + 1
        if inicio == -1 or fim <= inicio:
            return memoria
        novo = json.loads(resposta[inicio:fim])
        return novo if isinstance(novo, dict) else memoria
    except Exception as e:  # nunca quebra a conversa por causa da memória
        log.warning("Falha ao atualizar memória: %s", e)
        return memoria


def ler_memoria(memoria: Any) -> dict:
    if isinstance(memoria, str):
        try:
            return json.loads(memoria)
        except ValueError:
            return {}
    return memoria if isinstance(memoria, dict) else {}
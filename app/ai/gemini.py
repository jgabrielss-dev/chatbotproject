from __future__ import annotations

import asyncio
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
        _client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=45000),
        )
    return _client


def _modelos() -> list[str]:
    """Modelo principal + fallbacks (evita 503 de sobrecarga/descontinuacao)."""
    lista: list[str] = []
    for m in [settings.gemini_model, *settings.gemini_fallbacks.split(",")]:
        m = (m or "").strip()
        if m and m not in lista:
            lista.append(m)
    return lista

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


# Item 15: o que o agente recebe além de texto. Declarado no PROMPT DE SISTEMA
# (e não no texto que o cliente digita) para que valha também para os agentes
# internos dos itens 7, 12 e 13, que não têm prompt editável.
BLOCO_MULTIMODAL = """
## Anexos que você recebe
Além de texto, esta conversa pode vir com anexos. Quando vierem, eles chegaram
junto com a mensagem, e o que você vê deles é o conteúdo de verdade:

- `[foto]` — imagem. Você vê a imagem; descreva o que é relevante.
- `[audio]` — áudio. Você ouve; transcreva o que for fala e resuma o resto.
- `[video]` — vídeo. Você vê os quadros e ouve o áudio; descreva a cena.
- `[arquivo]` — documento (PDF, texto, planilha, código). Você lê o conteúdo.

Regras sobre anexos:
1. Um anexo marcado como "(não foi lido: ...)" NÃO chegou até você. Diga isso e
   peça para reenviar. Nunca descreva, adivinhe ou invente o conteúdo de um
   anexo que você não recebeu.
2. O texto entre colchetes é a descrição do arquivo (nome, tipo, tamanho), não o
   conteúdo dele. Se o anexo foi lido, o conteúdo está aí para você ver.
3. Anexos não voltam nas mensagens seguintes: se pedirem "e o que tinha na
   foto?", você tem o registro de que houve uma foto, não a foto. Peça de novo.
4. Responder a uma pergunta sobre um anexo é a prioridade daquela mensagem, mas
   o texto do usuário manda: se ele perguntou outra coisa, responda o que foi
   perguntado e só então ofereça o que viu no anexo.
"""


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
    # O item 15: a declaracao vai para TODO agente, e antes da memoria para que
    # a memoria (que e sobre a pessoa, nao sobre o canal) nao empurre a regra de
    # anexo para o fim do prompt, que e onde o modelo le com menos atencao.
    corpo = corpo + BLOCO_MULTIMODAL
    if memoria:
        bloco = "\n\nMemória do usuário (dados que você registrou antes; use como informações confirmadas, mas corrija se o usuário contradizer):\n"
        bloco += json.dumps(memoria, ensure_ascii=False, indent=2)
        corpo += bloco
    return corpo


def _gerar(conteudo: str | list[types.Content], instrucao: str | None = None) -> str:
    if not settings.has_gemini:
        raise ValueError("Configure GEMINI_API_KEY no .env para usar a IA.")
    config = types.GenerateContentConfig(
        system_instruction=instrucao or None,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    erro: Exception | None = None
    for modelo in _modelos():
        try:
            resposta = _get_client().models.generate_content(
                model=modelo,
                contents=conteudo,
                config=config,
            )
            return resposta.text.strip()
        except Exception as e:  # tenta o proximo modelo
            log.warning("Modelo %s indisponivel: %s", modelo, str(e)[:120])
            erro = e
    raise erro if erro else RuntimeError("Nenhum modelo Gemini disponivel.")


def _partes_da_mensagem(mensagem: str, anexos: list[bytes | tuple[str, bytes]]) -> list[types.Part]:
    """A mensagem nova vira N `Part`: o texto e um por anexo.

    Texto PRIMEIRO de propósito. O modelo anota o pedido e só depois olha a
    imagem; invertendo, ele descreve a foto e esquece a pergunta. E texto sempre
    presente, mesmo com anexos só: uma mensagem sem nenhuma `Part` de texto não
    é uma conversa, é um arquivo.
    """
    partes: list[types.Part] = [types.Part(text=mensagem or "")]
    for anexo in anexos or []:
        mime, dados = anexo if isinstance(anexo, tuple) else ("", anexo)
        if not dados:
            continue
        partes.append(types.Part(
            inline_data=types.Blob(mime_type=mime or "application/octet-stream",
                                   data=dados),
        ))
    return partes


async def responder(
    system_prompt: str,
    historico: list[dict],
    mensagem: str,
    memoria: dict | None = None,
    anexos: list[bytes | tuple[str, bytes]] | None = None,
) -> str:
    """Envia para o Gemini: system prompt + memória + histórico + mensagem (+ anexos).

    A biblioteca google-genai é síncrona e pode demorar 45s. Rodar isso direto no
    event loop congelaria o servidor inteiro (webhooks, worker da fila, keepalive)
    durante cada chamada, então tudo aqui vai para uma thread.

    `anexos` é lista de bytes, ou de `(mime, bytes)` — o item 15. O teto de
    tamanho é conferido antes, em `app/pipeline.py`, então aqui chega só o que
    cabe, e `_gerar` é chamado uma única vez com tudo.
    """
    instrucao = _contexto_prompt(system_prompt, memoria or {}) or None
    novas = types.Content(role="user", parts=_partes_da_mensagem(mensagem, anexos))
    conteudo = [*_montar_historico(historico), novas]
    return await asyncio.to_thread(_gerar, conteudo, instrucao)


async def atualizar_memoria(
    system_prompt: str,
    memoria: dict,
    trechos: list[str],
) -> dict:
    """Pede ao Gemini para extrair/atualizar a memória JSONB da sessão.

    Custa uma chamada de IA **por turno**, e só por isso: quem mede limite
    (item 9 e item 14) conta mensagens, e uma mensagem aqui vale duas chamadas.
    Fica escrito para ninguém achar que o limite de mensagens é o mesmo que a
    fatura.

    `system_prompt` entra na assinatura porque é o mesmo contexto do agente da
    sessão; hoje a memória é extraída sem ele (só os trechos), e o parâmetro
    fica para o dia em que a extração precisar saber de quem é a conversa.

    Nunca levanta: falha aqui devolve a memória de antes, e a conversa segue.
    """
    if not settings.has_gemini:
        return memoria
    return await asyncio.to_thread(_atualizar_memoria_sync, memoria, trechos)


def _atualizar_memoria_sync(memoria: dict, trechos: list[str]) -> dict:
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
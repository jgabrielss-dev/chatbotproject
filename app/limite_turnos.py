"""Teto de turnos do chat anônimo, por IP.

A cota do item 14 é por conversa, e a chave da conversa anônima é o campo
`sessao` — que o navegador inventa. Trocar o valor a cada mensagem cria uma
conversa nova a cada vez: os 12 turnos nunca acabam e cada um deles é uma
chamada de IA de verdade. Fica de graça para quem faz e caro para quem paga a
conta do Gemini, e nenhum botão da tela avisa que isso é o que está acontecendo.

O banco não resolve: ele sabe contar mensagem de uma conversa, não de uma
pessoa. O que segura é um contador em memória por IP, com duas janelas (hora e
dia). Em memória de propósito — o alvo é o abuso de agora, não a auditoria de
sempre, e o serviço é um processo só. O preço é o contador zerar no restart e
no deploy, o que é aceitável porque a cota por conversa continua valendo: quem
está abusando precisa gastar uma chamada de IA por turno para continuar.

Os números são altos de propósito. Quem conversa de verdade para nas 12
mensagens da cota, e várias pessoas atrás do mesmo IP (NAT de escritório,
rede móvel) compartilham o mesmo teto. Barrar uma pessoa por estar na mesma
empresa que outra seria pior que o abuso que isto previne. O que o teto segura
é a máquina que troca `sessao` em laço, que é o único jeito de passar da cota
por conversa.

Duas garantias de projeto:

* **Falha aqui é liberada.** O `checa_turno_anonimo` engole qualquer erro e
  devolve "liberado". Um bug de contagem neste arquivo não pode derrubar a
  venda da home, que é o que o módulo existe para proteger.
* **Só o anônimo entra.** Quem está logado tem consumo medido no mês
  (`registrar_consumo`) e plano de verdade: barrar essa pessoa aqui seria
  trancar quem já pagou por causa de um contador em memória.
"""

from __future__ import annotations

import logging
import time

from fastapi import Request

log = logging.getLogger("limite_turnos")

_HORA = 3600.0
_DIA = 86400.0

#: Quantos IPs o mapa guarda antes de varrer. Cada chave é um endereço visto
#: desde o último restart; sem teto, um varrão de origens diferentes enche a
#: memória do processo sem limite, e a memória do processo é a da venda.
_CHAVES_MAX = 4096

#: `ip` -> `[turnos_na_hora, inicio_da_hora, turnos_no_dia, inicio_do_dia]`.
#: Lista e não dataclass porque é estado interno e a mutação acontece toda em
#: `_conta`; o dicionário é só do módulo e `limpar()` devolve o mapa vazio.
_janelas: dict[str, list[float]] = {}


def chave_do_ip(request: Request) -> str:
    """Endereço de quem está chamando, do jeito que dá para confiar atrás do proxy.

    O `X-Forwarded-For` é uma cadeia em que cada proxy **acrescenta** no fim o
    endereço que viu chegar. Então o último item é o que o proxy do Render viu:
    quem manda cabeçalho forjado só consegue se esconder no começo da lista,
    que é descartado. Sem o cabeçalho (dev local, teste) cai no peer do socket.
    """
    cabecalho = request.headers.get("x-forwarded-for") or ""
    for parte in reversed(cabecalho.split(",")):
        if ip := parte.strip():
            return ip[:60]
    return (request.client.host if request.client else "") or "desconhecido"


def _conta(chave: str, teto_hora: int, teto_dia: int, agora: float) -> str:
    """Conta um turno. Devolve "" liberou, "hora" ou "dia" se bateu no teto."""
    if teto_hora <= 0 and teto_dia <= 0:
        return ""  # desligado por configuração: quem manda no número é o dono

    if len(_janelas) > _CHAVES_MAX:
        _varre(agora)

    janela = _janelas.get(chave)
    if janela is None:
        janela = [0.0, agora, 0.0, agora]
        _janelas[chave] = janela

    # Janela vencida recomeça: o par (contagem, início) anda junto, senão a
    # contagem antiga continuaria sendo comparada com a janela nova e o IP
    # ficaria barrado para sempre depois de um pico.
    if agora - janela[1] >= _HORA:
        janela[0], janela[1] = 0.0, agora
    if agora - janela[3] >= _DIA:
        janela[2], janela[3] = 0.0, agora

    if teto_hora > 0 and janela[0] >= teto_hora:
        return "hora"
    if teto_dia > 0 and janela[2] >= teto_dia:
        return "dia"

    janela[0] += 1
    janela[2] += 1
    return ""


def _varre(agora: float) -> None:
    """Esquece quem não bate em ninguém há um dia e corta o excesso.

    O primeiro passo sozinho não basta: numa rajada de origens diferentes (e
    cada uma com a sua janela nova, ou seja, nenhuma vencida) o mapa só
    cresceria. O segundo passo devolve o mapa para metade do teto, jogando fora
    as janelas mais antigas — perder a contagem de um IP é faro melhor do que
    trocar todo mundo por outro IP e deixar passar o resto.
    """
    for chave in [k for k, j in _janelas.items() if agora - j[3] >= _DIA]:
        _janelas.pop(chave, None)
    if len(_janelas) <= _CHAVES_MAX:
        return
    excesso = len(_janelas) - _CHAVES_MAX // 2
    for chave, _j in sorted(_janelas.items(), key=lambda it: it[1][3])[:excesso]:
        _janelas.pop(chave, None)


def checa_turno_anonimo(request: Request, teto_hora: int, teto_dia: int) -> str:
    """Conta o turno de quem não está logado e diz qual teto bateu (ou "").

    É a única função que a rota chama. Não levanta: qualquer erro aqui é
    registrado e vira "liberado", porque a venda da home vale mais que a
    contagem.
    """
    try:
        return _conta(chave_do_ip(request), teto_hora, teto_dia,
                      time.monotonic())
    except Exception:
        log.exception("Falha ao contar turno anônimo; liberando o chat")
        return ""


def limpar() -> None:
    """Esquece todas as contagens. Só para teste."""
    _janelas.clear()

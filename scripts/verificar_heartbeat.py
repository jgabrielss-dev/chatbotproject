"""Prova, de fora, que o heartbeat esta rodando no Render.

A API de logs do Render responde 404, entao nao da para ler "Heartbeat LIGADO"
pela API. Este script prova pelo efeito, que e o que importa de verdade:

O Render conta a hibernacao por ~15 min sem trafego. Entao, se o servico
ficar de pe SEM ninguem mexer por mais de 15 min, so pode ser porque algo esta
gerando trafego. Este script espera 20 min sem tocar em nada e depois mede.

Se responder rapido: o servico nao dormiu, e o unico ping do sistema e o
heartbeat (esta pagina nao acessa nada antes do fim da espera).

O tempo e configuravel porque 20 min excede o limite de 2 min do shell.

    python scripts/verificar_heartbeat.py [minutos]
"""
from __future__ import annotations

import sys
import time
from datetime import datetime

BASE = "https://chatbotproject-1-l9zr.onrender.com"
JANELA_HIBERNACAO_SEG = 900  # o Render dorme depois de ~15 min sem trafego


def main() -> None:
    import httpx

    minutos = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    espera = minutos * 60
    if espera <= JANELA_HIBERNACAO_SEG:
        raise SystemExit(
            f"precisa esperar mais que {int(JANELA_HIBERNACAO_SEG / 60)} min, "
            f"senao a hibernacao nao teria tempo de acontecer e o teste nao "
            f"prova nada (voce pediu {minutos})"
        )

    print(f"nenhum acesso ate {datetime.now():%H:%M:%S}. "
          f"esperando {minutos:g} min ({espera / 60:.0f} min > "
          f"{int(JANELA_HIBERNACAO_SEG / 60)} min de janela de hibernacao)")
    print("este processo NAO toca no servico durante a espera")

    t0 = time.time()
    while time.time() - t0 < espera:
        falta = espera - (time.time() - t0)
        print(f"  {datetime.now():%H:%M:%S}  faltam {falta / 60:.0f} min", end="\r")
        time.sleep(30)

    print()
    print(f"espera cumprida em {datetime.now():%H:%M:%S}. medindo a resposta...")
    ini = time.time()
    r = httpx.get(f"{BASE}/health", timeout=180)
    dt = time.time() - ini
    print(f"  GET /health -> {r.status_code} em {dt:.2f}s")

    print()
    if dt > 30:
        print("DORMIU: a primeira resposta demorou mais de 30s, que e a "
              "assinatura de um cold start. O heartbeat nao esta funcionando.")
    else:
        print(f"ACORDADO: {dt:.2f}s sem cold start depois de {minutos:g} min "
              f"sem nenhum acesso. O servico nao hibernou.")
    print()
    print("Observacao: isto prova que o servico ficou de pe. Nao prova sozinho "
          "que foi o heartbeat — o Render tambem acorda sozinho em deploy e "
          "restart. Para ver o log 'Heartbeat LIGADO', use o dashboard.")


if __name__ == "__main__":
    main()

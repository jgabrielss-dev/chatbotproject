"""Descobre e desfaz a corrupcao de encoding dos arquivos do site.

Sintoma: acentos viram "Ã©", "├óÔé¼┬ª", "â€¦". Cada arquivo foi quebrado por um
codepage diferente, e as tentativas nao foram todas de uma vez so:

    utf8 <- cp850 x1 -> "├óÔé¼┬ª" vira "â€¦"
    utf8 <- cp1252 x1 -> "â€¦" vira "…"

ou seja, admin.html precisa de DOIS passes, com codepages diferentes. Por isso o
script testa sequencias de codepages, e nao um codepage repetido.

E o login.html e pior: tem mojibake E acentos ja corretos ao lado ("para cá"
esta certo enquanto o resto esta quebrado). Passe unico quebra o que estava
certo. Por isso, quando a cadeia nao zera o resto, o script repara trecho por
trecho, so mexendo onde o trecho e de fato mojibake.

Como escolher a cadeia certa sem chutar: o texto e portugues, e "portugues" e
mensuravel. A cadeia que recuperar mais palavra conhecida e nao deixar resto
vence. Se nenhuma pontuar bem, o script falha em vez de gravar lixo.

    python scripts/corrigir_encoding.py            # so diagnostico
    python scripts/corrigir_encoding.py --aplicar   # corrige de verdade
"""
from __future__ import annotations

import itertools
import pathlib
import re
import sys

ARQUIVOS = ("index.html", "login.html", "admin.html", "painel.html", "style.css",
            "auth.js")

CODEPAGES = ("cp1252", "latin-1", "cp850", "cp437", "cp860", "mac_roman")

# Portuguese comum. Se a cadeia esta certa, essas palavras voltam inteiras.
PORTUGUES = re.compile(
    r"\b(?:que|não|nao|uma|para|com|você|voce|página|pagina|conta|senha|entrar|"
    r"criar|sessão|sessao|painel|memória|memoria|cliente|mudança|mudanca|obrigatório|"
    r"obrigatorio|conexão|conexao|início|inicio|será|sera|está|esta|são|sao|já|ja|"
    r"até|ate|funciona|servidor|carregando|agora|más|mas|é|e|ó|o|á|a|í|i)\b",
    re.IGNORECASE,
)

# ASSINATURA de mojibake, nao "caractere acentuado". Isso importa: 'é' e 'ã'
# legítimos caem na mesma faixa U+00C0-U+00FF que o 'Ã' do "VocÃª", e contar os
# dois como erro fazia o script看不到 o acerto e recusar um conserto bom.
#
# O que caracteriza o rastro e o PAR: um caractere delead (Ã, Â, â, ð) seguido
# de um byte de continuacao ou de uma substituicao do cp1252. Um 'é' de
# verdade e seguido de letra comum, entao nao casa.
SUBST_CP1252 = (
    "[\u2013\u2014\u2018\u2019\u201a\u201c\u201d\u201e\u2020\u2021\u2022\u2026"
    "\u2030\u2039\u203a\u2122\u0152\u0153\u0160\u0161\u0178\u017d\u017e"
    "\u0192\u02c6\u02dc\ufffd]"
)
# Lead = quem SEMPRE abre um par de mojibake. Nao colocar aqui 'ã', 'õ' ou
# 'þ': sao letras normais de portugues (e de soaking) e nao lideres de nada.
LEAD = "[\u00c2\u00c3\u00e2\u00f0]"
CONTINUACAO = "[\u0080-\u00bf]"

# Uma terceira classe, e a do cp850: "…" lido como cp850 vira "├óÔé¼┬ª", com
# caracteres de desenho de caixa. Nenhum deles existe em portugues, entao nao
# ha risco de confundir com acento legitimo.
CAIXA = "[\u2500-\u257f]"
# O trio "â€¦": sao tres caracteres separados, o do meio ('€', U+20AC) e uma
# substituicao do cp1252 e o ultimo ('¦', U+00A6) e um simbolo de cp850. Sem
# o '€' na lista, o Trecho pegava 'â' e '¦' como pedacos distintos e nao
# tinha como juntar. O PRECO de incluir o '€' e que um cifrao legitimate num
# texto ("100 €") passe a contar como rastro; e por isso que o conserto so
# substitui quando o resultado fica com MENOS rastro, e nao por padrao.
EURO = "\u20ac"
# E os simbolos que so o cp850 produz, e que nao aparecem em texto portugues.
# So os simbolos que o cp850 produz e que NAO aparecem em portugues escrito.
# 'ã' foi deliberadamente excluido: e a letra mais comum da lingua, e includi-la
# aqui fazia o script acusar o arquivo inteiro de quebrado.
SIMBOLOS_CP850 = "[\u00a2\u00a3\u00a4\u00a5\u00a6\u00a8\u00a9\u00ac\u00b0\u00b1\u00ba\u00bb\u00bc\u00bd\u00be\u00d4\u00d5\u00d8\u00de]"

Rastro = re.compile(LEAD + "[" + CONTINUACAO[1:-1] + "]" + r"|"
                     + LEAD + SUBST_CP1252 + r"|"
                     + SUBST_CP1252 + CONTINUACAO + r"|"
                     + CAIXA + r"|"
                     + SIMBOLOS_CP850 + r"|"
                     + EURO + r"|"
                     + r"\ufffd")

# Trecho candidato a mojibake: um lead mais os bytes que ele carrega. Um "cá"
# com acento certo nao casa, entao fica intacto; um "VocÃª" casa inteiro.
Trecho = re.compile(
    # Lead seguido de continuacao, e vice-versa: o par classico "Ã©".
    LEAD + "[" + CONTINUACAO[1:-1] + SUBST_CP1252[1:-1] + CAIXA[1:-1]
    + SIMBOLOS_CP850[1:-1] + EURO + "]+"
    r"|[" + SUBST_CP1252[1:-1] + CONTINUACAO[1:-1] + CAIXA[1:-1]
    + SIMBOLOS_CP850[1:-1] + "]+"
    r"|[" + CAIXA[1:-1] + SIMBOLOS_CP850[1:-1] + "]+"
    r"|\ufffd+"
)

CONTROLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def nota(texto: str) -> tuple[float, int, int]:
    """Maior pontuacao = mais portugues recuperado e menos resto de mojibake."""
    bons = len(PORTUGUES.findall(texto))
    ruins = len(Rastro.findall(texto)) + len(CONTROLE.findall(texto)) * 5
    return (bons - ruins * 3, bons, ruins)


def desfazer(texto: str, cps: tuple[str, ...]) -> str | None:
    """Aplica encode(cp)/decode(utf-8) na ordem dada; None se algum passo falha."""
    atual = texto
    for cp in cps:
        try:
            atual = atual.encode(cp).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return None
    return atual


def sequencias() -> list[tuple[str, tuple[str, ...]]]:
    """Sequencias de codepages em ordem. Comprimento 3 cobre o caso real."""
    out = []
    for n in (1, 2, 3):
        for combo in itertools.product(CODEPAGES, repeat=n):
            out.append(("utf8 <- " + " -> ".join(combo), combo))
    return out


def melhor_cadeia(texto: str) -> tuple[str, str] | None:
    """A cadeia que mais melhora a nota, ou None se nenhuma melhorar."""
    base = nota(texto)
    melhor: tuple[float, int, str, str] | None = None
    for nome, cps in sequencias():
        saida = desfazer(texto, cps)
        if saida is None:
            continue
        p, bons, ruins = nota(saida)
        if melhor is None or (p, -ruins) > (melhor[0], -melhor[1]):
            melhor = (p, ruins, nome, saida)
    if melhor is None or melhor[0] <= base[0]:
        return None
    return melhor[2], melhor[3]


def reparar_trechos(texto: str) -> str:
    """Ultimo recurso: conserta so os trechos que sao mojibake.

    Guarda o original e so troca se o conserto tiver menos rastro, para nunca
    piorar um trecho que ja estava certo.
    """
    def troca(m: re.Match[str]) -> str:
        bruto = m.group(0)
        for cps in (("cp1252",), ("latin-1",), ("cp850",), ("cp1252", "cp1252")):
            saida = desfazer(bruto, cps)
            if saida is not None and len(Rastro.findall(saida)) < len(Rastro.findall(bruto)):
                return saida
        return bruto

    return Trecho.sub(troca, texto)


def amostra(texto: str) -> str:
    linhas = [l.strip() for l in texto.splitlines() if Rastro.search(l)]
    for l in linhas:
        if "…" in l or "<h" in l or "<title" in l:
            return l[:88]
    return linhas[0][:88] if linhas else texto.splitlines()[0][:88]


def analisar(caminho: pathlib.Path) -> None:
    original = caminho.read_text(encoding="utf-8")
    base_nota, base_bons, base_ruins = nota(original)
    print(f"\n{'=' * 74}\n{caminho.name}")
    print(f"  estado atual: {base_bons} palavra(s) em portugues, "
          f"{base_ruins} resto(s) de mojibake")
    if base_ruins == 0:
        print("  sem rastro de mojibake, nao mexer")
        return

    cadeia = melhor_cadeia(original)
    if cadeia:
        nome, saida = cadeia
        print(f"  cadeia que melhorou: {nome}  -> {nota(saida)[1]} palavra(s), "
              f"{nota(saida)[2]} resto(s)")
        final = saida
    else:
        print("  nenhuma cadeia de codepage resolveu; reparando trecho por trecho")
        final = original

    final = reparar_trechos(final)
    f_nota, f_bons, f_ruins = nota(final)
    print(f"  depois: {f_bons} palavra(s) em portugues, {f_ruins} resto(s)")
    print("  antes:  " + amostra(original))
    print("  depois: " + amostra(final))
    if f_ruins:
        print(f"  AINDA RESTA {f_ruins} rastro(s); revisar a mao antes de gravar")
        return
    if f_nota <= base_nota:
        print("  resultado nao e melhor que o original; descartado")
        return

    if "--aplicar" in sys.argv:
        caminho.write_text(final, encoding="utf-8", newline="")
        print(f"  GRAVADO {caminho}")


def main() -> None:
    print("Diagnosticando encoding. Cada arquivo foi corrompido por um codepage\n"
          "diferente, e uns por dois passes. Sem medir qual, um 'e' vira acento\n"
          "falso em vez de 'é'.")
    for nome in ARQUIVOS:
        caminho = pathlib.Path(nome)
        if caminho.is_file():
            analisar(caminho)


if __name__ == "__main__":
    main()

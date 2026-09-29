"""Diagnostico de encoding: onde estao os caracteres estranhos do site.

Escrevi isso como script em vez de -c inline porque o here-string do PowerShell
comeu a comer a indentacao do Python, e um arquivo de leitura dao para conferir
a saida sem o console do PowerShell remontar os acentos.
"""
import pathlib
import re
import unicodedata

ARQUIVOS = ("index.html", "login.html", "admin.html", "painel.html",
            "auth.js", "style.css", "app/main.py")

# Mojibake classico: um acento que foi decodificado como latin-1 e re-salvo, o
# que transforma um 'a' em dois caracteres ('a' + um byte 0x80-0xBF).
MOJIBAKE = re.compile(r"[\u00c0-\u00ff][\u0080-\u00bf]|\u00c3[\u0080-\u00bf]")
# Latin estendido: caractere que existe mas nao pertence a portugues/frances.
LATIN_EXT = re.compile(r"[\u0100-\u024f]")
SUBSTITUIDO = "\ufffd"


def diagnostico() -> None:
    achou_problema = False
    for nome in ARQUIVOS:
        caminho = pathlib.Path(nome)
        if not caminho.is_file():
            continue
        texto = caminho.read_text(encoding="utf-8")
        brutos = caminho.read_bytes()
        try:
            brutos.decode("utf-8")
            valido = "UTF-8 ok"
        except UnicodeDecodeError:
            valido = "BYTE INVALIDO"

        m = MOJIBAKE.findall(texto)
        e = LATIN_EXT.findall(texto)
        sub = texto.count(SUBSTITUIDO)

        print(f"\n{nome}  [{valido}]")
        if not (m or e or sub):
            print("   limpo")
            continue
        achou_problema = True
        if m:
            print(f"   mojibake (2 chars por acento): {len(m)} -> "
                  f"{sorted(set(m))[:10]}")
        if e:
            print(f"   latin estendido: {len(e)} -> "
                  f"{sorted(set(e))[:16]}")
        if sub:
            print(f"   caractere de substituicao U+FFFD: {sub}")

        for n, linha in enumerate(texto.splitlines(), 1):
            if MOJIBAKE.search(linha) or LATIN_EXT.search(linha) or SUBSTITUIDO in linha:
                print(f"   L{n}: {linha.strip()[:96]}")
    print("\n" + ("TEM PROBLEMA" if achou_problema else "NADA SUSPEITO"))


def hipoteses() -> None:
    """Cada caractere estranho e a pista de qual passo quebrou o texto."""
    texto = pathlib.Path("index.html").read_text(encoding="utf-8")
    print("\n\nde onde veio cada caractere")
    vistos = set()
    for linha in texto.splitlines():
        for ch in linha:
            if LATIN_EXT.match(ch) or ch == SUBSTITUIDO:
                if ch in vistos:
                    continue
                vistos.add(ch)
                nome = unicodedata.name(ch, "?")
                # Se um 'a' virou 'a' + algo, o passo 1 foi decodificar como
                # latin-1. Se o resultado e U+FFFD, foi decodificar como UTF-8
                # um arquivo que ja era latin-1 (o passo 2, e nao tem volta sem
                # a fonte).
                print(f"   {ch!r} U+{ord(ch):04X} {nome:<28} "
                      f"{'decodificou como latin-1' if ord(ch) < 0x200 else 'perdeu o byte'}")


if __name__ == "__main__":
    diagnostico()
    hipoteses()

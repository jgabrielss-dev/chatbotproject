"""Simula o GitHub Pages: le as paginas do disco como o Pages serviria.

Por que um script e nao so abrir o navegador: o Pages de um projeto e publicado
em "https://<user>.github.io/<repo>/", o que muda DUAS coisas que o Render
esconde:

1. O caminho "/auth.js" vira "https://<user>.github.io/auth.js" (fora do repo) e
   da 404. Só caminho relativo funciona.
2. Nao existe servidor para rotear, entao "/admin" vira um pedido do arquivo
   "admin" e da 404. Precisa ser "/admin.html".

O Render responde 200 em tudo, entao ele nunca acusaria esse erro. Aqui o
caminho e resolvido contra a URL-base do Pages e o arquivo procurado tem de
existir na raiz.

    python scripts/simular_pages.py
"""
from __future__ import annotations

import pathlib
import re
import sys
from urllib.parse import urljoin, urlparse

RAIZ = pathlib.Path(__file__).resolve().parent.parent
BASE_PAGES = "https://jgabrielss-dev.github.io/chatbotproject/"
FALHAS: list[str] = []


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def ruim(msg: str) -> None:
    FALHAS.append(msg)
    print(f"  FALHA {msg}")


def _tem_arquivo(caminho_url: str) -> bool:
    """Resolve a URL do Pages contra a base e procura o arquivo na raiz."""
    caminho = urlparse(urljoin(BASE_PAGES, caminho_url)).path
    # /chatbotproject/ -> caminho relativo dentro do repo
    rel = caminho.split("/chatbotproject/", 1)[-1] if "/chatbotproject/" in caminho else caminho
    rel = rel.lstrip("/")
    destino = RAIZ / rel
    if rel in ("", "index.html"):
        destino = RAIZ / "index.html"
    return destino.is_file(), destino


def main() -> None:
    paginas = ["index.html", "login.html", "painel.html", "admin.html"]

    print(f"1) as paginas do Pages existem na raiz (base {BASE_PAGES})")
    for p in paginas:
        existe = (RAIZ / p).is_file()
        ok(f"{p} na raiz") if existe else ruim(f"{p} NAO esta na raiz")
    if not (RAIZ / "index.html").is_file():
        ruim('sem index.html a URL ".../chatbotproject/" nao tem indice de '
            "diretorio e o Pages mostra 404")

    print("2) assets resolvem pela URL do Pages (caminho relativo)")
    for p in paginas:
        html = (RAIZ / p).read_text(encoding="utf-8")
        for attr, alvo in re.findall(r'(src|href)="([^"]+)"', html):
            if alvo.startswith(("http://", "https://", "#", "data:")):
                continue
            if "${" in alvo:
                # src="${img}" e um template string: o valor so existe em
                # tempo de execucao, entao nao da para resolver por arquivo.
                ok(f"{p}: {attr}={alvo} (template em runtime, fora do escopo)")
                continue
            existe, destino = _tem_arquivo(alvo)
            if existe:
                ok(f"{p}: {attr}={alvo} -> {destino.name}")
            else:
                ruim(f"{p}: {attr}={alvo} nao resolve no Pages "
                    f"(procurou {destino})")

    print("3) rota limpa /admin nao funciona no Pages (nao ha servidor)")
    for cru in ("/admin", "/painel"):
        existe, _ = _tem_arquivo(cru)
        if existe:
            ruim(f"{cru} devolve um arquivo no Pages; use {cru}.html")
        else:
            ok(f"{cru} daria 404 no Pages, entao o JS nao pode usar esse caminho cru")

    print("4) o JS nao tem redirect cru para caminho de pagina")
    auth_js = (RAIZ / "auth.js").read_text(encoding="utf-8")
    if "EM_PAGINA_ESTATICA" in auth_js:
        ok("auth.js distingue pagina estatica de Render")
    else:
        ruim("auth.js nao sabe se esta no Pages; cairia em /admin e daria 404")
    for p in paginas:
        html = (RAIZ / p).read_text(encoding="utf-8")
        achou = re.findall(r'location\.replace\("/(admin|painel|\?)', html)
        if achou:
            ruim(f"{p}.html tem redirect cru para {achou}")
        else:
            ok(f"{p}.html sem redirect cru (usa Auth.pagina)")

    print()
    if FALHAS:
        print(f"{len(FALHAS)} FALHA(S): " + "; ".join(FALHAS))
        sys.exit(1)
    print("O SITE FUNCIONA NO GITHUB PAGES")


if __name__ == "__main__":
    main()

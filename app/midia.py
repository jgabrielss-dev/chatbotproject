"""Anexo de foto, áudio, vídeo ou arquivo — o item 15 do pedido.

O pedido é: "todos os agentes devem ler áudio, vídeo, arquivos, fotos,
declarados no prompt do sistema; também nos chats internos do site". Duas
metades distintas, e vale dizer por que elas vivem em arquivos diferentes:

1. **A capacidade** (ler o anexo) é do Gemini. Uma mensagem vira várias `Part`:
   uma de texto e uma por arquivo. O limite é o da API: 20 MB por requisição no
   total, então o teto aqui é por arquivo *e* somado.

2. **A declaração** (o agente saber que recebe isso) é do prompt de sistema, e
   está em `app/ai/gemini.py::_contexto_prompt`. Ela é anexada a *todo* agente,
   sem exceção — inclusive os internos dos itens 7, 12 e 13, que não passam pelo
   formulário do painel. Se a declaração vivesse no `system_prompt` gravado pelo
   cliente, os agentes internos ficariam mudos para anexos, que é exatamente o
   que o item 15 pede para não acontecer.

O que NÃO é feito aqui, e é uma decisão: o histórico guarda **o texto**
descritivo do anexo, não os bytes. Reenviar 15 MB de áudio a cada turno, só para
a IA voltar a enxergar a mesma foto da mensagem anterior, é custo por message
sem nenhum ganho na conversa. A descrição ("[foto] lajota.jpg — image/jpeg,
120 KB") fica na `mensagens.texto`, então a IA sabe que aquilo existiu, e a
pessoa que abre a conversa no painel vê o que foi enviado. Quem precisar reler o
conteúdo precisa reenviar o arquivo — e a descrição diz isso ao agente, para ele
não fingir que viu de novo.

Este módulo não importa `google.genai` de propósito: ele é usado também pelo
`main.py` e pelos adaptadores de canal, que não têm nada a ver com a IA.
"""
from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Os quatro tipos que o item 15 nomeia. A lista é fechada de propósito: cada
# tipo novo é um caminho novo de download por canal, e um tipo solto (svg, zip,
# .exe) não é lido por nenhum modelo — mandá-lo só produz erro da API.
# --------------------------------------------------------------------------
TIPOS: tuple[str, ...] = ("foto", "audio", "video", "arquivo")

#: MIME aceito por tipo. `application/octet-stream` NÃO entra: assim que um
#: arquivo chega sem tipo nenhum, ele é `arquivo` — o Gemini tenta ler como
#: texto, e é melhor que "não sei o que é isso".
MIME_POR_TIPO: dict[str, tuple[str, ...]] = {
    "foto": ("image/jpeg", "image/png", "image/webp", "image/heic", "image/gif"),
    "audio": ("audio/mpeg", "audio/mp3", "audio/ogg", "audio/wav", "audio/mp4",
              "audio/aac", "audio/flac", "audio/x-wav", "audio/amr"),
    "video": ("video/mp4", "video/webm", "video/quicktime", "video/3gpp",
              "video/x-matroska"),
    "arquivo": ("application/pdf", "text/plain", "text/csv", "text/markdown",
                "text/html", "application/json", "application/xml", "text/xml",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}

#: Prefixos que decidem o tipo sem tabela exata. O sufixo de tipo exato acima é
#: o caminho comum; estes cobrem o que os canais mandam de fora do registro
#: (Telegram manda `application/octet-stream` para .pdf, por exemplo).
_PREFIXO_POR_TIPO: tuple[tuple[str, str], ...] = (
    ("image/", "foto"),
    ("audio/", "audio"),
    ("video/", "video"),
    ("application/pdf", "arquivo"),
    ("text/", "arquivo"),
)

_EXTENSAO_POR_TIPO: dict[str, tuple[str, ...]] = {
    "foto": (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif"),
    "audio": (".mp3", ".ogg", ".oga", ".wav", ".m4a", ".aac", ".flac", ".opus", ".amr"),
    "video": (".mp4", ".mov", ".webm", ".mkv", ".3gp", ".avi"),
    "arquivo": (".pdf", ".txt", ".csv", ".md", ".json", ".xml", ".html", ".htm",
                ".doc", ".docx", ".xls", ".xlsx", ".rtf", ".log"),
}

# --------------------------------------------------------------------------
# Tetos. A API do Gemini recusa acima de ~20 MB na requisição inteira (inline
# data), então 15 MB por arquivo e 18 MB somados deixam folga para o texto e
# para o histórico que vai junto. O erro de um anexo grande demais é educado
# (a descrição entra com "(não foi lido: grande demais)") em vez de 400: quem
# enviou o vídeo de 3 minutos não pode ficar sem resposta nenhuma.
# --------------------------------------------------------------------------
LIMITE_POR_ARQUIVO = 15 * 1024 * 1024
LIMITE_TOTAL = 18 * 1024 * 1024
#: Quantos anexos por mensagem. Cinco é o que cabe numa conversa de verdade sem
#: estourar o contexto; acima disso os últimos viram só descrição.
MAX_ANEXOS = 5

_INSEGURO = re.compile(r"[^A-Za-z0-9._-]+")


def _limpar_nome(nome: str, tipo: str) -> str:
    """Nome seguro e legível, para descrever o anexo na conversa.

    O nome vem do cliente final (Telegram, WhatsApp, o webhook de terceiro) e vai
    parar dentro do histórico e da tela do painel. Preserva acentos? Não:
    ASCII + `.`/`-`/`_` é o que sobrevive a base64, a nome de arquivo em URL e a
    planilha de quem faz a auditoria.
    """
    limpo = _INSEGURO.sub("_", (nome or "").strip())[:80].strip("._")
    return limpo or f"anexo.{tipo}"


def classificar(mime: str | None, nome: str | None = None) -> str | None:
    """Qual dos quatro tipos é este arquivo, ou None se não é nenhum deles.

    A ordem é: MIME exato, prefixo de MIME, extensão, e só então None. Extensão
    vem antes de "não sei" porque `application/octet-stream` com `.pdf` é um PDF
    para qualquer pessoa e para o modelo; devolver None ali perderia um arquivo
    perfectly legível.
    """
    mime = (mime or "").split(";")[0].strip().lower()
    nome = (nome or "").strip().lower()
    for tipo, mimes in MIME_POR_TIPO.items():
        if mime in mimes:
            return tipo
    for prefixo, tipo in _PREFIXO_POR_TIPO:
        if mime.startswith(prefixo):
            return tipo
    for tipo, extensoes in _EXTENSAO_POR_TIPO.items():
        if any(nome.endswith(e) for e in extensoes):
            return tipo
    return None


def tamanho_legivel(n: int) -> str:
    """120 KB ou 1.4 MB. Legivel para quem precisa entender POR QUE o
    anexo nao entrou, que e a unica razao de esse texto existir.
    """
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


@dataclass(frozen=True)
class Midia:
    """Um anexo, normalizado. `fonte` diz de onde os bytes vêm.

    `fonte` é um dict de uma chave só, e o porquê é não ter um quarto jeito de
    esconder credencial:
      ``{"url": ...}``   o anexo está numa URL (Evolution, webhook genérico)
      ``{"base64": ...}`` o corpo já veio embutido (Evolution, o front do site)
      ``{"ref": ...}``   o canal precisa de um segundo passo com credencial
                         (Telegram devolve `file_id`; Meta devolve `id` e a URL
                         só sai da Graph API). Quem resolve é o adaptador do
                         canal, em `app/channels/`.
    """
    tipo: str
    mime: str
    nome: str
    fonte: dict[str, Any] = field(default_factory=dict)

    def descrever(self) -> str:
        """A linha que entra em `mensagens.texto` e no histórico."""
        return f"[{self.tipo}] {self.nome} ({self.mime})"

    def para_dict(self) -> dict[str, Any]:
        return {"tipo": self.tipo, "mime": self.mime,
                "nome": self.nome, "fonte": self.fonte}

    @staticmethod
    def de_dict(d: Any) -> "Midia | None":
        """Reconstrói do JSONB. Devolve None em vez de estourar: a fila não pode
        perder a mensagem inteira porque o anexo ficou corrompido no banco."""
        if not isinstance(d, dict):
            return None
        tipo = str(d.get("tipo") or "")
        if tipo not in TIPOS:
            return None
        fonte = d.get("fonte")
        if not isinstance(fonte, dict) or not fonte:
            # `fonte` vazia não é um anexo: é um anexo que o Gemini não vai
            # conseguir ler. O que gravamos no banco sempre vem de
            # `de_url`/`de_ref`/`de_base64`, que exigem a chave — então
            # devolver None aqui só afeta o item solto de webhook, e deixa o
            # `_de_item_solto` cuidar dele como deve ser.
            #
            # Antes disto, o `tipo` sozinho bastava: um `{"tipo": "audio",
            # "mime_type": ..., "ref": ...}` da Meta passava por aqui e virava
            # `Midia(tipo, fonte={})`. A mensagem era entregue ao modelo como
            # "[audio do cliente]" e o arquivo sumia — o bot respondia a um
            # áudio que nunca ouviu.
            return None
        return Midia(
            tipo=tipo,
            mime=str(d.get("mime") or "application/octet-stream"),
            nome=_limpar_nome(str(d.get("nome") or ""), tipo),
            fonte=fonte,
        )


def de_url(tipo: str, mime: str, nome: str, url: str) -> Midia | None:
    if tipo not in TIPOS or not url:
        return None
    return Midia(tipo, (mime or "").split(";")[0].strip() or "application/octet-stream",
                 _limpar_nome(nome, tipo), {"url": url})


def de_base64(tipo: str, mime: str, nome: str, dados: str) -> Midia | None:
    if tipo not in TIPOS or not dados:
        return None
    return Midia(tipo, (mime or "").split(";")[0].strip() or "application/octet-stream",
                 _limpar_nome(nome, tipo), {"base64": dados})


def de_ref(tipo: str, mime: str, nome: str, ref: str) -> Midia | None:
    """Anexo que o canal só referencia (`file_id` do Telegram, `id` da Meta)."""
    if tipo not in TIPOS or not ref:
        return None
    return Midia(tipo, (mime or "").split(";")[0].strip() or "application/octet-stream",
                 _limpar_nome(nome, tipo), {"ref": ref})


def decodificar(dados: str) -> bytes | None:
    """base64 → bytes, tolerando o que os canais mandam.

    `data:audio/ogg;base64,...` vem assim do WhatsApp e do front; e um base64
    com caractere estranho (o Evolution corta com `\n`) não pode derrubar a
    mensagem. Devolve None quando não dá para recuperar, e quem chama cai no
    caminho "(não foi lido)".
    """
    bruto = (dados or "").strip()
    if bruto.startswith("data:"):
        bruto = bruto.split(",", 1)[-1]
    bruto = bruto.replace("\n", "").replace("\r", "").replace(" ", "")
    try:
        return base64.b64decode(bruto, validate=False)
    except (binascii.Error, ValueError):
        return None


def normalizar(lista: Any) -> list[Midia]:
    """Aceita o que vier (list, JSON string, dict solto) e devolve anexos válidos.

    A entrada é sempre duvidosa — vem do webhook de terceiro, do painel ou de um
    campo que o cliente preencheu. Um item ruim é descartado em silêncio, sem
    derrubar os outros: perder o anexo é ruim, perder a mensagem é pior.
    """
    if isinstance(lista, str):
        try:
            lista = __import__("json").loads(lista)
        except ValueError:
            return []
    if isinstance(lista, dict):
        lista = [lista]
    if not isinstance(lista, list):
        return []
    saida: list[Midia] = []
    for item in lista:
        m = Midia.de_dict(item)
        if m is None and isinstance(item, dict):
            m = _de_item_solto(item)
        if m is not None:
            saida.append(m)
    return saida[:MAX_ANEXOS]


def _de_item_solto(item: dict) -> Midia | None:
    """Anexo vindo de um webhook de terceiro: a forma varia, o significado não.

    Aceita `mime_type`, `mimeType` e `content_type` pelo mesmo motivo: três
    integrações diferentes, nenhuma nossa para corrigir.
    """
    mime = (item.get("mime") or item.get("mime_type") or item.get("mimeType")
            or item.get("content_type") or "")
    nome = str(item.get("nome") or item.get("filename") or item.get("file_name")
              or item.get("name") or "")
    tipo = str(item.get("tipo") or item.get("type") or "").strip().lower()
    if tipo not in TIPOS:
        tipo = classificar(str(mime), nome) or ""
    if tipo not in TIPOS:
        return None
    # Um sticker é uma imagem; a Meta manda `sticker` e o nome não tem pista de
    # que formato é. Traduzir aqui evita o `return None` logo abaixo.
    if tipo == "arquivo" and str(mime).startswith("image/"):
        tipo = "foto"
    base = item.get("base64") or item.get("data") or item.get("conteudo") or ""
    url = item.get("url") or item.get("media_url") or item.get("link") or ""
    if base:
        return de_base64(tipo, str(mime), nome, str(base))
    if url:
        return de_url(tipo, str(mime), nome, str(url))
    # `ref` é a chave que a Edge Function `inbox` grava, para os canais oficiais
    # da Meta. Sem ela aqui, todo anexo do WhatsApp/Instagram oficial saía
    # descartado em silêncio: `_de_item_solto` devolvia None e o bot respondia
    # a "[imagem do cliente]" sem nunca ver a imagem.
    ref = item.get("ref") or item.get("file_id") or item.get("fileId") or item.get("id") or ""
    if ref:
        return de_ref(tipo, str(mime), nome or str(ref), str(ref))
    return None


def descrever(lista: Any) -> str:
    """Texto que entra na `mensagens.texto` quando a pessoa mandou só anexo.

    Sem legenda, `texto` seria string vazia — e `caixa_entrada.texto` é NOT NULL.
    Uma linha honesta ("[foto] lajota.jpg") é melhor que a mensagem não existir
    no histórico, e é o que a pessoa vê no painel.
    """
    anexos = normalizar(lista)
    if not anexos:
        return ""
    return " ".join(m.descrever() for m in anexos)


def orcamento(anexos: list[Midia]) -> tuple[list[Midia], list[str]]:
    """Separa o que cabe do que não cabe, e diz o motivo.

    Devolve `(aceitos, motivos)`. O teto é por arquivo e somado — um vídeo de
    30 MB não pode passar só porque "os outros são pequenos". O motivo vira
    texto na resposta, porque silêncio aqui é a pior saída: a pessoa acha que a
    IA leu e não leu.
    """
    aceitos: list[Midia] = []
    motivos: list[str] = []
    total = 0
    for m in anexos:
        fonte = m.fonte
        if "base64" in fonte:
            bruto = decodificar(str(fonte["base64"])) or b""
        else:
            # URL e ref não têm tamanho conhecido aqui: o download acontece no
            # pipeline, e o teto é conferido com os bytes na mão.
            bruto = b""
        if bruto and len(bruto) > LIMITE_POR_ARQUIVO:
            motivos.append(f"{m.nome}: {tamanho_legivel(len(bruto))}, "
                           f"o limite é {tamanho_legivel(LIMITE_POR_ARQUIVO)}")
            continue
        if total + len(bruto) > LIMITE_TOTAL:
            motivos.append(f"{m.nome}: o conjunto passou de "
                           f"{tamanho_legivel(LIMITE_TOTAL)}")
            continue
        total += len(bruto)
        aceitos.append(m)
    return aceitos, motivos

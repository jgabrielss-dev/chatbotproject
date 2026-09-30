"""Quais canais o cliente resolve sozinho, e que campos cada um pede.

Este é o item 2 do pedido: "cliente só pode editar/criar canais de API do
próprio agente". A metade difícil (a posse) já é garantida pelo `dono_id` em
toda query de canal; aqui está a metade que é decisão de produto: **quais
tipos o cliente pode criar**.

São três, e o critério é um só — *a credencial é do cliente*:

  Telegram             o cliente cria o bot no BotFather e tem o token
  Webhook              nenhuma credencial, ele só consome uma URL
  WhatsApp (oficial)   a credencial é da conta Meta do cliente
  Instagram (oficial)  idem

Ficam de fora **WhatsApp e Instagram não-oficiais** (Evolution/Baileys e
instagrapi), por três motivos: criam instância em infraestrutura compartilhada
da plataforma, precisam de processo vivo no Render (keepalive, que consome as
horas do plano grátis) e podem BANIR a conta conectada. Isso é decisão do
operador, não do cliente — e `main.py` (`_exigir_tipo_permitido`) já barra no
servidor, com a variável `CANAIS_NAO_OFICIAIS_PARA_USUARIOS` para o operador
abrir se quiser assumir esse risco.

Os campos são declarados aqui, e não no HTML, por dois motivos: o formulário
do cliente é gerado a partir desta lista (então cliente e servidor não podem
divergir — o cliente nunca pede um campo que o servidor não valida), e a
descrição longa de "como obter o token do BotFather" fica num lugar só, em vez
de duplicada em duas telas que iam apodrecer em ritmos diferentes.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Campo:
    chave: str
    rotulo: str
    tipo: str = "text"          # text | password
    obrigatorio: bool = False
    placeholder: str = ""
    ajuda: str = ""


@dataclass(frozen=True)
class TipoCanal:
    id: str
    nome: str
    resumo: str
    oficial: bool
    credencial_do_cliente: bool
    campos: tuple[Campo, ...] = field(default_factory=tuple)
    aviso: str = ""
    como_usar: tuple[str, ...] = field(default_factory=tuple)
    depois_de_salvar: str = ""


#: Ordem de exibição na tela: o mais simples primeiro, porque é por onde o
#: cliente começa.
TIPOS_CANAIS_CLIENTE: tuple[TipoCanal, ...] = (
    TipoCanal(
        id="telegram",
        nome="Telegram",
        resumo="Conecte um bot do Telegram. É o canal mais rápido de testar.",
        oficial=False,
        credencial_do_cliente=True,
        campos=(Campo(
            chave="token",
            rotulo="Token do bot (BotFather)",
            tipo="password",
            obrigatorio=True,
            placeholder="1234567890:AAEx…",
            ajuda="Fale com @BotFather no Telegram, mande /newbot e copie o token.",
        ),),
        como_usar=(
            "Fale com @BotFather no Telegram e mande <code>/newbot</code>.",
            "Dê um nome e um username terminados em <code>bot</code>.",
            "Copie o token (números e <code>:</code>) e cole no campo acima.",
            "Salve o canal e clique em <strong>registrar webhook</strong>: é esse passo "
            "que faz a mensagem chegar. A URL já carrega o segredo do canal — não edite à mão.",
        ),
        depois_de_salvar="Clique em “registrar webhook” para começar a receber mensagens.",
    ),
    TipoCanal(
        id="webhook",
        nome="Webhook (API)",
        resumo="Qualquer sistema seu que mande POST e receba a resposta na mesma chamada.",
        oficial=False,
        credencial_do_cliente=True,
        campos=(),
        como_usar=(
            "Salve o canal: a URL com o segredo é gerada automaticamente.",
            "Chame a URL com um POST JSON. A resposta vem no mesmo request:",
        ),
        depois_de_salvar="Copie a URL do canal e mande um POST para ela.",
    ),
    TipoCanal(
        id="whatsapp_oficial",
        nome="WhatsApp (oficial — Cloud API)",
        resumo="A integração estável de WhatsApp, feita pela Meta. Sem QR, sem risco de ban.",
        oficial=True,
        credencial_do_cliente=True,
        campos=(
            Campo("phone_number_id", "Phone Number ID", obrigatorio=True,
                  placeholder="123456789012345"),
            Campo("verify_token", "verify_token", obrigatorio=True,
                  placeholder="escolha um texto seu",
                  ajuda="Qualquer texto. É o que a Meta usa para validar a verificação."),
            Campo("access_token", "Access token permanente", tipo="password",
                  obrigatorio=True,
                  ajuda="Gere um permanente em WhatsApp › Configurações no painel da Meta."),
            Campo("app_secret", "App secret", tipo="password", obrigatorio=True,
                  placeholder="App Settings › Basic › App secret",
                  ajuda="Valida a assinatura do webhook. Sem ele o canal rejeita tudo."),
        ),
        como_usar=(
            "<code>developers.facebook.com</code> → seu app → <em>WhatsApp</em> → <em>API Setup</em>.",
            "Em <em>Configurações › Básico</em>, copie o <strong>App secret</strong>.",
            "Escolha um <strong>verify_token</strong> e salve o canal.",
            "Copie a <strong>URL de webhook</strong> do canal para <em>Configurações › Webhooks</em> "
            "e assine o campo <code>messages</code>.",
            "Clique em <strong>Verificar e salvar</strong> no painel da Meta.",
        ),
        depois_de_salvar="Cole a URL do canal em Configurações › Webhooks da Meta e assine “messages”.",
    ),
    TipoCanal(
        id="instagram_oficial",
        nome="Instagram (oficial — Messaging API)",
        resumo="Mensagens diretas do Instagram, pelo webhook da Meta. Sem polling.",
        oficial=True,
        credencial_do_cliente=True,
        campos=(
            Campo("ig_user_id", "Instagram Business Account ID", obrigatorio=True,
                  placeholder="17841400…"),
            Campo("verify_token", "verify_token", obrigatorio=True,
                  placeholder="escolha um texto seu"),
            Campo("access_token", "Access token permanente", tipo="password",
                  obrigatorio=True,
                  ajuda="Gere permanente em Instagram › Configurações, no painel da Meta."),
            Campo("app_secret", "App secret", tipo="password", obrigatorio=True,
                  placeholder="App Settings › Basic › App secret"),
        ),
        como_usar=(
            "A conta precisa ser <strong>Business</strong> ou <strong>Criador</strong>, num "
            "portfólio da Meta ligado a um app.",
            "<em>Instagram › API Setup</em>: copie o ID da conta e gere o access token permanente.",
            "Copie o <strong>App secret</strong> em <em>Configurações › Básico</em>.",
            "Salve o canal e cole a <strong>URL de webhook</strong> em <em>Configurações › Webhooks</em>.",
            "No app do Instagram, abra o botão de mensagem e confirme que a conta pode receber.",
        ),
        depois_de_salvar="Cole a URL do canal em Configurações › Webhooks da Meta e assine “messages”.",
    ),
)

POR_ID: dict[str, TipoCanal] = {t.id: t for t in TIPOS_CANAIS_CLIENTE}

IDS: tuple[str, ...] = tuple(t.id for t in TIPOS_CANAIS_CLIENTE)


def como_json() -> list[dict]:
    """O formulário do cliente é desenhado a partir disto.

    Mandar a lista pelo servidor (em vez de escrever os campos no HTML) é o
    que garante que os dois lados concordem: se um campo for REQUIRED aqui, o
    `_validar_config_canal` do servidor exige o mesmo, e o cliente não consegue
    enviar um formulário que o servidor não entende.
    """
    return [
        {
            "id": t.id,
            "nome": t.nome,
            "resumo": t.resumo,
            "oficial": t.oficial,
            "campos": [
                {
                    "chave": c.chave, "rotulo": c.rotulo, "tipo": c.tipo,
                    "obrigatorio": c.obrigatorio, "placeholder": c.placeholder,
                    "ajuda": c.ajuda,
                }
                for c in t.campos
            ],
            "como_usar": list(t.como_usar),
            "depois_de_salvar": t.depois_de_salvar,
        }
        for t in TIPOS_CANAIS_CLIENTE
    ]

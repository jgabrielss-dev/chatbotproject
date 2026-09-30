"""Os chats internos da plataforma: itens 7 (home), 12 (gerador de prompt) e
13 (suporte).

Fica separado do `main.py` e do `painel.py` porque este é o ÚNICO lugar do
sistema que conversa com quem não é cliente da plataforma, e é por isso que ele
precisa das próprias regras:

* **Quem é a pessoa.** A home fala com um anônimo; os outros dois, com a conta
  logada. O anônimo não tem id, então a sessão dele é um token que o navegador
  gera e guarda — é o `usuario_externo` da `sessoes`, exatamente como o id de um
  Telegram vale para o canal do Telegram.

* **O que o agente pode fazer.** Cada um tem uma lista fechada de ações
  (`app/agentes_internos.py`). O modelo pede uma emitindo `<<<ACAO>>>`; o
  servidor confere contra a lista, e ação fora da lista é recusada. Quem valida
  é o servidor, não o prompt: um prompt é texto, e texto o cliente influencia.

* **Que conta a ação afeta.** A ação de suporte só roda na conta de quem está
  logado, e não existe campo para escolher outra. Se existisse, "mude o plano da
  conta X" seria um pedido válido e o agente de suporte viraria um painel do
  admin com janela de chat.

O histórico, a memória e os anexos (item 15) NÃO são reimplementados aqui: o
chat interno usa o mesmo `app/pipeline.py` dos outros agentes, e é por isso que
foto, áudio, vídeo e documento funcionam aqui sem uma linha a mais. O que muda é
o `system_prompt`, que vem do código e nunca da tabela — é o item 14.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import agentes_internos as ai
from app import cobranca, limite_turnos, midia, pipeline
from app import repos_cobranca as rc
from app import repositories as repo
from app.ai import gemini
from app.config import settings
from app.limites import contexto as contexto_de_cota

log = logging.getLogger("chat_interno")

router = APIRouter(prefix="/api/interno", tags=["interno"])

#: Quantos turnos o modelo pode dar pedindo ação antes de desistir e responder
#: com o que ele já disse. Dois é o normal (pede, recebe o resultado, responde);
#: o teto existe para o caso de o modelo entrar em laço, e cada turno é uma
#: chamada de 10 a 40 s.
MAX_TURNOS_ACAO = 2

#: Tamanho máximo do texto de uma mensagem. 500 KB de prompt seria caro e não
#: cabe em conversa nenhuma; o teto dos anexos é o de `app/midia.py`.
MAX_TEXTO = 4000

#: Link do cadastro. É o MESMO caminho relativo das demais telas (`auth.js`
#: resolve o host): o item 4 exige que o link funcione no Render e no GitHub
#: Pages, e a forma de garantir isso é não hardcodear domínio em lugar nenhum.
LINK_CADASTRO = "/login"


class Mensagem(BaseModel):
    """O que o navegador manda a cada turno."""

    #: Identificador da conversa no navegador. Para a home é um token gerado
    #: pelo `crypto.randomUUID()`; para os outros dois o front manda o id da
    #: conta. O servidor nunca inventa esse valor, e nunca usa o de outra conta.
    sessao: str = Field(min_length=8, max_length=80)
    texto: str = Field(default="", max_length=MAX_TEXTO)
    #: Item 15. Só base64/`data:`: o `app/pipeline.py` recusa URL em canal
    #: 'site' justamente porque não há credencial nenhuma que a justificasse.
    anexos: list = Field(default_factory=list, max_length=midia.MAX_ANEXOS)


# --------------------------------------------------------------------------
# Interpretação do bloco de ação
# --------------------------------------------------------------------------

MARCADOR_ACAO = "<<<ACAO>>>"
MARCADOR_PROMPT = "<<<PROMPT>>>"
MARCADOR_FIM = "<<<FIM>>>"


def tirar_prompt(resposta: str) -> tuple[str, str | None]:
    """Separa o texto com marcadores do do prompt-gerado (item 12).

    O gerador de prompt entrega `<<<PROMPT>>> ... <<<FIM>>>`. Quem lê a conversa
    no chat não deveria ver o marcador — ele é um envelope, não conteúdo — então
    o servidor tira o envelope e devolve `(o_que_fica_visivel, o_prompt)`. O
    front guarda `prompt_gerado` para o botão de copiar/inserir no painel.

    `rfind` (e não `find`) pelo mesmo motivo do bloco de ação: se o modelo
    explicar o formato como exemplo, o exemplo não vira prompt.
    """
    corte = resposta.rfind(MARCADOR_PROMPT)
    if corte < 0 or MARCADOR_FIM not in resposta[corte:]:
        return resposta.strip(), None
    corpo = resposta[corte:].split(MARCADOR_PROMPT, 1)[1].split(MARCADOR_FIM, 1)[0].strip()
    visivel = (resposta[:corte].strip() or "Aqui está o prompt do seu agente.")
    return visivel, corpo


def tirar_acao(resposta: str) -> tuple[str, dict | None]:
    """Separa o texto que a pessoa lê do pedido de ação.

    Devolve `(texto, acao)`, com `acao` em None quando não há bloco. O texto que
    a pessoa vê é o que o modelo escreveu ANTES do bloco: é a resposta em
    linguagem natural, e o bloco é máquina falando com máquina.

    O bloco é procurado com `rfind`, não com `find`: se o modelo explicar o
    formato como exemplo ("termine assim: <<<ACAO>>> ..."), o exemplo sai junto
    e não pode virar ação. O último bloco é o que vale, porque é o mais perto do
    fim — e o fim é onde a instrução manda colocar.

    Bloco sem `<<<FIM>>>` é bloco malformado, e aí devolve o texto inteiro: a
    pessoa lê a explicação e o servidor não executa nada. Cortar o texto nesse
    caso deixaria a pessoa olhando uma resposta vazia.
    """
    corte = resposta.rfind(MARCADOR_ACAO)
    if corte < 0:
        return resposta.strip(), None
    if MARCADOR_FIM not in resposta[corte:]:
        return resposta.strip(), None
    texto = resposta[:corte].strip()
    corpo = resposta[corte:].split(MARCADOR_ACAO, 1)[1].split(MARCADOR_FIM, 1)[0].strip()
    try:
        dados = json.loads(corpo)
    except (ValueError, TypeError):
        return texto, None
    if not isinstance(dados, dict) or not dados.get("acao"):
        return texto, None
    return texto, dados


def resultado_como_texto(resultado: dict) -> str:
    """O que volta para o modelo depois de uma ação.

    Vai como mensagem de usuário e não como prompt de sistema porque é fato
    sobre ESTE turno, e porque o prompt é o que define quem o agente é: trocar o
    prompt no meio da conversa é exatamente o que o item 14 proíbe.
    """
    if resultado.get("ok"):
        return ("RESULTADO DA AÇÃO (foi executada):\n"
                + json.dumps(resultado.get("dados", {}), ensure_ascii=False))
    return ("RESULTADO DA AÇÃO (não foi executada, nada mudou):\n"
            + str(resultado.get("motivo", "erro desconhecido")))


# --------------------------------------------------------------------------
# Execução das ações
# --------------------------------------------------------------------------


def _plano_por_id(plano_id: str) -> cobranca.Plano | None:
    for p in cobranca.CATALOGO:
        if p.id == str(plano_id or "").strip().lower():
            return p
    return None


def _data_br(quando) -> str:
    """`dt.datetime` ou ISO string (o que `contexto()` da pelos períodos).

    O `app/limites.contexto()` devolve `fim_periodo` em string ISO (veio de
    jsonb), e os repositórios de cobrança devolvem `datetime`. O agente não
    pode quebrar num desses dois conforme a fonte — quem lê o erro é a pessoa
    que acabou de dar o comando.
    """
    if not quando:
        return "sem data"
    if isinstance(quando, str):
        try:
            quando = dt.datetime.fromisoformat(quando.replace("Z", "+00:00"))
        except ValueError:
            return quando
    if isinstance(quando, dt.datetime):
        return quando.strftime("%d/%m/%Y")
    return str(quando)


def _planos_como_texto() -> str:
    return json.dumps([p.detalhe() for p in cobranca.CATALOGO], ensure_ascii=False)


#: `nome da ação` -> implementação. A chave é a mesma de `AgenteInterno.acoes`;
#: o `_executar` só chega aqui depois de conferir que a ação está na lista do
#: agente, então este dict não é a fronteira de segurança — a lista é.
#:
#: Todas as implementações são `async def`, mesmo as que não esperam nada
#: (listar planos, link de cadastro). Motivo prático: `_executar` faz
#: `await _ACOES[nome](...)`, e uma função síncrona nesse lugar estoura
#: `TypeError: object dict can't be used in 'await' expression` — que só
#: apareceria em produção, no meio de uma conversa. Mixar as duas formas num
#: mesmo dicionário é o tipo de erro que o teste de unidade não pega.
_ACOES = {}


async def _acao_listar_planos(_usuario_id: str | None, _dados: dict) -> dict:
    return {"planos": json.loads(_planos_como_texto())}


async def _acao_criar_conta(_usuario_id: str | None, dados: dict) -> dict:
    """Link de cadastro, com o plano que a pessoa citava (se houver).

    Não há segredo aqui: é a tela pública de login. O `plano` vai na query
    string só para a tela dizer "você está olhando o plano X" — a conta nasce no
    teste de 7 dias de qualquer jeito, porque o teste é automático (item 11) e o
    plano escolhido entra pela assinatura, nunca pela URL.
    """
    p = _plano_por_id(dados.get("plano")) if dados.get("plano") else None
    return {
        "link": LINK_CADASTRO,
        "plano_sugerido": p.id if p else None,
        "plano_sugerido_nome": p.nome if p else None,
        "observacao": "O teste de 7 dias comeca sozinho, sem cartao.",
    }


async def _acao_comparar_planos(_usuario_id: str | None, dados: dict) -> dict:
    a = _plano_por_id(dados.get("plano_a"))
    b = _plano_por_id(dados.get("plano_b"))
    if not a or not b:
        return {"erro": "plano invalido",
                "planos": [p.id for p in cobranca.CATALOGO]}
    return {"a": a.detalhe(), "b": b.detalhe()}


async def _acao_minha_conta(usuario_id: str, _dados: dict) -> dict:
    """A conta, do jeito que `app/limites.py` já calcula para a tela.

    Reusar `contexto()` em vez de remontar aqui é o que garante que o número que
    o agente de suporte lê é o mesmo que o cliente lê no painel. Se divergirem, a
    pessoa percebe que um dos dois mente — e o mentiroso é o agente.
    """
    r = await contexto_de_cota(usuario_id)
    agentes = r.get("agentes") or {}
    return {"conta": {
        "plano": (r.get("plano") or {}).get("nome"),
        "plano_id": (r.get("plano") or {}).get("id"),
        "status": r.get("status"),
        "ciclo": r.get("ciclo"),
        "fim_periodo": _data_br(r.get("fim_periodo")),
        "dias_restantes": r.get("dias_restantes"),
        "em_teste": r.get("em_teste"),
        "agentes_usados": agentes.get("usados"),
        "agentes_limite": agentes.get("limite"),
        "canais_por_agente_limite": (r.get("canais_por_agente") or {}).get("limite"),
        "mensagens_no_mes": r.get("consumo_total"),
        "limite_total_do_mes": r.get("limite_total"),
        "por_agente": r.get("consumo"),
        "troca_agendada": r.get("troca_agendada"),
        "cancelamento_agendado": r.get("cancelamento_agendado"),
    }}


async def _acao_mudar_plano(usuario_id: str, dados: dict) -> dict:
    """Agenda a troca no fim do período pago — a regra do item 9.

    Em upgrade, abre a cobrança ANTES de agendar e NÃO confirma pagamento: a
    troca só entra em vigor quando alguém da equipe confirmar. Sem isso, "mudei
    meu plano" num chat seria serviço de graça, e o item 13 pede cobrança, não
    presente.
    """
    destino = _plano_por_id(dados.get("plano"))
    if destino is None:
        return {"erro": "plano inexistente",
                "planos": [p.id for p in cobranca.CATALOGO]}
    if destino.dias_gratis:
        return {"erro": "o teste de 7 dias nao pode ser contratado: "
                        "ele e automatico e gratuito."}
    ciclo = str(dados.get("ciclo") or "mensal").lower()
    if ciclo not in ("mensal", "anual"):
        return {"erro": "ciclo invalido: use 'mensal' ou 'anual'."}

    r = await contexto_de_cota(usuario_id)
    if not r.get("fim_periodo"):
        return {"erro": "sua assinatura nao tem periodo em aberto."}
    atual = (r.get("plano") or {}).get("nome")
    ordem_atual = (r.get("plano") or {}).get("ordem") or 0
    preco = destino.preco_anual if ciclo == "anual" else destino.preco_mensal
    upgrade = destino.ordem > ordem_atual

    pagamento = None
    if upgrade and preco > 0:
        pagamento = await rc.criar_pagamento(
            usuario_id, destino.id, ciclo, preco, metodo="suporte")

    await rc.agendar_troca(usuario_id, destino.id, ciclo)
    return {
        "plano_atual": atual,
        "plano_novo": destino.nome,
        "ciclo": ciclo,
        "valor": preco,
        "entra_em": _data_br(r.get("fim_periodo")),
        "upgrade": upgrade,
        "pagamento": pagamento.get("id") if pagamento else None,
        "pagamento_status": pagamento.get("status") if pagamento else None,
        "observacao": (
            "Upgrade: a cobranca ficou PENDENTE e a troca so vale depois que o "
            "pagamento for confirmado pela equipe."
            if upgrade else
            "A troca vale a partir do fim do periodo ja pago."),
    }


async def _acao_cancelar_plano(usuario_id: str, _dados: dict) -> dict:
    a = await rc.agendar_cancelamento(usuario_id)
    return {"cancela_em": _data_br(a.get("cancela_em")),
            "fim_periodo": _data_br(a.get("fim_periodo")),
            "observacao": "Cancelar nao apaga agentes, canais nem conversas."}


async def _acao_reativar_plano(usuario_id: str, _dados: dict) -> dict:
    a = await rc.reverter_cancelamento(usuario_id)
    return {"cancela_em": a.get("cancela_em"),
            "fim_periodo": _data_br(a.get("fim_periodo")),
            "observacao": "Cancelamento agendado desfeito."}


async def _acao_falar_com_time(_usuario_id: str | None, _dados: dict) -> dict:
    """O e-mail de atendimento é o do item 10, lido do mesmo lugar.

    Uma fonte só: se o agente de suporte tivesse o e-mail no próprio prompt,
    trocar o contato no painel do admin não mudaria nada e o endereço velho
    continuaria atendendo gente.
    """
    email = (await repo.ler_config("email_atendimento", "")).strip()
    return {"email": email or "nao configurado",
            "observacao": "Se estiver vazio, o administrador preenche no "
                          "painel do admin, na aba Perfil."}


_ACOES.update({
    "listar_planos": _acao_listar_planos,
    "criar_conta": _acao_criar_conta,
    "comparar_planos": _acao_comparar_planos,
    "minha_conta": _acao_minha_conta,
    "mudar_plano": _acao_mudar_plano,
    "cancelar_plano": _acao_cancelar_plano,
    "reativar_plano": _acao_reativar_plano,
    "falar_com_time": _acao_falar_com_time,
})


class Recusa(Exception):
    """A ação não rodou. A mensagem é o que a pessoa vai ler."""


async def _executar(agente: ai.AgenteInterno, usuario_id: str | None,
                    pedido: dict, *, aguardando_confirmacao: str = "") -> dict:
    """Roda a ação pedida e devolve o que a tela precisa mostrar.

    Quatro recusas, todas antes de tocar em qualquer conta:

    1. Ação fora da lista do agente. `listar_planos` existe para o produto e
       para o suporte, mas um pedido de `mudar_plano` do agente da home é
       recusado: a lista é por agente, não global.
    2. Ação que mexe em dado de valor (`Acao.valor`) sem ninguém logado. É a
       única forma de o agente de suporte rodar sem dono de conta.
    3. Ação que muda a assinatura (`Acao.confirma`) sem a pessoa ter dito que
       pode. E a segunda barreira é o `aguardando_confirmacao`: recusada uma
       vez neste turno, a mesma ação não passa com `confirmado: true` ainda
       neste turno — senão o modelo "confirmava" por conta da cliente, que é
       exatamente o que a regra quer impedir. Só no turno seguinte (isto é,
       depois que a pessoa escreveu algo) é que passa.
    4. Corpo pedindo outra conta. Não existe campo para isso e `usuario_id` vem
       do token, nunca do corpo — mas a checagem existe para o dia em que
       alguém acrescentar um.

    Recusa não é erro HTTP: a pessoa está conversando, não preenchendo
    formulário. Ela lê a recusa e continua, e o modelo explica em voz alta o que
    aconteceu.
    """
    nome = str(pedido.get("acao") or "").strip()
    acao = agente.acao(nome)
    if acao is None:
        disponiveis = ", ".join(a.nome for a in agente.acoes) or "nenhuma"
        raise Recusa(f"a acao {nome or '(vazia)'} nao existe para este agente; "
                     f"o que existe e: {disponiveis}")
    if acao.valor and not usuario_id:
        raise Recusa("esta acao precisa de uma conta com login")
    if acao.confirma:
        if not pedido.get("confirmado"):
            raise Recusa(
                f"{nome} mexe na assinatura e a pessoa ainda nao confirmou: "
                "pergunte se ela quer mesmo isso e espere a resposta")
        if aguardando_confirmacao == nome:
            raise Recusa(
                f"a pessoa nao respondeu nada desde que voce perguntou sobre a "
                f"{nome}; voce nao pode confirmar por ela. Escreva a pergunta e "
                "so chame de novo (com confirmado: true) depois que ela "
                "responder em outra mensagem")
    if pedido.get("usuario") and str(pedido["usuario"]) != str(usuario_id):
        raise Recusa("nao mexo na conta de outra pessoa; so falo da sua, que e "
                     "a que esta logada agora")

    dados = {campo: pedido.get(campo) for campo in acao.campos if campo in pedido}
    try:
        retorno = await _ACOES[nome](usuario_id, dados)
    except Exception as e:  # noqa: BLE001
        log.exception("Ação %s do agente %s falhou", nome, agente.chave)
        raise Recusa(f"a acao {nome} falhou: {str(e)[:180]}") from e
    if isinstance(retorno, dict) and retorno.get("erro"):
        raise Recusa(f"a acao {nome} nao rodou: {retorno['erro']}")
    return {"rotulo": acao.rotulo, "nome": nome, "dados": retorno}


# --------------------------------------------------------------------------
# O turno
# --------------------------------------------------------------------------


async def _falar(agente: ai.AgenteInterno, canal: dict, externo: str,
                 corpo: Mensagem, usuario_id: str | None) -> dict:
    """Um turno inteiro: sessão, memória, ações e o texto que a pessoa lê.

    É o `app/pipeline.py` dos outros agentes com três diferenças, e as três são
    porque aqui a resposta volta na requisição:

      * a sessão vem pronta do chamador (a cota do item 14 é conferida antes);
      * o prompt é o da constante, não o da coluna `agentes.system_prompt`;
      * há o laço de ação, que só existe aqui porque só aqui o modelo executa
        alguma coisa.
    """
    anexos = midia.normalizar(corpo.anexos)
    registro = (corpo.texto or "").strip() or midia.descrever(anexos)
    if not registro:
        raise HTTPException(400, "Escreva algo ou anexe um arquivo.")

    sessao = await repo.obter_ou_criar_sessao(canal["agente_id"], canal["id"], externo)
    await repo.salvar_mensagem(sessao["id"], False, registro)

    historico = await repo.historico_sessao(sessao["id"])
    memoria = gemini.ler_memoria(sessao.get("memoria"))
    baixados, falhas = await pipeline.resolver_anexos(canal, anexos)
    pergunta = pipeline.texto_com_aviso(registro, falhas)

    acao_tela: dict | None = None
    texto = ""
    # Ação de assinatura recusada por falta de confirmação NESTE turno. Zera a
    # cada requisição: uma mensagem nova da pessoa é a confirmação que faltava.
    aguardando_confirmacao = ""
    for _ in range(MAX_TURNOS_ACAO):
        bruto = await gemini.responder(agente.prompt, historico, pergunta,
                                       memoria, baixados)
        texto, pedido = tirar_acao(bruto)
        if pedido is None:
            break
        nome_acao = str(pedido.get("acao") or "").strip()
        _acao = agente.acao(nome_acao)
        try:
            acao_tela = await _executar(
                agente, usuario_id, pedido,
                aguardando_confirmacao=aguardando_confirmacao)
            pergunta = resultado_como_texto({"ok": True, "dados": acao_tela})
        except Recusa as recusa:
            acao_tela = None
            if _acao is not None and _acao.confirma:
                aguardando_confirmacao = nome_acao
            pergunta = resultado_como_texto({"ok": False, "motivo": str(recusa)})
        # O histórico local cresce com o par pergunta/resposta, para o modelo
        # enxergar o que ele mesmo pediu. `baixados` zera: os bytes já foram
        # lidos no primeiro turno e reenviá-los em cada iteração só gastaria
        # contexto.
        historico = [*historico, {"de_ia": True, "texto": bruto},
                     {"de_ia": False, "texto": pergunta}]
        baixados = []

    # Item 12: o gerador entrega o prompt em `<<<PROMPT>>>`. O marcador é
    # máquina falando com a máquina e some da conversa, mas o texto do prompt
    # fica: o balão mostra prelude + prompt (o histórico não pode ter um
    # "aqui está o prompt" sem o prompt), e o front ganha `prompt_gerado` à
    # parte para o botão de copiar/inserir no painel.
    resposta, prompt_gerado = tirar_prompt(texto)
    if prompt_gerado is not None:
        resposta = f"{resposta}\n\n{prompt_gerado}" if resposta else prompt_gerado
    await repo.salvar_mensagem(sessao["id"], True, resposta)
    trechos = await repo.ultimos_trechos(sessao["id"], 6)
    nova = await gemini.atualizar_memoria(agente.prompt, memoria, trechos)
    if nova != memoria:
        await repo.atualizar_memoria(sessao["id"], nova)

    resultado = {"resposta": resposta, "acao": acao_tela}
    if prompt_gerado is not None:
        resultado["prompt_gerado"] = prompt_gerado
    return resultado


# --------------------------------------------------------------------------
# Rotas
# --------------------------------------------------------------------------


@router.get("/agentes")
async def quais():
    """Quem são os agentes internos. Só o que a tela precisa mostrar."""
    return {"agentes": ai.para_json()}


@router.get("/{chave}/historico")
async def historico(chave: str, request: Request, sessao: str = ""):
    """A conversa recarregada depois de um F5.

    Isso deu certo porque a sessão está no banco e não na memória do processo:
    quem recarrega a página continua vendo o que já conversou, e quem fecha o
    navegador e volta amanhã também.
    """
    agente = _agente(chave)
    par = await _par(agente)
    externo = await _externo(agente, request, sessao)
    if externo is None:
        return {"mensagens": []}
    sessao_id = await repo.id_da_sessao(par[1]["agente_id"], par[1]["id"], externo)
    if sessao_id is None:
        return {"mensagens": []}
    return {"mensagens": await repo.mensagens_da_sessao(sessao_id, limite=50)}


@router.post("/{chave}")
async def conversar(chave: str, corpo: Mensagem, request: Request):
    """Um turno de conversa com um agente interno.

    A resposta é a mensagem da IA mais a ação que ela executou, para o front
    renderizar o botão certo (link de cadastro, tabela de planos) sem ter que
    adivinhar no texto.
    """
    agente = _agente(chave)
    externo = await _externo(agente, request, corpo.sessao)
    par = await _par(agente)
    _linha_agente, canal = par

    usuario = getattr(request.state, "usuario", None)
    await _exigir_plano(agente, usuario)

    # Segunda rede do item 14, e ela vem antes da cota por conversa porque é a
    # que pega o abuso: quem troca o campo `sessao` a cada mensagem nunca chega
    # a 12 numa conversa só, e cada uma das mensagens é uma chamada de IA nossa.
    # Só o anônimo entra — quem está logado tem consumo medido no mês e plano de
    # verdade, e trancar essa pessoa por causa de um contador em memória seria
    # cobrar duas vezes pelo mesmo motivo.
    if usuario is None:
        estourou = limite_turnos.checa_turno_anonimo(
            request, settings.limite_turno_anonimo_hora,
            settings.limite_turno_anonimo_dia)
        if estourou == "hora":
            raise HTTPException(
                429,
                "Você mandou mensagens neste atendente rápido demais. "
                "Experimente de novo em uma hora — ou crie a conta, que é "
                "grátis e abre o painel inteiro.",
                headers={"Retry-After": "3600"},
            )
        if estourou == "dia":
            raise HTTPException(
                429,
                "Você usou muitas mensagens do atendente de teste hoje. Volte "
                "amanhã, ou crie a conta (grátis, sem cartão): o painel abre "
                "na hora e os limites são bem maiores.",
                headers={"Retry-After": "86400"},
            )

    # Item 14: o agente da home não pode ser usado para sempre. A cota é por
    # conversa anônima e é conferida ANTES de chamar o modelo, para o limite não
    # custar uma chamada de IA à pessoa que já bateu nele.
    if agente.limite_mensagens and settings.has_db:
        usadas = await repo.contar_mensagens_da_sessao(
            canal["agente_id"], canal["id"], externo)
        if usadas >= agente.limite_mensagens:
            raise HTTPException(
                429,
                f"Você usou as {agente.limite_mensagens} mensagens deste "
                "atendente de teste. Criando a conta, o painel abre: lá dá para "
                "criar os agentes, ligar os canais e ver tudo funcionando.",
                headers={"Retry-After": "86400"},
            )

    return await _falar(agente, canal, externo, corpo,
                        usuario.id if usuario else None)


# --------------------------------------------------------------------------
# Auxiliares de rota
# --------------------------------------------------------------------------


def _agente(chave: str) -> ai.AgenteInterno:
    agente = ai.get(chave)
    if agente is None:
        # 404 e não 400: a chave é um enum, não entrada de usuário. A lista
        # completa está em `/api/interno/agentes`.
        raise HTTPException(404, "Agente interno desconhecido.")
    return agente


def _logado(request: Request) -> bool:
    return getattr(request.state, "usuario", None) is not None


async def _exigir_plano(agente: ai.AgenteInterno, usuario) -> None:
    """O recurso é do plano X? O catálogo já diz isso; aqui é onde se cumpre.

    `Plano.sem` do Início lista "sem acesso ao gerador de prompt por
    perguntas". Se essa frase fosse só texto de marketing, o painel aceitaria
    o clique e o chat voltaria 403 — a pessoa experimentaria um plano que não
    comprou, o que é o contrário de "regras claras com limites e preços".

    A comparação é por `ordem` do catálogo, e não por nome: `interno.ordem >=
    pro.ordem` continua valendo se alguém renomear o plano amanhã.
    """
    if not agente.plano_minimo or usuario is None:
        return
    atual = await contexto_de_cota(usuario.id, usuario.eh_admin)
    if atual.get("sem_cota"):
        return  # admin não tem plano, e não é barrado
    exigido = _plano_por_id(agente.plano_minimo)
    obtido = (atual.get("plano") or {}).get("id")
    if exigido is None or obtido is None:
        return
    ordem = {p.id: p.ordem for p in cobranca.CATALOGO}
    if ordem.get(obtido, -1) < exigido.ordem:
        raise HTTPException(
            403,
            f"O {agente.titulo.lower()} faz parte do plano "
            f"{exigido.nome} ou de um maior. A sua conta está no "
            f"{(atual.get('plano') or {}).get('nome')}. Peço a troca pelo "
            "atendimento, e a troca só entra no fim do período que você já pagou.",
        )


async def _externo(agente: ai.AgenteInterno, request: Request,
                   sessao: str = "") -> str:
    """De qual conversa este turno é, e a porta de entrada do login.

    Logado: o id da conta vence o que o corpo diz. Um cliente mandando o
    `sessao` de outra pessoa continua na conversa DELE — é o que impede ver o
    histórico de outra conta trocando um campo do formulário.

    Anônimo: o token do navegador, e só para o agente que aceita anônimo.
    """
    usuario = getattr(request.state, "usuario", None)
    if usuario is not None:
        # `perfis.id` é coluna UUID e o `Usuario.id` carrega um uuid.UUID. Aqui
        # ele vira chave de sessão numa coluna TEXT (`sessoes.usuario_externo`):
        # mandar o objeto era para o asyncpg quebraria em "expected str, got
        # UUID" — que os canais externos nunca viram porque lá a chave sempre
        # era string (o id do Telegram etc.), e que o chat interno é o primeiro
        # a passar por cima.
        return str(usuario.id)
    if agente.exige_login:
        raise HTTPException(401, "Este atendente precisa de você com a conta aberta.",
                            headers={"WWW-Authenticate": "Bearer"})
    token = (sessao or "").strip()
    if len(token) < 8:
        raise HTTPException(400, "Sessão de conversa inválida.")
    return token[:80]


async def _par(agente: ai.AgenteInterno) -> tuple[dict, dict]:
    par = await repo.par_interno(agente.chave)
    if par is None:
        # O boot ainda está criando (ou o banco sumiu). 503 e não 500: é
        # transitório e o front pode tentar de novo.
        raise HTTPException(503, "Atendente ainda subindo. Tente de novo em instantes.")
    return par

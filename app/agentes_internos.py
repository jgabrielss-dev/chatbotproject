"""Os três agentes internos da plataforma — itens 7, 12 e 13.

São os agentes que não são do cliente:

  `produto`   o da home (item 7): vende, explica planos e limites, cria a conta
  `suporte`  o do botão flutuante (item 13): olha a conta de quem está falando
              e agenda mudança de plano, cancelamento ou reativação
  `prompt`    o gerador de system prompt (item 12): pergunta até ter contexto
              suficiente e devolve a instrução pronta

Por que as definições vivem em código, e não em `app_config` ou no formulário
do painel: o item 14 diz que eles "nunca podem ser desviados". Uma definição em
banco é uma definição que alguém com acesso a escrita muda — e o único jeito de
um agente interno virar outra coisa é exatamente trocar o prompt dele. Aqui o
prompt é uma constante: para o agente se desviar, é preciso editar o código e
subir deploy, o que deixa rastro e passa por revisão. O que o cliente controla é
a *conversa*, nunca a identidade do agente.

O item 15 aparece aqui sem esforço: estes agentes passam pelo mesmo
`app/pipeline.py` dos outros, e a declaração de multimodal mora no prompt de
sistema, em `app/ai/gemini.py`. Por isso um anexo funciona nos chats internos
sem uma linha a mais aqui.

O que o modelo pode fazer é declarado, não livre. Cada agente tem uma lista
fechada de ações; ele pede uma emitindo um bloco `<<<ACAO>>>`, o servidor valida
contra essa lista e devolve o resultado. Ação fora da lista é recusada e o
modelo é avisado — é o que impede um "injection" de botar o agente de suporte a
trocar o plano de outra pessoa (a ação só roda na conta de quem está logado) ou
de inventar preço (o preço vem do catálogo, não do texto do modelo).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app import cobranca

# --------------------------------------------------------------------------
# Ferramenta
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Acao:
    """Uma coisa que o agente pode pedir ao servidor para fazer.

    `campos` é a lista de campos aceitos; o servidor valida os tipos, então um
    campo inventado pelo modelo é simplesmente ignorado.
    """

    nome: str
    rotulo: str
    ajuda: str
    campos: tuple[str, ...] = ()
    #: Quando a ação mexe em dado do cliente, ela é executada só na conta de
    #: quem está falando. Não existe campo para escolher a conta, e isso é
    #: proposital: se existisse, "troque o plano da conta X" seria um pedido
    #: válido e o agente de viraria painel do admin.
    valor: bool = False


# --------------------------------------------------------------------------
# Agente
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AgenteInterno:
    chave: str
    nome: str
    titulo: str
    apresentacao: str
    prompt: str
    acoes: tuple[Acao, ...] = ()
    #: Login é obrigatório. O de suporte mexe na conta de quem fala, e o
    #: gerador de prompt é recurso pago (o catálogo do item 9 diz que o plano
    #: Início não o inclui).
    exige_login: bool = True
    #: Plano mínimo, quando o recurso não é de todos. Vem do mesmo catálogo que
    #: a tela mostra: `Plano.sem` do Início lista "sem acesso ao gerador de
    #: prompt por perguntas", e um item do catálogo que o código ignora é uma
    #: promessa que o produto quebra sozinho. `None` = liberado para qualquer
    #: conta (e para quem não tem conta, se `exige_login` for False).
    plano_minimo: str | None = None
    #: Item 14: o agente da home "não pode ser usado para sempre". O limite é de
    #: mensagens por conversa anônima; depois disso a resposta manda criar a
    #: conta. Zero desliga o limite (usado por quem testa).
    limite_mensagens: int = 0
    contexto: dict = field(default_factory=dict)

    def acao(self, nome: str) -> Acao | None:
        for a in self.acoes:
            if a.nome == nome:
                return a
        return None

    def para_json(self) -> dict:
        return {
            "chave": self.chave,
            "nome": self.nome,
            "titulo": self.titulo,
            "apresentacao": self.apresentacao,
            "exige_login": self.exige_login,
            "plano_minimo": self.plano_minimo,
            "limite_mensagens": self.limite_mensagens,
        }


# --------------------------------------------------------------------------
# Blocos de prompt compartilhados
# --------------------------------------------------------------------------

#: Como o modelo pede uma ação. Fica numa constante só porque os três agentes
#: precisam falar exatamente o mesmo dialeto — se um inventasse o formato, o
#: servidor não entenderia e a ação viraria texto morto na resposta.
COMO_USAR_ACOES = """\
## Pedir uma ação ao sistema

Para escrever, alterar ou consultar algo de verdade, termine a sua mensagem
exatamente assim:

<<<ACAO>>>
{"acao": "NOME_DA_ACAO", "campo": "valor"}
<<<FIM>>>

Regras:
- O bloco vai no fim da mensagem, e é a última coisa que você escreve.
- Só use as ações da sua lista. Uma ação que não está na lista não existe, e o
  servidor a recusa: você vai receber de volta o motivo.
- Use o valor que a pessoa pediu, nunca um que você inventou. Se a pessoa não
  disse o plano, pergunte.
- Depois que o servidor responder, você recebe o resultado e explica em
  português, com o número que veio no resultado. Nunca arredonde preço na sua
  cabeça: se o resultado traz R$ 80,00, é R$ 80,00.
- Uma ação por mensagem. Duas ações no mesmo bloco: só a primeira é executada.
- Se a ação deu erro, diga o que o servidor respondeu. Não insista e não tente
  outro caminho: só existe este."""

#: A diferença entre "responder" e "fazer". O erro clássico de um agente de
#: produto é prometer o que o sistema não fez ("pronto, seu plano foi mudado"),
#: e aí a pessoa bate na tela e continua no plano antigo.
VERDADE_ANTES_DE_PROMETER = """\
## Não prometa o que você não fez

Você só pode dizer que algo foi feito se o resultado de uma ação vier dizendo
que foi. Se você não chamou a ação, você não sabe: diga o que precisa e pare.
Prometer e não executar é pior que dizer "posso fazer isso agora?"."""


def _brl(valor: float) -> str:
    """`1250.0` -> `'1.250,00'`. Um agente que escreve 'R$ 1.250.00' ou
    'R$ 1250' é pior do que um que não responde: a pessoa confia no número."""
    inteiro, _, centavos = f"{valor:.2f}".partition(".")
    return f"{int(inteiro):,}".replace(",", ".") + "," + centavos


def _tabela_precos() -> str:
    """O catálogo em texto, para dentro do prompt.

    Vai no *prompt* e não na mensagem porque preço é fato do produto, não da
    conversa: os três agentes precisam dele sempre, e a mesma tabela servindo
    para os dois lados evita que o texto do modelo e a tela discordem.
    """
    linhas = [
        "| plano | por mês | por ano (20% off) | agentes | canais por agente |"
        " mensagens por agente por mês |",
        "|---|---|---|---|---|---|",
    ]
    for p in cobranca.CATALOGO:
        mensagens = f"{p.max_mensagens_por_agente_mes:,}".replace(",", ".")
        linhas.append(
            f"| {p.nome} | R$ {_brl(p.preco_mensal)} | R$ {_brl(p.preco_anual)}"
            f" | {p.max_agentes} | {p.max_canais_por_agente} | {mensagens} |"
        )
    excedente = ", ".join(
        f"{f'{t:,}'.replace(',', '.')} por R$ {_brl(v)}"
        for t, v in cobranca.PRECO_POR_MIL_MENSAGEM
    )
    linhas.append("")
    linhas.append(f"Mensagens além do plano, por pacote: {excedente}.")
    linhas.append("O teste de 7 dias não pode ser contratado: ele é automático e gratuito.")
    return "\n".join(linhas)


# --------------------------------------------------------------------------
# 1) Agente da home (item 7)
# --------------------------------------------------------------------------

PRODUTO = AgenteInterno(
    chave="produto",
    nome="Assistente do Chatbot Project",
    titulo="Fale com a gente",
    apresentacao=(
        "Pergunte sobre planos, limites e como o bot funciona. Se quiser, "
        "crio a sua conta agora."
    ),
    exige_login=False,
    limite_mensagens=12,
    prompt=f"""\
Você é o assistente do Chatbot Project, uma plataforma de chatbots para
atendimento. Está na página inicial do site, falando com alguém que ainda NÃO
tem conta. Sua parte é essa pessoa entender o produto em cinco minutos e criar a
conta se quiser.

## O produto, sem rodeio

Um bot atende no Telegram, WhatsApp e Instagram. Você cria um agente (é a
personalidade e o que ele faz), liga um ou mais canais nele, e ele responde
quem chega pelos canais. As conversas ficam salvas, com o que o bot aprendeu de
cada pessoa, e cada conta tem o seu painel, separado do dos outros. Todo agente
lê foto, áudio, vídeo e arquivo que a pessoa mandar.

## Planos e limites

{_tabela_precos()}

Três coisas que as pessoas sempre perguntam, e que você tem que dizer sem que
perguntem:
- O limite de mensagens é POR AGENTE, por mês. Não é um saldo único da conta,
  então um cliente com 20 agentes não esvazia a cota de um só.
- O que passa do limite é pacote, não medidor aberto, e o preço por mil cai
  quanto maior o pacote.
- A troca de plano só entra no fim do período já pago. Quem muda para mais
  continua com o que pagou até lá; ninguém perde dia pago.

## O teste

7 dias, de graça, com 1 agente, 1 canal e 100 mensagens. Não precisa cartão.
Depois dos 7 dias a conta para de responder até escolher um plano, mas nada do
que foi configurado é apagado.

## Como criar a conta

Use a ação `criar_conta` para dar o link de cadastro. A pessoa escolhe a senha e
confirma o e-mail; o teste de 7 dias começa sozinho. Não peça senha, não peça
cartão e não peça dado pessoal para ela mandar no chat.

## Como você escreve

- Português do Brasil, direto, sem formal e sem tutor.
- De 2 a 5 frases por resposta, salvo quando a pessoa pede a tabela de preços.
- Se a pergunta não é sobre este produto, responda em uma frase que você não
  sabe e ofereça o que você sabe.
- Nunca invente preço, limite, prazo ou funcionalidade que não esteja aqui em
  cima. Se não está na tabela, você não sabe: mande a pessoa falar com o suporte
  pelo botão de atendimento.

{COMO_USAR_ACOES}

## Ações que você tem

- `listar_planos`: manda a tabela completa de planos, preços e limites. Use
  quando a pessoa perguntar "quanto custa" sem dizer qual plano.
- `comparar_planos`: explica as diferenças entre dois planos, principalmente o
  que cada um NÃO tem. Use quando a pessoa estiver em dúvida entre dois.
- `criar_conta`: devolve o link de cadastro. Use quando a pessoa disser que
  quer criar conta, quando perguntar como começar, ou no fim de uma resposta em
  que ela claramente gostou.

{VERDADE_ANTES_DE_PROMETER}""",
    acoes=(
        Acao("listar_planos", "Ver planos",
             "Manda a tabela de planos, preços e limites."),
        Acao("comparar_planos", "Comparar planos",
             "Compara dois planos, incluindo o que cada um não tem.",
             campos=("plano_a", "plano_b")),
        Acao("criar_conta", "Criar conta",
             "Devolve o link de cadastro, com o teste de 7 dias já valendo.",
             campos=("plano",)),
    ),
)

# --------------------------------------------------------------------------
# 2) Agente de suporte (item 13)
# --------------------------------------------------------------------------

SUPORTE = AgenteInterno(
    chave="suporte",
    nome="Suporte",
    titulo="Atendimento",
    apresentacao=(
        "Olho os seus dados e posso trocar plano, cancelar ou reativar a "
        "assinatura por aqui."
    ),
    exige_login=True,
    prompt=f"""\
Você é o suporte do Chatbot Project. Está falando com o DONO da conta que está
logada. Você não é o dono do produto: você explica, olha o que está na conta e
executa o que for pedido, sem nunca prometer o que não fez.

Você tem duas restrições que não são suas, e que protegem a pessoa que está na
sua frente:

1. Você só enxerga e só altera a conta de quem está logado agora. Se pedirem
   para mexer em outra conta, diga que isso você não faz e ofereça o e-mail de
   atendimento.
2. Você não dá desconto, não estende teste e não confirma pagamento por conta
   própria. Se a pessoa pedir algo disso, explique que é decisão da equipe e
   ofereça o contato.

## Planos e limites

{_tabela_precos()}

## As regras de cobrança, que você não pode quebrar

- Plano pago NUNCA se renova sozinho. Sem gateway confirmado, renovar é dar de
  graça o que a pessoa não pagou.
- A troca de plano entra no fim do período já pago. A pessoa pode pedir a troca
  quando quiser; o efeito é sempre depois do que já pagou.
- Teste vencido não vira plano pago sozinho, e a troca agendada morre junto:
  seria serviço grátis.
- Cancelamento também vale no fim do período. Quem cancela continua usando até
  a data que pediu.
- Cancelar não apaga nada: agentes, canais e conversas continuam guardados.

## Como você escreve

- Português do Brasil, direto. Frases curtas.
- Diga o número que veio no resultado da ação, com o valor em reais e a data.
- Se a pessoa perguntar algo que você não tem dado, chame `minha_conta` antes de
  responder. Adivinhar quantidade de mensagem usada é o erro que faz a pessoa
  abrir chamado no suporte de verdade.
- Encaminhe para a pessoa humana (`falar_com_time`) quando o pedido for desconto,
  contrato, nota fiscal, ou mexer em outra conta.

{COMO_USAR_ACOES}

## Ações que você tem

- `minha_conta`: plano atual, status, quando o período acaba, quantos dias
  faltam, quantos agentes e canais já estão usados e as mensagens do mês. É a
  primeira coisa a chamar quando a pessoa perguntar "como está minha conta".
- `listar_planos`: a tabela de planos. Chame quando pedirem os preços.
- `mudar_plano`: agenda a troca para o fim do período pago. Campo `plano`
  (obrigatório) e `ciclo` ("mensal" ou "anual"). Se for upgrade, o servidor
  abre a cobrança e a troca só vale depois que o pagamento for confirmado —
  diga isso, porque é o que a pessoa precisa ouvir para não achar que já
  mudou.
- `cancelar_plano`: agenda o cancelamento para o fim do período pago.
- `reativar_plano`: desmarca um cancelamento agendado.
- `falar_com_time`: devolve o e-mail de atendimento. Use para desconto, nota
  fiscal, contrato e qualquer coisa fora do seu alcance.

{VERDADE_ANTES_DE_PROMETER}""",
    acoes=(
        Acao("minha_conta", "Minha conta",
             "Plano, status, fim do período, consumo e limites em uso.",
             valor=True),
        Acao("listar_planos", "Ver planos", "Manda a tabela de planos e preços."),
        Acao("mudar_plano", "Trocar plano",
             "Agenda a troca para o fim do período pago; em upgrade abre a "
             "cobrança do valor.",
             campos=("plano", "ciclo"), valor=True),
        Acao("cancelar_plano", "Cancelar assinatura",
             "Agenda o cancelamento para o fim do período já pago.",
             valor=True),
        Acao("reativar_plano", "Reativar assinatura",
             "Desmarca um cancelamento que estava agendado.", valor=True),
        Acao("falar_com_time", "Falar com a equipe",
             "Devolve o e-mail de atendimento da plataforma."),
    ),
)

# --------------------------------------------------------------------------
# 3) Gerador de prompt (item 12)
# --------------------------------------------------------------------------

PROMPT_AGENTE = AgenteInterno(
    chave="prompt",
    nome="Gerador de prompt",
    titulo="Gerar a instrução",
    apresentacao=(
        "Me conta o que o agente vai fazer e eu escrevo a instrução pronta."
    ),
    exige_login=True,
    plano_minimo="pro",
    prompt="""\
Você escreve o *system prompt* de um agente de atendimento. A pessoa te conta o
que o agente precisa fazer e você devolve a instrução pronta para colar no
painel.

## Como trabalhar

Faça UMA pergunta por mensagem. Sete é o teto, e você não precisa chegar lá:
se já sabe o essencial, escreva o prompt.

As quatro coisas que um prompt de agente precisa, e a ordem em que vale
perguntar:
1. Quem é o agente e para quem ele atende (empresa, área, tom da conversa).
2. O que ele FAZ: as tarefas concretas, incluindo as que exigem informação que
   só existe no sistema (preço, prazo, estoque, número do pedido).
3. O que ele NÃO faz, e o que fazer quando não sabe. Esta é a parte que mais
   falta em prompt escrito à mão, e é a que evita o bot inventando preço.
4. Formato da resposta: tamanho, se usa lista, o que fazer com o áudio e com a
   foto que a pessoa mandar.

Se a pessoa responder de forma vaga ("quero um bot de vendas"), aprofunde o
que está faltando antes de escrever: vendas do quê, para quem, o que ele pode
prometer. Prompt genérico gera bot genérico, e isso volta como reclamação.

## A instrução que você entrega

- Escreva em segunda pessoa ("Você é...", "Você atende..."), como um prompt.
- Inclua o que fazer com anexos: foto, áudio, vídeo e arquivo chegam junto da
  mensagem, e o agente deve descrever a foto, transcrever o áudio, ler o
  documento, e dizer que não recebeu quando não recebeu.
- Diga o que fazer quando não souber a resposta, e de onde buscar.
- Não invente política da empresa (preço, prazo, garantia) que a pessoa não
  tenha dito. Use o que ela contou; se um número importa e ela não falou,
  pergunte.
- Entregue o texto entre estes marcadores, e nada depois do fim:

<<<PROMPT>>>
a instrução inteira, em linhas
<<<FIM>>>

## A frase de abertura

Antes dos marcadores, escreva uma ou duas frases explicando o que você montou
e o que a pessoa pode ajustar. Depois de `<<<FIM>>>`, nada.""",
    acoes=(),
)

AGENTES: dict[str, AgenteInterno] = {
    a.chave: a for a in (PRODUTO, SUPORTE, PROMPT_AGENTE)
}

#: Ordem de apresentação no menu (e o que o item 14 chama de "os três").
CHAVES: tuple[str, ...] = ("produto", "suporte", "prompt")


def get(chave: str) -> AgenteInterno | None:
    return AGENTES.get((chave or "").strip().lower())


def para_json() -> list[dict]:
    return [AGENTES[c].para_json() for c in CHAVES]

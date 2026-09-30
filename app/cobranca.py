"""Planos, limites e virada de período.

Este módulo é a fonte da verdade de PREÇO. A tabela `planos` no banco é só o
espelho para o banco saber do que se fala (e para o admin editar sem deploy),
mas todas as decisões de cota e de cobrança saem daqui, sem I/O — assim dá
para testar o catálogo inteiro sem banco nenhum.

Os números vêm de pesquisa de mercado (conferir `pesquisa()` para a fonte e a
data), não de chute:

  Chatbase (chatbase.co/pricing, consultado em 2026-09): Free $0 (50 créditos),
  Hobby $40 (700), Standard $150 (4.000), Pro $500 (15.000). Agente extra
  $25/mês. 20% de desconto no anual. 7 dias de teste.
  Botpress (botpress.com/pricing, 2026-09): Free $0 (25 conversas), Plus
  $150/mês *pago no ano* (250 conversas + pacotes de 100). "Annual · Save 20%".
  Typebot (typebot.io/pricing, 2026-09): Free 200 chats, Starter $39 (2.000),
  Pro $89 (10.000). Extra: $10 por 500 chats. Sem plano anual.

Três conclusões que moldaram o catálogo:

1. Ninguém cheap compete por mensagem avulsa no topo; competem por volume
   fechado. Por isso o limite é "N mensagens por AGENTE", e não um saldo
   único da conta: é o que impede um cliente grande de colocar 20 agentes e
   esvaziar a cota de um só.
2. Todo mundo dá 20% no anual (Chatbase e Botpress dizem 20% explicitamente).
   Adotamos 20%, o que é o piso do mercado — mais que isso e o desconto vira
   o produto.
3. O adicional (o que passa do plano) é vendido em pacote, não em medidor
   aberto. Chatbase vende créditos avulsos, Typebot vende $10/500 chats,
   Botpress vende packs de 100 conversas. É o `PRECO_POR_MIL_MENSAGEM`.

A conversão de moeda usa 5,0 R$/USD, a cotação de近郊 que os preços em dólar
das três páginas acima implicam para o mercado brasileiro (o Chatbase Hobby,
$40/mês, sai por R$ 199 no mercado nacional). Os preços ficam em REAIS: o
cliente-alvo paga em real e ver "$" num checkout brasileiro é atrito de venda.
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Catálogo
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Plano:
    """Um plano do catálogo. `dias_gratis` > 0 marca o período de teste."""

    id: str
    nome: str
    descricao: str
    preco_mensal: float          # R$ por 30 dias
    preco_anual: float           # R$ por 12 meses, já com o desconto
    max_agentes: int
    max_canais_por_agente: int
    max_mensagens_por_agente_mes: int
    ordem: int
    destaque: bool = False
    dias_gratis: int = 0
    # O que o plano NÃO tem. Mostrar a lista vale mais que dizer que tem
    # "tudo": o cliente decide comparando o que falta.
    sem: tuple[str, ...] = field(default_factory=tuple)

    @property
    def por_agente_mes(self) -> float:
        return self.preco_mensal / max(self.max_agentes, 1)

    @property
    def preco_efetivo_por_mil(self) -> float:
        """Quanto custa 1.000 mensagens neste plano, já rateado por agente.

        É o número que o usuário compara entre planos: R$ 9,90 contra R$ 4,00
        lê como "compensa subir de plano", que é exatamente a venda que
        queremos fazer.
        """
        return self.preco_mensal / max(self.max_agentes * self.max_mensagens_por_agente_mes, 1) * 1000

    def detalhe(self) -> dict:
        """O plano como vai para o navegador e para os agentes internos.

        `ordem` é a posição do plano na escada de preços, e ela é pública de
        propósito: é o que permite ao chat de suporte dizer "isso é um upgrade"
        ou "isso é um downgrade" sem inventar regra nova. Sem esta chave, todo
        `get("ordem")` no agente caía no 0 do `.get()` e *qualquer* troca era
        tratada como upgrade — um downgrade virava cobrança.
        """
        return {
            "id": self.id,
            "nome": self.nome,
            "descricao": self.descricao,
            "ordem": self.ordem,
            "preco_mensal": self.preco_mensal,
            "preco_anual": self.preco_anual,
            "max_agentes": self.max_agentes,
            "max_canais_por_agente": self.max_canais_por_agente,
            "max_mensagens_por_agente_mes": self.max_mensagens_por_agente_mes,
            "dias_gratis": self.dias_gratis,
            "destaque": self.destaque,
            "sem": list(self.sem),
            "preco_por_mil_mensagens": round(self.preco_efetivo_por_mil, 2),
            "preco_por_agente_mes": round(self.por_agente_mes, 2),
            "desconto_anual": (
                round(self.preco_mensal * 12 - self.preco_anual, 2) if self.preco_mensal else 0.0
            ),
        }


#: Desconto do pagamento antecipado anual. Mesmo número que Chatbase e Botpress
#: anunciam ("20% off yearly" / "Annual · Save 20%").
DESCONTO_ANUAL = 0.20

PLANO_TESTE = "teste"
PLANO_INICIO = "inicio"
PLANO_PRO = "pro"
PLANO_NEGOCIO = "negocio"
PLANO_ESPECIALISTA = "especialista"

# Os preços mensais são escolhidos para que o anual SAIA redondo depois do
# desconto (80 x 9,6 = 768; 200 x 9,6 = 1.920). Um "R$ 758,40" numa página de
# venda parece erro de digitação, e um desconto que não fecha em 20% é um
# desconto que o cliente vai conferir.
#
# Faixas de preço, e por que estes números:
#   R$ 80   = entrada. Chatbase Hobby é US$ 40 (~R$ 200 no mercado nacional) e
#              Typebot Starter é US$ 39; R$ 80 é a versão com 2 agentes que dá
#              para o cliente sair do teste com algo rodando.
#   R$ 200  = o plano do meio, onde cai a maioria (como o Standard da Chatbase,
#              a US$ 150).
#   R$ 500  = volume. Chatbase Pro é US$ 500.
#   R$ 1250 = operação grande; acima disso o cliente pede negotiate e o preço
#              vira "talk with us".
CATALOGO: tuple[Plano, ...] = (
    Plano(
        id=PLANO_TESTE,
        nome="Teste",
        descricao="7 dias para você montar seu primeiro agente e falar com ele de verdade.",
        preco_mensal=0.0,
        preco_anual=0.0,
        max_agentes=1,
        max_canais_por_agente=1,
        max_mensagens_por_agente_mes=100,
        ordem=0,
        dias_gratis=7,
    ),
    Plano(
        id=PLANO_INICIO,
        nome="Início",
        descricao="Para quem está saindo do teste e quer um bot no ar.",
        preco_mensal=80.0,
        preco_anual=768.0,       # 80 * 12 * 0.8
        max_agentes=2,
        max_canais_por_agente=2,
        max_mensagens_por_agente_mes=1_000,
        ordem=1,
        sem=("Sem acesso ao gerador de prompt por perguntas",),
    ),
    Plano(
        id=PLANO_PRO,
        nome="Pro",
        descricao="O plano que a maioria dos clientes escolhe.",
        preco_mensal=200.0,
        preco_anual=1_920.0,     # 200 * 12 * 0.8
        max_agentes=5,
        max_canais_por_agente=4,
        max_mensagens_por_agente_mes=5_000,
        ordem=2,
        destaque=True,
    ),
    Plano(
        id=PLANO_NEGOCIO,
        nome="Negócio",
        descricao="Vários agentes, vários canais, volume de atendimento real.",
        preco_mensal=500.0,
        preco_anual=4_800.0,     # 500 * 12 * 0.8
        max_agentes=15,
        max_canais_por_agente=6,
        max_mensagens_por_agente_mes=25_000,
        ordem=3,
    ),
    Plano(
        id=PLANO_ESPECIALISTA,
        nome="Especialista",
        descricao="Para operação que conversa com o público o dia inteiro.",
        preco_mensal=1_250.0,
        preco_anual=12_000.0,    # 1250 * 12 * 0.8
        max_agentes=50,
        max_canais_por_agente=10,
        max_mensagens_por_agente_mes=100_000,
        ordem=4,
    ),
)

POR_ID: dict[str, Plano] = {p.id: p for p in CATALOGO}

#: Plano de quem ainda não tem assinatura. Mesmo desenho do teste, mas o
#: período não expira: é o que a conta recebe entre o cadastro e o primeiro
#: pagamento confirmado.
PLANO_PADRAO = PLANO_TESTE


def plano(plano_id: str | None) -> Plano:
    """Plano pelo id, com fallback para o padrão.

    O fallback é deliberado: uma assinatura apontando para um plano que foi
    removido do catálogo não pode derrubar o painel inteiro — cai no teste e o
    cliente vê o que fazer.
    """
    return POR_ID.get(plano_id or "", POR_ID[PLANO_PADRAO])


def maior_plano(a: str, b: str) -> str:
    return a if plano(a).ordem >= plano(b).ordem else b


def pesquisar(termo: str) -> list[Plano]:
    """Busca por id, nome ou descrição — é o que o agente de suporte usa para
    responder "quanto custa o plano que tem 5 canais?"."""
    alvo = (termo or "").strip().lower()
    if not alvo:
        return list(CATALOGO)
    achados = [p for p in CATALOGO
               if alvo in p.id or alvo in p.nome.lower() or alvo in p.descricao.lower()]
    return achados or list(CATALOGO)


# --------------------------------------------------------------------------
# Mensagens além do plano
# --------------------------------------------------------------------------

#: Pacotes de mensagens extras, comprados no fim do mês. O preço cai por
#: unidade conforme o pacote cresce (R$ 12,00 -> R$ 7,50 por mil): é a regra
#: "quanto maior o volume, menor o preço unitário" do item 9, e é o formato
#: que o mercado usa (Typebot $10/500; Chatbase créditos avulsos).
PRECO_POR_MIL_MENSAGEM: tuple[tuple[int, float], ...] = (
    (1_000, 12.00),
    (5_000, 9.00),
    (20_000, 7.50),
    (100_000, 6.00),
)


def custo_mensagens_excedentes(quantidade: int) -> float:
    """Preço de `quantidade` mensagens além do limite, em R$.

    Preenche do pacote mais caro para o mais barato: quem compra 5.000
   essages paga 1.000 a R$ 12 + 4.000 a R$ 9. Sem isso, bastaria comprar o
    pacote grande de uma vez para sempre pagar o preço menor.
    """
    restante = max(int(quantidade), 0)
    if restante <= 0:
        return 0.0
    # A primeira faixa é o mínimo cobrado: quem passou do limite por 1
    # mensagem paga por 1.000. Sem isso o excedente de um dia valeria R$ 0,01.
    primeiro, preco_mil = PRECO_POR_MIL_MENSAGEM[0]
    total = preco_mil
    restante = max(restante - primeiro, 0)
    # Da maior faixa para a menor, uma vez cada: 5.000 excedentes = 1.000 a
    # R$ 12 + 4.000 a R$ 9. Encher só a faixa barata daria desconto infinito.
    for tamanho, preco_mil in PRECO_POR_MIL_MENSAGEM[1:]:
        if restante <= 0:
            break
        cabe = min(restante, tamanho)
        total += cabe / 1_000 * preco_mil
        restante -= cabe
    return round(total, 2)


# --------------------------------------------------------------------------
# Períodos
# --------------------------------------------------------------------------


def _add_meses(quando: dt.datetime, meses: int) -> dt.datetime:
    """Soma meses preservando o dia.

    `relativedelta` não vem no requirements (o projeto instala o mínimo), então
    é feito à mão. Cuidado com 31 de janeiro + 1 mês: vira 28/29 de fevereiro,
    nunca 2 ou 3 de março — o dia overflow "corrigido" mudaria a data de
    cobrança para sempre.
    """
    mes_total = quando.month - 1 + meses
    ano = quando.year + mes_total // 12
    mes = mes_total % 12 + 1
    dia = min(quando.day, calendar.monthrange(ano, mes)[1])
    return quando.replace(year=ano, month=mes, day=dia)


def normalizar_ciclo(ciclo: str | None) -> str:
    """'mensal' ou 'anual', canônico, nunca outra coisa.

    O ciclo decide duas coisas ao mesmo tempo — o preço (`preco_do_ciclo`) e
    quantos meses o cliente recebe (`fim_do_periodo`). Se as duas lessem o
    valor cru, um `Anual` com espaço no banco seria cobrado como mensal e
    entregaria 12 meses: 11 de graça. Por isso as duas normalizam, e o
    desconhecido cai em 'mensal' (a opção que não multiplica período).
    """
    c = str(ciclo or "").strip().lower()
    return c if c in ("mensal", "anual") else "mensal"


def fim_do_periodo(inicio: dt.datetime, ciclo: str) -> dt.datetime:
    """Fim do período pago, a partir do início e do ciclo."""
    if normalizar_ciclo(ciclo) == "anual":
        return _add_meses(inicio, 12)
    return _add_meses(inicio, 1)


def preco_do_ciclo(plano_atual: Plano, ciclo: str) -> float:
    return (
        plano_atual.preco_anual
        if normalizar_ciclo(ciclo) == "anual"
        else plano_atual.preco_mensal
    )


def desconto_anual(plano_atual: Plano) -> float:
    """Quanto o cliente economiza pagando o ano. Para exibir ao lado do preço."""
    if plano_atual.preco_mensal <= 0:
        return 0.0
    return round(plano_atual.preco_mensal * 12 - plano_atual.preco_anual, 2)


# --------------------------------------------------------------------------
# Regra do item 9: a troca só vale no fim do que já foi pago
# --------------------------------------------------------------------------


def pode_trocar_de_plano(
    atual_id: str, novo_id: str, proximo_id: str | None = None
) -> tuple[bool, str]:
    """Troca de plano é sempre permitida, mas o efeito é sempre adiado.

    A segunda volta do reason: o cliente pode pedir downgrade a qualquer hora
    (mudar de ideia é um direito dele) e upgrade a qualquer hora (não segurar
    alguém no plano errado), e os dois só entram no fim do período pago.

    `proximo_id` é a troca já agendada. Sem esta conferência, clicar duas vezes
    no mesmo plano criava dois pagamentos pendentes para o mesmo destino — e,
    com o gateway ligado, duas cobranças.
    """
    if novo_id not in POR_ID:
        return False, f"Plano \"{novo_id}\" não existe."
    if novo_id == atual_id:
        return False, "Você já está neste plano."
    if proximo_id and novo_id == proximo_id:
        return False, "Esta troca já está agendada e entra no fim do período atual."
    if novo_id == PLANO_TESTE:
        return False, "O teste de 7 dias não pode ser contratado; ele é gratuito e automático."
    return True, ""


def resumo_para_usuario(assinatura: dict | None, consumo: dict[int, int] | None = None) -> dict:
    """O JSON que o painel e os agentes internos leem.

    Junta plano,_usage e o que está agendado, para a tela de perfil e para o
    agente de suporte responder "quando o meu plano muda?" sem consultar três
    tabelas diferentes.
    """
    consumo = consumo or {}
    if not assinatura:
        p = POR_ID[PLANO_PADRAO]
        assinatura = {
            "plano_id": p.id, "status": "teste", "ciclo": "mensal",
            "inicio_periodo": None, "fim_periodo": None,
            "plano_proximo": None, "ciclo_proximo": None, "cancela_em": None,
        }
    atual = plano(assinatura.get("plano_id"))
    proximo = plano(assinatura.get("plano_proximo")) if assinatura.get("plano_proximo") else None

    por_agente = []
    for agente_id, usadas in sorted(consumo.items()):
        por_agente.append({
            "agente_id": agente_id,
            "mensagens": usadas,
            "limite": atual.max_mensagens_por_agente_mes,
            "restante": max(atual.max_mensagens_por_agente_mes - usadas, 0),
            "excedeu": usadas > atual.max_mensagens_por_agente_mes,
        })

    return {
        "plano": atual.detalhe(),
        "status": assinatura.get("status", "teste"),
        "ciclo": assinatura.get("ciclo", "mensal"),
        "inicio_periodo": assinatura.get("inicio_periodo"),
        "fim_periodo": assinatura.get("fim_periodo"),
        "em_teste": atual.dias_gratis > 0,
        "dias_restantes": dias_restantes(assinatura.get("fim_periodo")),
        "agentes": {"usados": assinatura.get("agentes_usados", 0), "limite": atual.max_agentes},
        "canais_por_agente": {"limite": atual.max_canais_por_agente},
        "consumo": por_agente,
        "consumo_total": sum(consumo.values()),
        "limite_total": atual.max_agentes * atual.max_mensagens_por_agente_mes,
        "troca_agendada": None if not proximo else {
            "plano": proximo.detalhe(),
            "ciclo": assinatura.get("ciclo_proximo") or "mensal",
            "entra_em": assinatura.get("fim_periodo"),
            "pago": bool(assinatura.get("proximo_pago")),
            "observacao": (
                "Pagamento confirmado: o plano novo entra no fim do período atual."
                if assinatura.get("proximo_pago") else
                "O plano novo só começa depois do fim do período já pago, "
                "e só se o pagamento for confirmado."
            ),
        },
        "cancelamento_agendado": assinatura.get("cancela_em"),
        "economia_anual": desconto_anual(atual),
    }


def dias_restantes(fim: dt.datetime | None, agora: dt.datetime | None = None) -> int | None:
    """Dias até o fim do período pago. None quando não há período."""
    if fim is None:
        return None
    if isinstance(fim, str):
        fim = _parse_iso(fim)
        if fim is None:
            return None
    agora = agora or dt.datetime.now(dt.timezone.utc)
    if fim.tzinfo is None:
        fim = fim.replace(tzinfo=dt.timezone.utc)
    return max((fim - agora).days, 0)


def _parse_iso(texto: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return None


def catalogo_para_json() -> list[dict]:
    """Tabela de preços da home. Só plano pago: mostrar teste na vitrine
    vende trials para quem não vai converter."""
    return [p.detalhe() for p in CATALOGO if p.dias_gratis == 0]

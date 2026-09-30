from __future__ import annotations

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _verdadeiro(valor: str | None) -> bool:
    """'1', 'true', 'sim', 'yes' (sem diferenciar maiúsculas) são verdadeiro.

    Qualquer outra coisa — inclusive string vazia, que é o padrão de um flag de
    segurança — é falso. O padrão desligado é o que protege; por isso não
    existe aqui nenhum caso que "pareça verdadeiro".
    """
    return str(valor or "").strip().lower() in ("1", "true", "sim", "yes")


def _ler_isentas(valor: str | None) -> frozenset[str]:
    """Lista de e-mails isentos, normalizada.

    Aceita vírgula, ponto e vírgula, espaço e quebra de linha como separador.
    Só com `split(",")`, um valor escrito como `a@x.com b@y.com` (separado por
    espaço, o jeito mais natural de digitar dois e-mails) virava UMA entrada,
    `a@x.com b@y.com`, que não bate com nenhum e-mail: a isenção sumia em
    silêncio e a conta do dono passava a ser barrada por cota no meio da
    operação.
    """
    return frozenset(
        e.strip().lower()
        for e in re.split(r"[,\s;]+", str(valor or ""))
        if e.strip()
    )


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    gemini_fallbacks: str = os.getenv(
        "GEMINI_FALLBACKS",
        "gemini-flash-lite-latest,gemini-3.1-flash-lite,gemini-flash-latest,gemini-3.6-flash",
    )
    database_url: str = os.getenv("DATABASE_URL", "")
    base_url: str = os.getenv("BASE_URL", "").rstrip("/")
    evolution_instance_prefix: str = os.getenv("EVOLUTION_INSTANCE_PREFIX", "ag").lower()
    evolution_api_url: str = os.getenv("EVOLUTION_API_URL", "").rstrip("/")
    evolution_api_key: str = os.getenv("EVOLUTION_API_KEY", "")
    # Intervalo do keepalive Evolution, em segundos. Acima de 900s o Render
    # hiberna entre os pings (custo baixo); abaixo disso o serviço fica ligado
    # o mês inteiro e consome as 750h do plano grátis. Os canais OFICIAIS da
    # Meta não usam este valor.
    evolution_keepalive_seg: float = float(os.getenv("EVOLUTION_KEEPALIVE_SEG", "1800"))

    # ------------------------------------------------------------------
    # Heartbeat: mantém o serviço Render acordado 24h
    # ------------------------------------------------------------------
    # O Render hiberna o serviço depois de ~15 min sem tráfego. Isso não
    # custava dinheiro, mas custava a UX: quem abria a site pela primeira vez
    # ficava 30-90s olhando "servidor não respondendo", porque o browser trava
    # ANTES de qualquer HTML chegar — não há JavaScript da página que resolva
    # esse caso.
    #
    # Este heartbeat faz o app dar GET em /health no próprio BASE_URL. É tráfego
    # HTTP de verdade para o Render, então conta como atividade.
    #
    # Custo: 24h por dia = 744h/mês, dentro das 750h do plano grátis. Um único
    # serviço 24h NÃO estoura a cota; dois estoura (1488h). O comentário antigo
    # em EVOLUTION_KEEPALIVE_SEG falava em 720h como se fosse o mês inteiro e
    # tratava 24h como proibido, mas a conta fecha: 744 < 750.
    #
    # IMPORTANTE: isto roda dentro do processo, então só funciona com o serviço
    # acordado. Ele não é o que acorda um serviço adormecido — é o que impede
    # que ele durma depois do primeiro acesso. O wake de verdade para um serviço
    # parado (deploy, crash) é o próprio Render, que sempre restartou.
    #
    # 0 = desliga. Abaixo de 120s é desperdício, então o piso é 120s.
    heartbeat_seg: float = float(os.getenv("HEARTBEAT_SEG", "600"))

    # ------------------------------------------------------------------
    # Supabase Auth (contas de usuário)
    # ------------------------------------------------------------------
    # A URL e a chave *publicável* do projeto. A publicável vai no navegador de
    # propósito (ela authorizes só o sign-in e o sign-up; a service_role nunca
    # sai do servidor). As duas são obrigatórias: sem elas não há contas e o
    # app se recusa a abrir a API, em vez de degradar para um modo aberto.
    supabase_url: str = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_anon_key: str = os.getenv("SUPABASE_ANON_KEY", "")

    # ------------------------------------------------------------------
    # Mercado Pago (cobrança real, PIX)
    # ------------------------------------------------------------------
    # O gateway liga pela presença da chave de acesso: com o PAT configurado
    # as rotas de checkout criam PIX de verdade e o webhook confirma; sem o
    # PAT o app segue funcionando 100% (né o min 0 da home) mas devolve 503
    # no checkout, em vez de "confirmar" pagamento que não aconteceu.
    mercadopago_pat: str = os.getenv("MERCADOPAGO_PAT", "")
    mercadopago_pk: str = os.getenv("MERCADOPAGO_PK", "")
    # Segredo opcional de assinatura do webhook. Sem ele confiamos na
    # consulta reversa à API do MP (fonte da verdade), que é segura; com ele,
    # só aceitamos webhook assinado (anti-falsificação de notificação).
    mercadopago_webhook_secret: str = os.getenv("MERCADOPAGO_WEBHOOK_SECRET", "")

    # E-mails que NUNCA precisam pagar: o dono/operador da plataforma e a
    # conta de teste. Quem está na lista recebe o plano máximo sem checkout —
    # é a garantia de que a operação própria não fica trancada atrás do
    # próprio funil de cobrança. A leitura é feita por `_ler_isentas` (aceita
    # vírgula, ponto e vírgula, espaço e quebra de linha) para que a regra
    # exista uma única vez e possa ser testada sozinha.
    contas_isentas: frozenset[str] = _ler_isentas(
        os.getenv("CONTAS_ISENTAS", "joaogabrielss.2007@gmail.com,jgkwy07@gmail.com")
    )

    # Confirmação de pagamento sem gateway (dev local / teste de interface).
    # Desligado por padrão DE PROPÓSITO: `/api/plano/pagamento/{id}/pagar` é o
    # botão que abre o período, então ligá-lo num ambiente que só esqueceu de
    # cadastrar o PAT do Mercado Pago entregaria plano pago de graça para
    # qualquer conta logada, com um clique. Para ligar, é preciso escrever.
    pagamento_demo: bool = _verdadeiro(os.getenv("PAGAMENTO_DEMO", ""))

    # Token de acesso do painel admin. OPCIONAL e fora do fluxo normal de login:
    # serve só como vidro quebrado de emergência, para recuperar o acesso se o
    # Supabase Auth ficar indisponível ou mal configurado. Vazio = desativado.
    # Nunca confundir com os segredos dos canais, que são guardados por canal
    # em canais.config.
    admin_token: str = os.getenv("ADMIN_TOKEN", "")

    # Canais NÃO oficiais (Evolution/Baileys e instagrapi) criam instâncias
    # numa infraestrutura compartilhada da plataforma e podem banir a conta que
    # os usa. Por isso ficam restritos ao admin. "1" libera para qualquer
    # usuário cadastrado.
    canais_nao_oficiais_para_usuarios: bool = os.getenv(
        "CANAIS_NAO_OFICIAIS_PARA_USUARIOS", ""
    ).strip().lower() in ("1", "true", "sim", "yes")

    # Backoff da fila. O teto é baixo de propósito: uma falha sempre volta para
    # o fim da fila em vez de ser descartada, mas sem lotar o banco de tentativa.
    inbox_backoff_base_seg: int = int(os.getenv("INBOX_BACKOFF_BASE_SEG", "20"))
    inbox_backoff_teto_seg: int = int(os.getenv("INBOX_BACKOFF_TETO_SEG", "300"))
    inbox_lote: int = int(os.getenv("INBOX_LOTE", "8"))

    @property
    def has_evolution(self) -> bool:
        return bool(self.evolution_api_url and self.evolution_api_key)

    @property
    def has_auth(self) -> bool:
        """Contas de usuário prontas. Sem isto a API fica fechada (503) — nunca
        aberta: um 'modo sem login' seria exatamente o que a 0004 removeu."""
        return bool(self.supabase_url and self.supabase_anon_key)

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def has_db(self) -> bool:
        return bool(self.database_url)

    @property
    def has_mercadopago(self) -> bool:
        """Checkout PIX ligado. A chave pública sozinha não cobra nada: o PAT
        é quem autoriza criar o pagamento; a PK é só o identificador público."""
        return bool(self.mercadopago_pat)

    @property
    def supabase_functions_base(self) -> str:
        """Base das Edge Functions (fila sempre ativa) derivada do projeto
        Supabase, usado como webhook público do Telegram/Evolution.
        O app do Render pode dormir à vontade: quem grava na caixa é a função."""
        v = os.getenv("SUPABASE_FUNCTIONS_BASE", "").rstrip("/")
        if v:
            return v
        m = re.search(r"postgres\.([a-z0-9]+)\b", self.database_url or "")
        return f"https://{m.group(1)}.supabase.co/functions/v1/inbox" if m else ""


settings = Settings()
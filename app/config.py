from __future__ import annotations

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


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
    # Supabase Auth (contas de usuário)
    # ------------------------------------------------------------------
    # A URL e a chave *publicável* do projeto. A publicável vai no navegador de
    # propósito (ela authorizes só o sign-in e o sign-up; a service_role nunca
    # sai do servidor). As duas são obrigatórias: sem elas não há contas e o
    # app se recusa a abrir a API, em vez de degradar para um modo aberto.
    supabase_url: str = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_anon_key: str = os.getenv("SUPABASE_ANON_KEY", "")

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
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
    # Token do painel. Vazio = API fechada (fail-closed): nenhum endpoint /api/*
    # responde sem ele. Nunca confundir com os segredos dos canais, que são
    # guardados por canal em canais.config e validados na própria rota.
    admin_token: str = os.getenv("ADMIN_TOKEN", "")

    # Backoff da fila. O teto é baixo de propósito: uma falha sempre volta para
    # o fim da fila em vez de ser descartada, mas sem lotar o banco de tentativa.
    inbox_backoff_base_seg: int = int(os.getenv("INBOX_BACKOFF_BASE_SEG", "20"))
    inbox_backoff_teto_seg: int = int(os.getenv("INBOX_BACKOFF_TETO_SEG", "300"))
    inbox_lote: int = int(os.getenv("INBOX_LOTE", "8"))

    @property
    def has_evolution(self) -> bool:
        return bool(self.evolution_api_url and self.evolution_api_key)

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def has_db(self) -> bool:
        return bool(self.database_url)

    @property
    def has_admin(self) -> bool:
        return bool(self.admin_token)

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
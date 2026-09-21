from __future__ import annotations

import os
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

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def has_db(self) -> bool:
        return bool(self.database_url)


settings = Settings()
"""Aplica sql/schema.sql no banco (Supabase). Uso: python init_db.py"""

import asyncio
import logging
from pathlib import Path

import asyncpg

from app.config import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("init_db")


async def main() -> None:
    schema = Path(__file__).parent / "sql" / "schema.sql"
    conn = await asyncpg.connect(settings.database_url, statement_cache_size=0)
    try:
        await conn.execute(schema.read_text(encoding="utf-8"))
        log.info("Schema aplicado com sucesso.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
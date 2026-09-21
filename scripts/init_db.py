"""Aplica sql/schema.sql no banco (Supabase). Uso: python scripts/init_db.py"""

import asyncio
import logging
import sys
from pathlib import Path

import asyncpg

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app.config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("init_db")


async def main() -> None:
    schema = RAIZ / "sql" / "schema.sql"
    conn = await asyncpg.connect(settings.database_url, statement_cache_size=0)
    try:
        await conn.execute(schema.read_text(encoding="utf-8"))
        log.info("Schema aplicado com sucesso.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
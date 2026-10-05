import os
import ssl

import asyncpg
from config import DATABASE_URL

_pool = None

# Режим SSL для подключения к Postgres:
#   "require"  — SSL без проверки сертификата (исходно под Render Postgres,
#                 который требует TLS).
#   "disable"  — без SSL, для локального Postgres без настроенного TLS.
SSL_MODE = os.environ.get("DB_SSL", "require")


def _build_ssl_context() -> ssl.SSLContext:
    """
    Явный SSLContext вместо строки "require".

    На некоторых сочетаниях версий asyncpg/Python строковый режим
    ssl="require" не всегда надёжно транслируется в реальный TLS-
    хендшейк, из-за чего сервер (Render Postgres и подобные) может
    ответить InvalidAuthorizationSpecificationError: SSL/TLS required,
    хотя код формально просит SSL. Явный SSLContext убирает эту
    неоднозначность — TLS используется гарантированно, но без
    проверки сертификата (аналог поведения sslmode=require в libpq).
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def init_pool():
    global _pool

    _pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=30,
        ssl=_build_ssl_context() if SSL_MODE == "require" else False,
    )


async def close_pool():
    global _pool

    if _pool:
        await _pool.close()


def get_pool():
    return _pool

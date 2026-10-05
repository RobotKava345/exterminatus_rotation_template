import logging

from database.pool import get_pool

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

async def init_db():
    """
    Создаёт недостающие таблицы. Не удаляет существующие данные.

    ВАЖНО: перед вызовом должен быть инициализирован пул
    (database.pool.init_pool()).
    """

    pool = get_pool()

    async with pool.acquire() as db:
        # ====================================================
        # SEEN USERS (без изменений от оригинала)
        # ====================================================
        await db.execute("""
            CREATE TABLE IF NOT EXISTS seen_users (
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                first_seen TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_seen_users_chat ON seen_users(chat_id)")

        # ====================================================
        # BOT BURNS — журнал ротации: когда и какой бот из пула
        # был "сожжён" (выкинут/лишён прав) Telegram'ом в каком
        # чате. Чисто диагностика — помогает понять, насколько
        # близко к порогу был прогон, и сколько ботов реально
        # потребовалось.
        # ====================================================
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_burns (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                bot_user_id BIGINT,
                bot_username TEXT,
                burned_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ====================================================
        # OPERATIONS — состояние последнего/текущего запуска
        # /fire_exterminatus по чату. Пишется периодически по
        # ходу операции, поэтому если процесс упадёт посреди
        # работы — в БД останется последний известный прогресс
        # (видно через /laststatus), а не только то, что было в
        # оперативной памяти упавшего процесса.
        # ====================================================
        await db.execute("""
            CREATE TABLE IF NOT EXISTS operations (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                total INT DEFAULT 0,
                processed INT DEFAULT 0,
                banned INT DEFAULT 0,
                skipped INT DEFAULT 0,
                errors INT DEFAULT 0,
                bots_used INT DEFAULT 1,
                started_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMPTZ
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_operations_chat ON operations(chat_id)")

    logger.info("Структура базы данных инициализирована (Postgres)")


# ============================================================
# SEEN USERS
# ============================================================

async def add_user(chat_id: int, user_id: int):
    pool = get_pool()
    try:
        await pool.execute(
            """
            INSERT INTO seen_users (chat_id, user_id)
            VALUES ($1, $2)
            ON CONFLICT (chat_id, user_id) DO NOTHING
            """,
            chat_id, user_id,
        )
    except Exception as e:
        logger.error(f"Ошибка записи пользователя {user_id} в чат {chat_id}: {e}")


async def get_seen_users(chat_id: int) -> list[int]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT user_id FROM seen_users WHERE chat_id = $1",
        chat_id,
    )
    return [row["user_id"] for row in rows]


async def count_seen_users(chat_id: int) -> int:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM seen_users WHERE chat_id = $1",
        chat_id,
    )
    return row["cnt"] if row else 0


# ============================================================
# BOT BURNS
# ============================================================

async def log_bot_burn(chat_id: int, bot_user_id: int | None, bot_username: str | None):
    pool = get_pool()
    try:
        await pool.execute(
            """
            INSERT INTO bot_burns (chat_id, bot_user_id, bot_username)
            VALUES ($1, $2, $3)
            """,
            chat_id, bot_user_id, bot_username,
        )
    except Exception as e:
        logger.error(f"Ошибка записи burn-лога для @{bot_username}: {e}")


async def count_bot_burns(chat_id: int) -> int:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM bot_burns WHERE chat_id = $1",
        chat_id,
    )
    return row["cnt"] if row else 0


# ============================================================
# OPERATIONS
# ============================================================

async def start_operation(chat_id: int, total: int) -> int:
    """Создаёт запись операции, возвращает её id."""
    pool = get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO operations (chat_id, status, total)
        VALUES ($1, 'running', $2)
        RETURNING id
        """,
        chat_id, total,
    )
    return row["id"]


async def update_operation(operation_id: int, **fields):
    """
    Частичное обновление счётчиков операции.
    Пример: update_operation(op_id, processed=10, banned=8, bots_used=2)
    """
    if not fields:
        return

    pool = get_pool()
    set_clauses = []
    values = []

    for i, (key, value) in enumerate(fields.items(), start=1):
        set_clauses.append(f"{key} = ${i}")
        values.append(value)

    values.append(operation_id)

    query = (
        f"UPDATE operations SET {', '.join(set_clauses)}, "
        f"updated_at = CURRENT_TIMESTAMP WHERE id = ${len(values)}"
    )

    try:
        await pool.execute(query, *values)
    except Exception as e:
        logger.error(f"Ошибка обновления operation id={operation_id}: {e}")


async def finish_operation(operation_id: int, status: str):
    pool = get_pool()
    try:
        await pool.execute(
            """
            UPDATE operations
            SET status = $2, finished_at = CURRENT_TIMESTAMP
            WHERE id = $1
            """,
            operation_id, status,
        )
    except Exception as e:
        logger.error(f"Ошибка завершения operation id={operation_id}: {e}")


async def get_last_operation(chat_id: int):
    pool = get_pool()
    return await pool.fetchrow(
        """
        SELECT * FROM operations
        WHERE chat_id = $1
        ORDER BY started_at DESC
        LIMIT 1
        """,
        chat_id,
    )

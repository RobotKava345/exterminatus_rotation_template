import asyncio
import logging

from aiogram import Dispatcher
from aiogram.enums import ParseMode

from config import BOT_TOKENS
from database.pool import init_pool, close_pool
from database.db import init_db

from mtproto import client as telethon_client
from bot_pool import BotPool

from handlers import ext, stats, tracking

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ============================================================
# ПУЛ БОТОВ (новый принцип работы)
# ============================================================
# Раньше здесь был один Bot(token=BOT_TOKEN). Теперь — пул из
# N ботов (config.BOT_TOKENS, см. .env). Только ПЕРВЫЙ (primary)
# держит long polling и отвечает за весь UI: команды, трекинг,
# статистику, редактирование статус-сообщений. Остальные боты
# пула не делают getUpdates вообще — они существуют только для
# того, чтобы ext.py мог переключаться на них при ротации.
pool = BotPool(BOT_TOKENS, parse_mode=ParseMode.HTML)

dp = Dispatcher()

# Порядок подключения важен: команды (ext, stats) должны быть выше
# tracking, чтобы отслеживание сообщений не перехватывало апдейт
# раньше нужного хендлера команды.
dp.include_router(ext.router)
dp.include_router(stats.router)
dp.include_router(tracking.router)


async def main():
    # 1. Пул соединений к Postgres
    await init_pool()
    logger.info("Пул соединений к Postgres создан")

    # 2. Структура таблиц
    await init_db()

    # 3. Telethon-клиент — теперь нужен не только для чтения веток
    #    и /sync, но и для promote_bot_admin при ротации ботов.
    await telethon_client.start()
    logger.info("Telethon-клиент подключён")

    # 4. Пул ботов — подтягиваем get_me() для каждого токена.
    await pool.warmup()

    valid_bots = [pb for pb in pool.all() if pb.user_id is not None]
    if len(valid_bots) < len(pool.all()):
        logger.warning(
            "%d из %d токенов в пуле не прошли warmup (невалидны?) — "
            "ротация на них работать не будет",
            len(pool.all()) - len(valid_bots),
            len(pool.all()),
        )

    logger.info(
        "Пул ботов готов: %d шт. Primary=@%s",
        len(pool.all()),
        pool.primary_info.username,
    )

    logger.info("Админ-бот запущен (режим ротации)")

    primary = pool.primary

    try:
        await primary.delete_webhook(drop_pending_updates=True)

        # pool=pool — прокидывается аiogram'ом во все хендлеры как
        # именованный параметр (см. handlers/ext.py, handlers/stats.py).
        await dp.start_polling(
            primary,
            allowed_updates=["message", "chat_member", "callback_query"],
            pool=pool,
        )
    finally:
        await telethon_client.disconnect()
        await pool.close_all()
        await close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")

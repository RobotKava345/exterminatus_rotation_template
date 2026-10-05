import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# ПУЛ ТОКЕНОВ
# ============================================================
# Новый принцип работы: вместо одного BOT_TOKEN — список из
# нескольких (рекомендуется 3-4). Telegram выбрасывает/режет
# права конкретному боту-аккаунту после определённого порога
# подряд идущих банов (эмпирически — около 70-80 в супергруппе).
# Порог висит на BOT_TOKEN, а не на процессе — поэтому решает
# только смена аккаунта бота, а не разбиение кода на процессы.
#
# BOT_TOKENS=токен1,токен2,токен3,токен4
# Для обратной совместимости также читается старый BOT_TOKEN
# (как единственный токен в пуле, если BOT_TOKENS не задан).
_raw_tokens = os.environ.get("BOT_TOKENS") or os.environ.get("BOT_TOKEN", "")
BOT_TOKENS = [t.strip() for t in _raw_tokens.split(",") if t.strip()]

# Публичный HTTPS-адрес мини-аппа (ngrok на этапе разработки,
# домен хостинга — в проде). Нужен для web_app-кнопки в ЛС.
MINIAPP_BASE_URL = os.environ.get("MINIAPP_BASE_URL", "")

# Строка подключения к Postgres (Render External Database URL).
# Формат: postgresql://user:password@host/dbname
DATABASE_URL = os.environ.get("DATABASE_URL")

# Userbot-аккаунт (Telethon) — используется не только для чтения
# форумных веток, но теперь и для ДИНАМИЧЕСКОЙ выдачи прав
# следующему боту из пула при ротации (mtproto.promote_bot_admin).
TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH")

if not BOT_TOKENS:
    raise RuntimeError(
        "BOT_TOKENS не найден. Создай файл .env рядом с bot.py "
        "и укажи в нём строку вида:\n"
        "BOT_TOKENS=токен1,токен2,токен3,токен4\n"
        "(можно указать и один токен — тогда ротация просто "
        "никогда не будет срабатывать, поведение как раньше)"
    )

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL не найден. Добавь в .env строку вида:\n"
        "DATABASE_URL=postgresql://user:password@host/dbname"
    )

if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
    raise RuntimeError(
        "TELEGRAM_API_ID / TELEGRAM_API_HASH не найдены в .env.\n"
        "Они обязательны в новой схеме — userbot-аккаунт не только "
        "читает ветки, но и выдаёт права следующему боту из пула "
        "при ротации."
    )

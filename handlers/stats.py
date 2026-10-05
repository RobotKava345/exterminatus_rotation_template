from aiogram import Router, types

from aiogram.filters import Command

from database.db import count_seen_users, get_last_operation
from mtproto import sync_chat_members
from bot_pool import BotPool

router = Router()


@router.message(Command("stats"))
async def cmd_stats(message: types.Message, pool: BotPool):
    # Статистика доступна любому участнику чата.

    tracked_count = await count_seen_users(message.chat.id)

    try:
        real_count = await pool.primary.get_chat_member_count(message.chat.id)
    except Exception:
        real_count = None

    lines = []

    if real_count is not None:
        lines.append(
            f"Всего участников чата (по данным Telegram): "
            f"<b>{real_count}</b>"
        )

    lines.append(f"Отслежено в базе: <b>{tracked_count}</b>")

    if real_count is not None and real_count > tracked_count:
        lines.append(
            "\n<i>Есть расхождение — выполни /sync, чтобы подтянуть "
            "полный список участников через Telethon.</i>"
        )

    await message.reply("\n".join(lines))


@router.message(Command("sync"))
async def cmd_sync(message: types.Message):
    # Синхронизация доступна любому участнику чата.

    status_message = await message.reply(
        "Синхронизация участников через Telethon..."
    )

    try:
        count = await sync_chat_members(message.chat.id)
    except Exception as e:
        return await status_message.edit_text(
            f"Ошибка синхронизации: {e}"
        )

    await status_message.edit_text(
        f"Синхронизация завершена. Обработано участников: <b>{count}</b>"
    )


@router.message(Command("pool"))
async def cmd_pool(message: types.Message, pool: BotPool):
    """
    Новая команда: показывает состояние пула ботов — сколько их
    всего, кто активен сейчас, кто уже сожжён в прошлых прогонах.
    """

    lines = ["<b>ПУЛ БОТОВ</b>\n"]

    for i, pb in enumerate(pool.all()):
        marker = "👉" if i == pool.index else ("💀" if pb.burned else "✅")
        lines.append(f"{marker} @{pb.username or '???'}")

    await message.reply("\n".join(lines))


@router.message(Command("laststatus"))
async def cmd_laststatus(message: types.Message):
    """
    Новая команда: показывает последнюю операцию /fire_exterminatus
    по этому чату из БД — полезно, если процесс перезапустили и
    хочется узнать, чем закончился прошлый прогон.
    """

    row = await get_last_operation(message.chat.id)

    if row is None:
        return await message.reply("По этому чату ещё не было ни одной операции.")

    await message.reply(
        "<b>ПОСЛЕДНЯЯ ОПЕРАЦИЯ</b>\n\n"
        f"Статус: <b>{row['status']}</b>\n"
        f"Всего в реестре: <b>{row['total']}</b>\n"
        f"Обработано: <b>{row['processed']}</b>\n"
        f"Забанено: <b>{row['banned']}</b>\n"
        f"Пропущено: <b>{row['skipped']}</b>\n"
        f"Ошибок: <b>{row['errors']}</b>\n"
        f"Ботов использовано: <b>{row['bots_used']}</b>\n"
        f"Начато: {row['started_at']}\n"
        f"Обновлено: {row['updated_at']}"
    )

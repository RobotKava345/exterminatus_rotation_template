import logging
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.channels import EditAdminRequest, InviteToChannelRequest
from telethon.tl.types import ChatAdminRights
from telethon.errors import UserAlreadyParticipantError, ChatAdminRequiredError

from database.db import add_user, get_seen_users


load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")

logger = logging.getLogger(__name__)

client = TelegramClient(
    "telegram_session",
    API_ID,
    API_HASH,
)


# Чаты, которые /sync умеет обходить пачкой (sync_all_chats).
# Ключ — chat_id (число), значение — просто человекочитаемое имя
# для логов. Отредактируй под свои чаты.
SYNC_CHATS = {
    # -1001234567890: "Мой чат",
}


# ============================================================
# РОТАЦИЯ ПРАВ БОТОВ (новый принцип работы)
# ============================================================
#
# Раньше бот должен был быть заранее вручную назначен админом с
# правами "Ban users" + "Manage topics" до запуска /fire_exterminatus.
#
# Теперь это делает userbot-аккаунт ДИНАМИЧЕСКИ: прямо перед тем,
# как очередной бот из пула (config.BOT_TOKENS) начинает банить,
# userbot сам выдаёт ему нужные права через EditAdminRequest. Когда
# Telegram выкидывает/режет текущего бота — его права неактуальны
# сами по себе (аккаунт уже не в чате), и userbot просто промоутит
# СЛЕДУЮЩИЙ бот из пула — без участия человека.
#
# ВАЖНО: сам userbot-аккаунт должен быть в чате полноценным админом
# с правом "Add New Admins" (add_admins) — иначе EditAdminRequest
# откажет с ChatAdminRequiredError.

BOT_ADMIN_RIGHTS = ChatAdminRights(
    ban_users=True,
    delete_messages=True,
    manage_topics=True,
    invite_users=True,
    other=True,
)


async def ensure_bot_in_chat(chat_id: int, bot_username: str):
    """
    Добавляет бота в чат через userbot, если его там ещё нет.

    Тихо проглатывает "уже участник" — это ожидаемый штатный
    случай, если бот был добавлен заранее (рекомендуется делать
    это для всех ботов пула один раз на этапе настройки, чтобы
    ротация в разгар операции не теряла время на инвайт).
    """

    chat = await client.get_entity(chat_id)

    try:
        await client(InviteToChannelRequest(chat, [bot_username]))

        logger.info(
            "Бот @%s добавлен в чат %s",
            bot_username,
            chat_id,
        )

    except UserAlreadyParticipantError:
        logger.info(
            "Бот @%s уже состоит в чате %s",
            bot_username,
            chat_id,
        )

    except Exception as e:
        logger.warning(
            "Не удалось добавить @%s в чат %s: %s "
            "(продолжаю — возможно, бот уже там)",
            bot_username,
            chat_id,
            e,
        )


async def promote_bot_admin(
    chat_id: int,
    bot_user_id: int,
    bot_username: str | None = None,
) -> bool:
    """
    Выдаёт боту (bot_user_id) права администратора (бан, управление
    темами) в чате chat_id через userbot-аккаунт.

    Если бот ещё не состоит в чате — сначала пробует добавить его
    (нужен bot_username). Возвращает True при успехе, False — если
    права выдать не удалось (например, у самого userbot недостаточно
    прав — тогда ротация бесполезна и операцию нужно останавливать).
    """

    chat = await client.get_entity(chat_id)

    if bot_username:
        await ensure_bot_in_chat(chat_id, bot_username)

    try:
        await client(
            EditAdminRequest(
                channel=chat,
                user_id=bot_user_id,
                admin_rights=BOT_ADMIN_RIGHTS,
                rank="exterminatus",
            )
        )

        logger.info(
            "Бот user_id=%s (@%s) промоутнут в админы чата %s",
            bot_user_id,
            bot_username,
            chat_id,
        )

        return True

    except ChatAdminRequiredError:
        logger.error(
            "У userbot-аккаунта недостаточно прав, чтобы назначать "
            "админов в чате %s (нужно право 'Add New Admins'). "
            "Ротация ботов невозможна без этого.",
            chat_id,
        )
        return False

    except Exception as e:
        logger.exception(
            "Не удалось выдать права боту user_id=%s (@%s) в чате %s: %s",
            bot_user_id,
            bot_username,
            chat_id,
            e,
        )
        return False


async def demote_bot(chat_id: int, bot_user_id: int):
    """
    Снимает права администратора у бота (best-effort, не критично).

    Используется после того, как бот уже "сожжён" и вручную его
    зачищать от админки необязательно (Telegram и так мог уже сам
    его выкинуть) — но если он просто лишился части прав, а не
    был удалён из чата целиком, аккуратнее явно снять с него админку,
    чтобы не оставлять мёртвый админ-флаг.
    """

    chat = await client.get_entity(chat_id)

    try:
        await client(
            EditAdminRequest(
                channel=chat,
                user_id=bot_user_id,
                admin_rights=ChatAdminRights(),
                rank="",
            )
        )
    except Exception as e:
        logger.debug(
            "Не удалось снять права с бота user_id=%s в чате %s "
            "(скорее всего он уже вне чата): %s",
            bot_user_id,
            chat_id,
            e,
        )


# ============================================================
# СИНХРОНИЗАЦИЯ УЧАСТНИКОВ (без изменений от оригинала)
# ============================================================

async def sync_chat_members(chat_id: int) -> int:
    """
    Синхронизирует участников указанного Telegram-чата
    с таблицей seen_users.

    Возвращает количество обработанных пользователей.

    После синхронизации отдельно проверяет через БД,
    какие из полученных user_id действительно присутствуют
    в seen_users, и выводит результат в консоль/лог.
    """

    chat = await client.get_entity(chat_id)

    title = getattr(chat, "title", str(chat_id))

    logger.info(
        "Начинается синхронизация чата '%s' (%s)",
        title,
        chat_id,
    )

    count = 0
    synced_user_ids = []

    async for user in client.iter_participants(chat):
        if user.bot:
            continue

        user_id = user.id
        synced_user_ids.append(user_id)

        await add_user(
            chat_id=chat_id,
            user_id=user_id,
        )

        count += 1

    logger.info(
        "Проверка наличия синхронизированных пользователей в БД..."
    )

    db_user_ids = set(await get_seen_users(chat_id))
    synced_user_ids_set = set(synced_user_ids)

    found_user_ids = synced_user_ids_set & db_user_ids
    missing_user_ids = synced_user_ids_set - db_user_ids

    logger.info(
        "ПРОВЕРКА БД для '%s' (%s):",
        title,
        chat_id,
    )

    logger.info("  Получено через Telethon: %d", len(synced_user_ids_set))
    logger.info("  Найдено в БД: %d", len(found_user_ids))
    logger.info("  НЕ найдено в БД: %d", len(missing_user_ids))

    if missing_user_ids:
        logger.warning(
            "User ID отсутствуют в БД: %s",
            sorted(missing_user_ids),
        )
    else:
        logger.info("✅ Все синхронизированные пользователи найдены в БД.")

    logger.info(
        "Синхронизация '%s' завершена. Обработано: %d",
        title,
        count,
    )

    return count


async def sync_all_chats():
    """
    Синхронизирует все чаты из SYNC_CHATS.
    """

    for chat_id, title in SYNC_CHATS.items():
        try:
            await sync_chat_members(chat_id)

        except Exception:
            logger.exception(
                "Ошибка синхронизации чата '%s' (%s)",
                title,
                chat_id,
            )

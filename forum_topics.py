import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from telethon import functions, types

from mtproto import client
from utils import AdaptiveThrottle, is_bot_ejected_error


logger = logging.getLogger(__name__)

# Telegram General topic
GENERAL_TOPIC_ID = 1

# ============================================================
# ПАРАМЕТРЫ ЗАМЕНЯЮЩЕЙ ВЕТКИ "АВЕ ИМП"
# ============================================================

REPLACEMENT_TOPIC_NAME = "АВЕ ИМП"

MAX_REPLACEMENT_TOPICS = 10

REPLACEMENT_TOPIC_TEXT = (
    "<b>АВЕ ИМПЕРАТОР</b>\n\n"
    "<i>«Там, где была ересь — воцарился порядок.»</i>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "Сектор очищен. Порча Хаоса выжжена до основания "
    "силой и волей Инквизиции.\n\n"
    "Ни один культ, ни одна ересь, ни один изменник "
    "не укроются от справедливости Империума — "
    "рано или поздно возмездие настигнет всех.\n\n"
    "Из пепла старого порядка возводится новый — "
    "чистый, дисциплинированный, верный только "
    "одному знамени.\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>Император защищает.</b>\n"
    "<b>Слава Империуму Человечества.</b>"
)


class BotEjectedError(Exception):
    """
    Сигнал "этот конкретный bot-аккаунт больше не дееспособен в
    чате" (выкинут/лишён прав) — отдельно от прочих ошибок,
    чтобы вызывающий код (handlers/ext.py) мог среагировать
    ротацией на следующий бот из пула, а не просто посчитать это
    обычной ошибкой операции.
    """
    pass


async def create_replacement_topic(
    bot: Bot,
    chat_id: int,
):
    """
    Создаёт новую форумную ветку "АВЕ ИМП" на месте удалённой
    и публикует в ней заявление об ответственности.

    Возвращает True при успехе, False при неустранимой (но не
    ejected-типа) ошибке. TelegramRetryAfter пробрасывается наверх.
    BotEjectedError — тоже пробрасывается наверх, вызывающий код
    должен выполнить ротацию и повторить операцию новым ботом.
    """

    try:
        new_topic = await bot.create_forum_topic(
            chat_id=chat_id,
            name=REPLACEMENT_TOPIC_NAME,
        )

        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=new_topic.message_thread_id,
            text=REPLACEMENT_TOPIC_TEXT,
        )

        logger.info(
            "Создана заменяющая ветка '%s' (ID=%s) в чате %s",
            REPLACEMENT_TOPIC_NAME,
            new_topic.message_thread_id,
            chat_id,
        )

        return True

    except TelegramRetryAfter:
        raise

    except (TelegramForbiddenError, TelegramBadRequest) as e:
        if is_bot_ejected_error(e):
            raise BotEjectedError(str(e)) from e

        logger.exception(
            "Не удалось создать заменяющую ветку в чате %s: %s",
            chat_id,
            e,
        )
        return False

    except Exception as e:
        if is_bot_ejected_error(e):
            raise BotEjectedError(str(e)) from e

        logger.exception(
            "Не удалось создать заменяющую ветку в чате %s: %s",
            chat_id,
            e,
        )
        return False


async def find_chat(chat_id: int):
    """
    Находит чат через уже авторизованный Telethon-клиент.

    Используется существующая Telethon-сессия из mtproto.py.
    Новый TelegramClient здесь не создаётся.
    """

    async for dialog in client.iter_dialogs():
        if dialog.id == chat_id:
            logger.info(
                "Telethon нашёл чат '%s' (%s)",
                dialog.name,
                chat_id,
            )

            return dialog.entity

    raise ValueError(
        f"Чат {chat_id} не найден среди диалогов Telethon"
    )


async def get_forum_topics(chat_id: int):
    """
    Получает список форумных веток через MTProto (не зависит от
    текущего активного bot-аккаунта, поэтому не участвует в
    ротации — userbot-сессия не подвержена тому же порогу банов).
    """

    chat = await find_chat(chat_id)

    result = await client(
        functions.messages.GetForumTopicsRequest(
            peer=chat,
            q="",
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=100,
        )
    )

    topics = [
        topic
        for topic in result.topics
        if isinstance(topic, types.ForumTopic)
    ]

    logger.info(
        "Получено форумных веток для '%s': %d",
        chat_id,
        len(topics),
    )

    return topics


async def get_topic_info(chat_id: int):
    """
    Возвращает информацию о форумных ветках.
    """

    topics = await get_forum_topics(chat_id)

    return [
        {
            "id": topic.id,
            "title": topic.title,
            "closed": topic.closed,
            "hidden": topic.hidden,
        }
        for topic in topics
    ]


async def delete_all_topics_except(
    bot: Bot,
    chat_id: int,
    keep_topic_id: int | None = None,
):
    """
    Удаляет все форумные ветки, кроме указанной, используя права
    переданного bot-аккаунта (manage_topics).

    General (ID=1) всегда сохраняется.

    Возвращает: total_topics, deleted, errors

    Бросает BotEjectedError, если по ходу выяснилось, что bot
    больше не дееспособен в чате — вызывающий код должен выполнить
    ротацию на следующий бот пула и вызвать функцию заново с новым
    bot (уже удалённые на этом проходе ветки просто не попадут в
    список снова при повторном get_forum_topics).
    """

    topics = await get_forum_topics(chat_id)

    total_topics = len(topics)
    deleted = 0
    errors = 0

    throttle = AdaptiveThrottle(base_delay=1.0, max_delay=6.0)

    logger.info(
        "Начинается удаление веток в '%s'. Найдено веток: %d",
        chat_id,
        total_topics,
    )

    for topic in topics:
        topic_id = topic.id
        topic_title = topic.title

        if topic_id == GENERAL_TOPIC_ID:
            logger.info("General сохранён: ID=%s", topic_id)
            continue

        if keep_topic_id is not None and topic_id == keep_topic_id:
            logger.info(
                "Текущая ветка сохранена: '%s' (ID=%s)",
                topic_title,
                topic_id,
            )
            continue

        attempts = 0
        success = False

        while attempts < 2 and not success:
            try:
                await bot.delete_forum_topic(
                    chat_id=chat_id,
                    message_thread_id=topic_id,
                )

                deleted += 1
                success = True

                logger.info(
                    "Ветка удалена: '%s' (ID=%s)",
                    topic_title,
                    topic_id,
                )

            except TelegramRetryAfter as e:
                logger.warning(
                    "FloodWait %s секунд при удалении '%s' (ID=%s)",
                    e.retry_after,
                    topic_title,
                    topic_id,
                )

                throttle.on_flood_wait(e.retry_after)
                await asyncio.sleep(e.retry_after)
                attempts += 1

            except (TelegramForbiddenError, TelegramBadRequest) as e:
                if is_bot_ejected_error(e):
                    logger.error(
                        "Бот лишён возможности удалять ветки в "
                        "чате %s — похоже, выкинут/лишён прав: %s",
                        chat_id,
                        e,
                    )
                    raise BotEjectedError(str(e)) from e

                errors += 1

                logger.error(
                    "Telegram отклонил удаление '%s' (ID=%s): %s",
                    topic_title,
                    topic_id,
                    e,
                )
                break

            except Exception as e:
                if is_bot_ejected_error(e):
                    raise BotEjectedError(str(e)) from e

                errors += 1

                logger.exception(
                    "Ошибка удаления '%s' (ID=%s): %s",
                    topic_title,
                    topic_id,
                    e,
                )
                break

        if not success and attempts >= 2:
            errors += 1

        if success:
            throttle.on_success()

        await throttle.wait()

    logger.info(
        "Удаление веток завершено: найдено=%d, удалено=%d, ошибок=%d",
        total_topics,
        deleted,
        errors,
    )

    return total_topics, deleted, errors


async def create_replacement_topics(
    bot: Bot,
    chat_id: int,
    count: int,
):
    """
    Создаёт заменяющие ветки "АВЕ ИМП".

    Возвращает: created, errors
    Бросает BotEjectedError по тем же правилам, что и
    delete_all_topics_except.
    """

    target = min(count, MAX_REPLACEMENT_TOPICS)

    created = 0
    errors = 0

    throttle = AdaptiveThrottle(base_delay=1.0, max_delay=6.0)

    logger.info(
        "Начинается создание %d заменяющих веток в '%s' (удалено веток: %d)",
        target,
        chat_id,
        count,
    )

    for _ in range(target):
        attempts = 0
        success = False

        while attempts < 2 and not success:
            try:
                success = await create_replacement_topic(bot=bot, chat_id=chat_id)
                if not success:
                    break

            except TelegramRetryAfter as e:
                logger.warning(
                    "FloodWait %s секунд при создании заменяющей ветки",
                    e.retry_after,
                )

                throttle.on_flood_wait(e.retry_after)
                await asyncio.sleep(e.retry_after)
                attempts += 1

        if success:
            created += 1
            throttle.on_success()
        else:
            errors += 1

        await throttle.wait()

    logger.info(
        "Создание заменяющих веток завершено: создано=%d, ошибок=%d",
        created,
        errors,
    )

    return created, errors

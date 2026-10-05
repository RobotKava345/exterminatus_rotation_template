import asyncio
import logging

from aiogram import Router, types
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramForbiddenError,
    TelegramBadRequest,
)

from utils import get_admin_ids, AdaptiveThrottle, is_bot_ejected_error
from database.db import (
    get_seen_users,
    log_bot_burn,
    start_operation,
    update_operation,
    finish_operation,
)
from forum_topics import (
    delete_all_topics_except,
    create_replacement_topics,
    BotEjectedError,
)
from mtproto import promote_bot_admin
from bot_pool import BotPool


logger = logging.getLogger(__name__)
router = Router()

# Партия "для статуса/пауз" — поведение то же, что в оригинале:
# не долбить одним сплошным проходом, а перемежать его с другими
# действиями (ветки) между партиями.
BAN_BATCH_SIZE = 50

# Порог подряд идущих ОБЫЧНЫХ (не ejected) неудач — это НЕ про
# превышение лимита банов конкретным ботом (на это реагирует
# ротация пула), а про что-то другое пошедшее не так (сетевые
# сбои, невалидные user_id и т.п.), где долбить дальше бессмысленно.
MAX_CONSECUTIVE_FAILURES = 5

# Пауза после ротации — чтобы права, выданные userbot'ом через
# Telethon, гарантированно успели примениться на стороне Telegram
# до первого вызова ban_chat_member новым ботом.
ROTATION_SETTLE_DELAY = 2.5


class BanCircuitBreaker:
    """Отслеживает подряд идущие ОБЫЧНЫЕ (не ejected) неудачи банов."""

    def __init__(self, limit: int = MAX_CONSECUTIVE_FAILURES):
        self.limit = limit
        self.consecutive_failures = 0
        self.tripped = False

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self) -> bool:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.limit:
            self.tripped = True
        return self.tripped


async def _ban_single_user(bot, chat_id: int, user_id: int, throttle: AdaptiveThrottle) -> str:
    """
    Пытается забанить одного пользователя переданным bot-аккаунтом.

    Возвращает "banned", "ejected" или "error".

    "ejected" — отдельная категория: это не ошибка конкретного
    бана, а сигнал, что ЭТОТ bot-аккаунт больше не дееспособен в
    чате (выкинут/лишён прав). Вызывающий код должен выполнить
    ротацию, а не пробовать банить этим же ботом дальше.
    """

    attempts = 0

    while attempts < 2:
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)

            logger.info("Пользователь %s исключён из чата %s", user_id, chat_id)
            return "banned"

        except TelegramRetryAfter as e:
            wait = e.retry_after

            logger.warning("FloodWait %s секунд на user_id=%s", wait, user_id)

            throttle.on_flood_wait(wait)
            await asyncio.sleep(wait)
            attempts += 1

        except (TelegramForbiddenError, TelegramBadRequest) as e:
            if is_bot_ejected_error(e):
                logger.error(
                    "Бот похоже выкинут/лишён прав в чате %s: %s",
                    chat_id,
                    e,
                )
                return "ejected"

            logger.warning("Не удалось исключить user_id=%s: %s", user_id, e)
            return "error"

        except Exception as e:
            if is_bot_ejected_error(e):
                return "ejected"

            logger.exception("Ошибка бана пользователя %s: %s", user_id, e)
            return "error"

    logger.warning(
        "user_id=%s: обе попытки бана закончились FloodWait подряд",
        user_id,
    )
    return "error"


async def _rotate_pool(pool: BotPool, chat_id: int) -> bool:
    """
    Выполняет ротацию пула: помечает текущего бота burned,
    переключается на следующего и выдаёт ему права через userbot.

    Возвращает True, если ротация удалась и можно продолжать
    операцию новым активным ботом (pool.current()). False — если
    пул исчерпан ИЛИ userbot не смог выдать права (тогда дальше
    пробовать бессмысленно).
    """

    burned = pool.current()

    try:
        await log_bot_burn(chat_id, burned.user_id, burned.username)
    except Exception:
        logger.debug("Не удалось записать burn-лог (не критично)", exc_info=True)

    next_pb = pool.advance()

    if next_pb is None:
        logger.error(
            "Пул ботов полностью исчерпан в чате %s — операция "
            "не может продолжаться",
            chat_id,
        )
        return False

    ok = await promote_bot_admin(chat_id, next_pb.user_id, next_pb.username)

    if not ok:
        logger.error(
            "Не удалось выдать права следующему боту @%s — "
            "userbot, возможно, сам потерял права 'Add New Admins'",
            next_pb.username,
        )
        return False

    await asyncio.sleep(ROTATION_SETTLE_DELAY)
    return True


async def _ban_batch(
    pool: BotPool,
    chat_id: int,
    users_batch: list,
    admin_ids: set,
    throttle: AdaptiveThrottle,
    breaker: BanCircuitBreaker,
    counters: dict,
    status_msg: types.Message,
    total: int,
    status_update_every: int,
    operation_id: int,
) -> bool:
    """
    Банит одну партию пользователей активным ботом из pool,
    выполняя ротацию на следующий бот при ejected-сигнале.

    Возвращает True, если операцию нужно прервать досрочно
    (пул исчерпан или сработал circuit breaker по обычным ошибкам).
    """

    async def _update_progress():
        try:
            await status_msg.edit_text(
                "<b>ORDO INQUISITIONIS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "<b>EXTERMINATUS</b>\n\n"
                "Статус: <b>ВЫПОЛНЕНИЕ</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Обработано: <b>{counters['processed']}/{total}</b>\n"
                f"Уничтожено: <b>{counters['banned']}</b>\n"
                f"Пропущено (админы): <b>{counters['skipped']}</b>\n"
                f"Ошибок: <b>{counters['errors']}</b>\n"
                f"Активный бот: <b>@{pool.current().username}</b> "
                f"({pool.burned_count()}/{len(pool.all())} сожжено)\n\n"
                "<i>«Огонь не гаснет, пока не сгорит вся ересь.»</i>"
            )
        except Exception:
            logger.debug("Не удалось обновить промежуточный статус", exc_info=True)

        try:
            await update_operation(
                operation_id,
                processed=counters["processed"],
                banned=counters["banned"],
                skipped=counters["skipped"],
                errors=counters["errors"],
                bots_used=pool.burned_count() + 1,
            )
        except Exception:
            pass

    for user_id in users_batch:
        counters["processed"] += 1

        if user_id in admin_ids:
            counters["skipped"] += 1

        else:
            result = await _ban_single_user(pool.current().bot, chat_id, user_id, throttle)

            if result == "ejected":
                rotated = await _rotate_pool(pool, chat_id)

                if not rotated:
                    counters["errors"] += 1
                    await _update_progress()
                    return True

                # Новым ботом пробуем этого же пользователя ещё раз —
                # он не должен выпасть из списка просто из-за того,
                # что ему "не повезло" попасть на момент ротации.
                result = await _ban_single_user(
                    pool.current().bot, chat_id, user_id, throttle
                )

            if result == "banned":
                counters["banned"] += 1
                throttle.on_success()
                breaker.record_success()
                await throttle.wait()

            else:
                counters["errors"] += 1
                tripped = breaker.record_failure()

                if tripped:
                    logger.error(
                        "Circuit breaker сработал: %s подряд идущих "
                        "обычных (не ejected) неудачных банов. Похоже, "
                        "проблема не в лимите Telegram на аккаунт бота, "
                        "а в чём-то другом — операция прервана.",
                        breaker.limit,
                    )
                    await _update_progress()
                    return True

        if counters["processed"] % status_update_every == 0:
            await _update_progress()

    return False


async def _run_topics_phase(pool: BotPool, chat_id: int, keep_topic_id):
    """
    Выполняет удаление/создание форумных веток активным ботом
    пула, с ротацией при BotEjectedError. Возвращает
    (topics_total, topics_deleted, topics_created, topics_errors, aborted).
    """

    topics_total = topics_deleted = topics_created = topics_errors = 0

    # ---- удаление ----
    while True:
        try:
            topics_total, topics_deleted, topics_errors = await delete_all_topics_except(
                bot=pool.current().bot,
                chat_id=chat_id,
                keep_topic_id=keep_topic_id,
            )
            break

        except BotEjectedError:
            rotated = await _rotate_pool(pool, chat_id)
            if not rotated:
                return topics_total, topics_deleted, topics_created, topics_errors + 1, True
            # повторяем удаление заново новым ботом — уже удалённые
            # ветки просто не вернутся в список get_forum_topics

        except Exception as e:
            logger.exception("Ошибка удаления форумных веток: %s", e)
            topics_errors += 1
            break

    # ---- создание заменяющих ----
    if topics_deleted > 0:
        while True:
            try:
                topics_created, create_errors = await create_replacement_topics(
                    bot=pool.current().bot,
                    chat_id=chat_id,
                    count=topics_deleted,
                )
                topics_errors += create_errors
                break

            except BotEjectedError:
                rotated = await _rotate_pool(pool, chat_id)
                if not rotated:
                    return topics_total, topics_deleted, topics_created, topics_errors + 1, True

            except Exception as e:
                logger.exception("Ошибка создания заменяющих веток: %s", e)
                topics_errors += 1
                break

    return topics_total, topics_deleted, topics_created, topics_errors, False


@router.message(Command("fire_exterminatus"))
async def cmd_ext(
    message: types.Message,
    command: CommandObject,
    pool: BotPool,
):
    # ============================================================
    # ПРОВЕРКА ПОДТВЕРЖДЕНИЯ
    # ============================================================

    if not command.args or command.args.strip().lower() != "confirm":
        return await message.reply(
            "<b>ORDO INQUISITIONIS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>EXTERMINATUS</b>\n\n"
            "Класс протокола: <b>EXTREMIS</b>\n"
            "Статус: <b>ОЖИДАНИЕ ПОДТВЕРЖДЕНИЯ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>ОБНАРУЖЕНА ЕРЕСЬ</b>\n\n"
            "Сектор признан заражённым.\n"
            "Инквизиция санкционирует полное\n"
            "очищение сектора.\n\n"
            "Все обнаруженные в реестре\n"
            "субъекты, кроме авторизованного\n"
            "персонала, будут подвергнуты\n"
            "окончательному изгнанию.\n\n"
            f"Орбитальная группа укомплектована:\n"
            f"<b>{len(pool.all())}</b> боевых единиц в резерве.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>ДИРЕКТИВА</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Для активации протокола введите:\n"
            "<code>/fire_exterminatus confirm</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>«Ересь требует очищения.\n"
            "Очищение требует огня.»</i>"
        )

    # ============================================================
    # ОСНОВНЫЕ ДАННЫЕ
    # ============================================================

    chat_id = message.chat.id
    keep_topic_id = message.message_thread_id

    # Новая операция всегда начинается с первого небёрнутого бота
    # пула — даже если прошлый запуск сжёг часть ботов.
    pool.reset_for_new_operation()

    logger.info(
        "Запущен EXTERMINATUS: chat_id=%s, keep_topic_id=%s, "
        "активный бот=@%s (%d/%d в пуле)",
        chat_id,
        keep_topic_id,
        pool.current().username,
        pool.burned_count() + 1,
        len(pool.all()),
    )

    status_msg = await message.reply(
        "<b>ORDO INQUISITIONIS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>EXTERMINATUS</b>\n\n"
        "Авторизация: <b>ПОДТВЕРЖДЕНА</b>\n"
        "Приоритет: <b>АБСОЛЮТНЫЙ</b>\n"
        "Статус: <b>ВЫПОЛНЕНИЕ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Орбитальная группа возмездия\n"
        "выходит на позицию.\n\n"
        "Сканирование реестра...\n"
        "Идентификация субъектов...\n"
        "Синхронизация форумных веток...\n\n"
        "<b>ОГОНЬ РАЗРЕШЁН.</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>«Во имя Императора.»</i>"
    )

    # ============================================================
    # АДМИНИСТРАТОРЫ + ВСЕ БОТЫ ПУЛА (никогда не банятся)
    # ============================================================

    admin_ids = await get_admin_ids(pool.primary, chat_id)
    admin_ids |= pool.all_user_ids()
    admin_ids.add(message.from_user.id)

    # ============================================================
    # ЗАРЕГИСТРИРОВАННЫЕ ПОЛЬЗОВАТЕЛИ
    # ============================================================

    users = await get_seen_users(chat_id)
    total = len(users)

    operation_id = await start_operation(chat_id, total)

    first_batch = users[:BAN_BATCH_SIZE]
    remaining_batch = users[BAN_BATCH_SIZE:]

    counters = {"processed": 0, "banned": 0, "skipped": 0, "errors": 0}

    throttle = AdaptiveThrottle(base_delay=1.0, max_delay=8.0)
    breaker = BanCircuitBreaker()
    STATUS_UPDATE_EVERY = 25

    # Перед первым баном — сразу выдаём активному боту права,
    # не дожидаясь, пока он кого-то забанит. Если это первый
    # запуск и бот уже был назначен админом вручную — promote
    # просто подтвердит те же права повторно, это безопасно.
    await promote_bot_admin(
        chat_id,
        pool.current().user_id,
        pool.current().username,
    )
    await asyncio.sleep(ROTATION_SETTLE_DELAY)

    # ============================================================
    # ФАЗА 1: ПЕРВАЯ ПАРТИЯ
    # ============================================================

    aborted = await _ban_batch(
        pool=pool,
        chat_id=chat_id,
        users_batch=first_batch,
        admin_ids=admin_ids,
        throttle=throttle,
        breaker=breaker,
        counters=counters,
        status_msg=status_msg,
        total=total,
        status_update_every=STATUS_UPDATE_EVERY,
        operation_id=operation_id,
    )

    topics_total = topics_deleted = topics_created = topics_errors = 0

    if not aborted:
        await asyncio.sleep(3)

        # ========================================================
        # ФАЗА 2: УДАЛЕНИЕ/СОЗДАНИЕ ФОРУМНЫХ ВЕТОК
        # ========================================================

        (
            topics_total,
            topics_deleted,
            topics_created,
            topics_errors,
            topics_aborted,
        ) = await _run_topics_phase(pool, chat_id, keep_topic_id)

        if topics_aborted:
            aborted = True

        if not aborted:
            await asyncio.sleep(3)

            # ====================================================
            # ФАЗА 3: ОСТАВШИЕСЯ ПОЛЬЗОВАТЕЛИ
            # ====================================================

            if remaining_batch:
                aborted = await _ban_batch(
                    pool=pool,
                    chat_id=chat_id,
                    users_batch=remaining_batch,
                    admin_ids=admin_ids,
                    throttle=throttle,
                    breaker=breaker,
                    counters=counters,
                    status_msg=status_msg,
                    total=total,
                    status_update_every=STATUS_UPDATE_EVERY,
                    operation_id=operation_id,
                )
    else:
        logger.warning(
            "EXTERMINATUS прерван на первой партии банов — "
            "дальше (ветки, вторая партия) не выполняется."
        )

    await finish_operation(operation_id, "aborted" if aborted else "completed")

    # ============================================================
    # ФИНАЛЬНЫЙ ОТЧЁТ
    # ============================================================

    if keep_topic_id is None:
        kept_topic_text = "General / текущая тема не определена"
    elif keep_topic_id == 1:
        kept_topic_text = "General"
    else:
        kept_topic_text = f"ID {keep_topic_id}"

    bots_used = pool.burned_count() + 1
    bots_total = len(pool.all())

    if aborted:
        status_block = (
            "<b>СТАТУС ОПЕРАЦИИ</b>\n"
            "ПРЕРВАНО ДОСРОЧНО\n\n"
            f"Пул из {bots_total} бот(ов) исчерпан, либо подряд "
            f"накопилось {breaker.limit} обычных ошибок, не связанных "
            "с лимитом на аккаунт. Рекомендуется добавить в пул ещё "
            "токенов и/или подождать перед повтором.\n\n"
        )
    else:
        status_block = (
            "<b>СТАТУС ОПЕРАЦИИ</b>\n"
            "ЗАВЕРШЕНО\n\n"
            f"Задействовано ботов из пула: <b>{bots_used}/{bots_total}</b>\n\n"
        )

    text = (
        "<b>ORDO INQUISITIONIS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>EXTERMINATUS — FINAL REPORT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        + status_block +

        "<b>ФОРУМНЫЕ ВЕТКИ</b>\n\n"
        f"Найдено: <b>{topics_total}</b>\n"
        f"Удалено: <b>{topics_deleted}</b>\n"
        f"Создано веток «АВЕ ИМП»: <b>{topics_created}</b>\n"
        f"Ошибок: <b>{topics_errors}</b>\n"
        f"Сохранена ветка: <b>{kept_topic_text}</b>\n\n"

        "<b>ОПЕРАТИВНЫЕ ДАННЫЕ</b>\n\n"
        f"Зарегистрировано: <b>{total}</b>\n"
        f"Обработано: <b>{counters['processed']}</b>\n"
        f"Администраторов/ботов пула: <b>{counters['skipped']}</b>\n"
        f"Уничтожено: <b>{counters['banned']}</b>\n"
        f"Ошибок: <b>{counters['errors']}</b>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n\n"

        + (
            "<b>СЕКТОР ОЧИЩЕН</b>\n\n"
            "Враждебные субъекты устранены.\n"
            "Форумные ветки обработаны.\n"
            "Протокол Exterminatus завершён.\n\n"
            if not aborted else
            "<b>ОТСТУПЛЕНИЕ</b>\n\n"
            "Орбитальная группа отозвана до\n"
            "прояснения обстановки.\n\n"
        )

        + "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>«Из пепла рождается порядок.\n"
        "Из порядка — Империум.»</i>\n\n"

        "<b>СЛАВА ИМПЕРИУМУ ЧЕЛОВЕЧЕСТВА</b>"
    )

    async def _send_final_report():
        attempts = 0
        max_attempts = 5

        while attempts <= max_attempts:
            try:
                await status_msg.edit_text(text)
                return
            except TelegramRetryAfter as e:
                attempts += 1
                logger.warning(
                    "FloodWait при edit_text: ожидание %s сек (%s/%s)",
                    e.retry_after, attempts, max_attempts,
                )
                await asyncio.sleep(e.retry_after + 1)
            except Exception as e:
                logger.warning("Не удалось изменить статус-сообщение: %s", e)
                break

        attempts = 0
        while attempts <= max_attempts:
            try:
                await message.answer(text)
                return
            except TelegramRetryAfter as e:
                attempts += 1
                logger.warning(
                    "FloodWait при answer: ожидание %s сек (%s/%s)",
                    e.retry_after, attempts, max_attempts,
                )
                await asyncio.sleep(e.retry_after + 1)
            except Exception:
                logger.exception("Не удалось отправить финальный отчёт")
                return

        logger.error(
            "Не удалось отправить финальный отчёт после нескольких попыток FloodWait"
        )

    await _send_final_report()

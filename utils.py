import asyncio
import logging
from enum import IntEnum

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)


class Role(IntEnum):
    MEMBER = 0
    HELPER = 1
    MODERATOR = 2
    SENIOR_ADMIN = 3
    OWNER = 4


ROLE_NAMES = {
    Role.OWNER: "Owner",
    Role.SENIOR_ADMIN: "Senior Admin",
    Role.MODERATOR: "Moderator",
    Role.HELPER: "Helper",
    Role.MEMBER: "Участник",
}

NO_RIGHTS_TEXT = "Недостаточно Порчи в твоей крови для этого ритуала."


async def get_member(bot: Bot, chat_id: int, user_id: int):
    try:
        return await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError as e:
        logger.warning(
            "Не удалось получить участника user_id=%s в chat_id=%s: %s",
            user_id, chat_id, e
        )
        return None
    except Exception as e:
        logger.error(
            "Неожиданная ошибка при получении участника user_id=%s в chat_id=%s: %s",
            user_id, chat_id, e
        )
        return None


def resolve_role(member) -> Role:
    if member is None:
        return Role.MEMBER

    if member.status == ChatMemberStatus.CREATOR:
        return Role.OWNER

    if member.status == ChatMemberStatus.ADMINISTRATOR:
        if getattr(member, "can_promote_members", False):
            return Role.SENIOR_ADMIN
        if getattr(member, "can_restrict_members", False) and getattr(member, "can_delete_messages", False):
            return Role.MODERATOR
        return Role.HELPER

    return Role.MEMBER


async def get_role(bot: Bot, chat_id: int, user_id: int) -> Role:
    member = await get_member(bot, chat_id, user_id)
    return resolve_role(member)


async def has_role(bot: Bot, chat_id: int, user_id: int, minimal: Role) -> bool:
    role = await get_role(bot, chat_id, user_id)
    return role >= minimal


# Используется в /exterminatus и для защиты "нельзя банить админа" —
# не завязана на RBAC специально, чтобы защищать любого Telegram-админа.
async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await get_member(bot, chat_id, user_id)
    if member is None:
        return False
    return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


async def get_admin_ids(bot: Bot, chat_id: int) -> set[int]:
    try:
        admins = await bot.get_chat_administrators(chat_id)
        return {admin.user.id for admin in admins}
    except Exception as e:
        logger.warning(f"Не удалось получить список админов чата {chat_id}: {e}")
        return set()


# ============================================================
# ДЕТЕКЦИЯ "БОТА ВЫКИНУЛИ ИЗ ЧАТА" (новый принцип работы)
# ============================================================
#
# Отдельно от обычных ошибок бана конкретного пользователя: это
# сигнал, что ВЕСЬ аккаунт бота больше не дееспособен в этом чате
# (выкинут целиком или лишён нужных прав антиспам-эвристикой
# Telegram после слишком большой серии банов подряд). На такой
# сигнал нет смысла реагировать "ещё раз попробовать этого же
# юзера этим же ботом" — нужна ротация на следующий бот из пула.

_EJECTED_MARKERS = (
    "bot was kicked",
    "bot_kicked",
    "bot is not a member",
    "not enough rights",
    "have no rights",
    "member_no_rights",
    "chat not found",
    "chat_not_found",
    "user_not_participant",
    "kicked from the",
    "chat admin required",
    "need administrator rights",
)


def is_bot_ejected_error(exc: Exception) -> bool:
    """
    True, если исключение похоже на "бота выкинули/лишили прав
    именно в этом чате", а не на обычную ошибку по конкретному
    пользователю (например "USER_ID_INVALID" для уже вышедшего
    юзера — это НЕ ejected-сигнал).
    """

    text = str(exc).lower()
    return any(marker in text for marker in _EJECTED_MARKERS)


class AdaptiveThrottle:
    """
    Адаптивная пауза между массовыми административными действиями
    (баны, удаление/создание веток и т.п.).

    Идея: не держать фиксированную "на глаз" паузу всё время, а
    начинать с небольшой (base_delay) и увеличивать её только
    когда Telegram реально пожаловался (FloodWait), постепенно
    возвращая к базовой после recovery_after подряд успешных
    операций без жалоб.
    """

    def __init__(
        self,
        base_delay: float = 0.8,
        max_delay: float = 6.0,
        increase_factor: float = 1.6,
        recovery_after: int = 10,
    ):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.increase_factor = increase_factor
        self.recovery_after = recovery_after

        self.current_delay = base_delay
        self._success_streak = 0

    async def wait(self):
        await asyncio.sleep(self.current_delay)

    def on_success(self):
        """Вызывать после каждой успешной операции."""
        self._success_streak += 1

        if (
            self._success_streak >= self.recovery_after
            and self.current_delay > self.base_delay
        ):
            self.current_delay = max(
                self.base_delay,
                self.current_delay / self.increase_factor,
            )
            self._success_streak = 0

    def on_flood_wait(self, retry_after: float):
        """Вызывать при получении TelegramRetryAfter."""
        self._success_streak = 0

        self.current_delay = min(
            self.max_delay,
            max(self.current_delay * self.increase_factor, retry_after * 0.1),
        )


def get_real_reply(message):
    """
    Возвращает message.reply_to_message только если это НАСТОЯЩИЙ
    ответ на чьё-то сообщение — а не автоматическая привязка
    к теме форума.

    В супергруппах с темами (топиками) Telegram связывает сообщение
    с темой через тот же механизм reply_to_message, что и обычный
    ответ пользователя. Без этой проверки любая команда, опирающаяся
    на "ответь на сообщение того, кого хочешь забанить/замутить/
    посмотреть карточку" — рискует принять корневое сообщение темы
    за настоящую цель.

    Признак автопривязки к теме: reply_to_message.message_id
    совпадает с message_thread_id.
    """
    reply = message.reply_to_message
    if reply is None:
        return None

    is_topic_auto_link = (
        message.is_topic_message
        and reply.message_id == message.message_thread_id
    )

    if is_topic_auto_link:
        return None

    return reply

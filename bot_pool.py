import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

logger = logging.getLogger(__name__)


@dataclass
class PoolBot:
    token: str
    bot: Bot
    user_id: int | None = None
    username: str | None = None
    burned: bool = False


class BotPool:
    """
    Пул из нескольких ботов (по одному Bot-инстансу на каждый
    токен из config.BOT_TOKENS), используемых ПО ОЧЕРЕДИ в рамках
    одной операции /fire_exterminatus.

    Принцип ротации: Telegram выбрасывает/лишает прав конкретный
    бот-аккаунт после определённого порога банов подряд в чате —
    это особенность самого аккаунта бота, а не процесса, который
    им управляет. Поэтому как только активный бот словил такой
    сигнал ("ejected" — см. utils.is_bot_ejected_error) — пул
    отмечает его burned и переключается на следующий токен, а
    mtproto.promote_bot_admin выдаёт новому боту права прямо по
    ходу операции, без участия человека.

    primary (индекс 0) — это ещё и бот, который держит long polling
    и отвечает за весь UI (приём команд, редактирование статус-
    сообщений). Он тоже участвует в банах как обычный член пула —
    отдельного "бота только для команд" заводить не обязательно,
    но можно, если хочется вообще не рисковать его доступом в чат
    (тогда просто не используйте его для банов — это не автоматизировано
    отдельным флагом в этом шаблоне, но pool.advance() можно вызвать
    вручную при старте операции, чтобы пропустить primary).
    """

    def __init__(self, tokens: list[str], parse_mode: ParseMode = ParseMode.HTML):
        if not tokens:
            raise ValueError("Пул не может быть пустым — нужен хотя бы один BOT_TOKEN")

        self._bots: list[PoolBot] = [
            PoolBot(
                token=t,
                bot=Bot(token=t, default=DefaultBotProperties(parse_mode=parse_mode)),
            )
            for t in tokens
        ]
        self._index = 0

    async def warmup(self):
        """
        Один раз при старте процесса — подтягивает get_me() для
        каждого бота пула (нужен user_id/username для промоута
        через Telethon).
        """

        for pb in self._bots:
            try:
                me = await pb.bot.get_me()
                pb.user_id = me.id
                pb.username = me.username

                logger.info(
                    "Бот в пуле: @%s (id=%s)",
                    pb.username,
                    pb.user_id,
                )

            except Exception:
                logger.exception(
                    "Не удалось получить get_me() для одного из "
                    "токенов пула — проверь, что токен валиден"
                )

    def all(self) -> list[PoolBot]:
        return list(self._bots)

    def all_user_ids(self) -> set[int]:
        """Все боты пула — чтобы они никогда не банили друг друга."""
        return {pb.user_id for pb in self._bots if pb.user_id is not None}

    @property
    def primary(self) -> Bot:
        """Бот, который держит polling / tracking / stats / UI."""
        return self._bots[0].bot

    @property
    def primary_info(self) -> PoolBot:
        return self._bots[0]

    def current(self) -> PoolBot:
        return self._bots[self._index]

    @property
    def index(self) -> int:
        return self._index

    def has_next(self) -> bool:
        return self._index < len(self._bots) - 1

    def remaining(self) -> int:
        return len(self._bots) - self._index - 1

    def advance(self) -> PoolBot | None:
        """
        Помечает текущего бота burned и переключается на
        следующего. Возвращает новый активный PoolBot, либо None,
        если пул исчерпан полностью.
        """

        self._bots[self._index].burned = True

        if not self.has_next():
            logger.error(
                "Пул ботов исчерпан (%d шт.) — ротация больше невозможна",
                len(self._bots),
            )
            return None

        self._index += 1

        logger.warning(
            "Ротация: переключаюсь на бота @%s (%d/%d)",
            self._bots[self._index].username,
            self._index + 1,
            len(self._bots),
        )

        return self.current()

    def reset_for_new_operation(self):
        """
        Перед новым запуском /fire_exterminatus — начинаем снова
        с первого небёрнутого бота в пуле.
        """

        for i, pb in enumerate(self._bots):
            if not pb.burned:
                self._index = i
                return

        # Все боты уже отмечены burned с прошлого раза (например,
        # операцию запускали вчера и исчерпали весь пул). Telegram
        # обычно снимает ограничение через какое-то время — даём
        # пулу ещё один шанс с начала, а не блокируем команду
        # навсегда.
        logger.warning(
            "Все боты пула помечены burned с прошлого запуска — "
            "сбрасываю флаги и пробую сначала"
        )

        for pb in self._bots:
            pb.burned = False

        self._index = 0

    def burned_count(self) -> int:
        return sum(1 for pb in self._bots if pb.burned)

    async def close_all(self):
        for pb in self._bots:
            try:
                await pb.bot.session.close()
            except Exception:
                pass

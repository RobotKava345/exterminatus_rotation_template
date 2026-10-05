from aiogram import Router, types, F
from aiogram.enums import ChatType

from database.db import add_user

router = Router()


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    ~F.text.startswith("/")
)
async def track_messages(message: types.Message):
    if not message.from_user or message.from_user.is_bot:
        return
    await add_user(message.chat.id, message.from_user.id)


@router.chat_member()
async def track_joins(event: types.ChatMemberUpdated):
    new_member = event.new_chat_member
    if new_member.status in {"member", "restricted"} and not new_member.user.is_bot:
        await add_user(event.chat.id, new_member.user.id)

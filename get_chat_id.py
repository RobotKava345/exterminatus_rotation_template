import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")

client = TelegramClient(
    "telegram_session",
    API_ID,
    API_HASH,
)


async def main():
    await client.start()

    print("\n=== ДОСТУПНЫЕ ЧАТЫ ===\n")

    async for dialog in client.iter_dialogs():
        entity = dialog.entity

        if dialog.is_group:
            print(
                f"ID: {dialog.id} | "
                f"Title: {dialog.name}"
            )

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

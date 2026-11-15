"""aiogram router wiring updates to the :class:`Assistant`.

Only three update kinds are requested from Telegram. Business updates drive the
assistant; the owner's private ``/pause``, ``/resume``, ``/status`` and
``/start`` are handled here. Everyone else who writes to the bot directly gets
a short note (``/start``) or is ignored.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BusinessConnection, Message

from .service import Assistant

# The only updates we ask Telegram to deliver (long polling).
ALLOWED_UPDATES = ["message", "business_connection", "business_message"]

_STRANGER_START = (
    "Bu — shaxsiy yordamchi bot va faqat o'z egasi uchun ishlaydi. "
    "Bu yerda suhbatlashib bo'lmaydi. Iltimos, kerakli odamga "
    "to'g'ridan-to'g'ri yozing."
)


def build_router(assistant: Assistant) -> Router:
    router = Router(name="assistant")
    owner_id = assistant.settings.owner_id

    def is_owner_private(message: Message) -> bool:
        user = message.from_user
        return (
            message.chat.type == "private"
            and user is not None
            and user.id == owner_id
        )

    @router.business_connection()
    async def _on_connection(event: BusinessConnection) -> None:
        await assistant.on_business_connection(event)

    @router.business_message()
    async def _on_business_message(message: Message, bot: Bot) -> None:
        await assistant.on_business_message(bot, message)

    @router.message(CommandStart())
    async def _on_start(message: Message) -> None:
        if message.chat.type != "private":
            return
        if is_owner_private(message):
            await message.answer(assistant.start_text())
        else:
            await message.answer(_STRANGER_START)

    @router.message(Command("pause"))
    async def _on_pause(message: Message) -> None:
        if not is_owner_private(message):
            return
        assistant.pause()
        await message.answer("To'xtatildi. Qayta yoqish uchun /resume yuboring.")

    @router.message(Command("resume"))
    async def _on_resume(message: Message) -> None:
        if not is_owner_private(message):
            return
        assistant.resume()
        await message.answer("Qayta ishga tushdi.")

    @router.message(Command("status"))
    async def _on_status(message: Message) -> None:
        if not is_owner_private(message):
            return
        await message.answer(assistant.status_text())

    # Any other direct message (owner chit-chat or a stranger) is ignored.
    @router.message()
    async def _ignore(message: Message, **_: Any) -> None:
        return

    return router

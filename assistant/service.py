"""The assistant's core logic: decide, generate, preview or send.

Everything Telegram-shaped enters through :meth:`Assistant.on_business_message`.
The method is a straight-line decision pipeline that returns an :class:`Outcome`
so it is fully unit-testable without a real network or a real Claude.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, Optional, Protocol

from aiogram.exceptions import TelegramAPIError

from .claude_cli import (
    ClaudeAuthError,
    ClaudeError,
    ClaudeLimitError,
    ClaudeTimeout,
    ClaudeCLI,
)
from .config import Settings
from .language import detect_language
from .prompts import SKIP_TOKEN, build_system_prompt, build_user_prompt
from .store import HistoryStore, RuntimeState, SeenMessages, ensure_private_dir

log = logging.getLogger("assistant.service")

MAX_TELEGRAM_CHARS = 4000
_ERROR_NOTIFY_INTERVAL = 600  # at most one generic-error DM every 10 minutes


class Outcome(str, Enum):
    PREVIEWED = "previewed"
    SENT = "sent"
    DUPLICATE = "duplicate"
    IGNORED_BOT = "ignored_bot"
    NO_CONNECTION = "no_connection"
    FOREIGN_CONNECTION = "foreign_connection"
    CONNECTION_DISABLED = "connection_disabled"
    UNSUPPORTED = "unsupported"
    OWNER_MESSAGE = "owner_message"
    OWNER_ACTIVE = "owner_active"
    PAUSED = "paused"
    LIMITED = "limited"
    SKIPPED = "skipped"
    PERMISSION_DENIED = "permission_denied"
    ERROR = "error"


@dataclass
class Preview:
    connection_id: str
    chat_id: int
    chat_name: str
    language: Optional[str]
    incoming: str
    reply: str
    note: Optional[str] = None


class Generator(Protocol):
    async def generate(self, system_prompt: str, conversation: str) -> str: ...


def print_preview(preview: Preview) -> None:
    """Pretty-print a drafted reply that was NOT sent."""
    note = f"\n  note:     {preview.note}" if preview.note else ""
    log.info(
        "\n----- PREVIEW, not sent -----\n"
        "  chat:     %s (id=%s)\n"
        "  language: %s\n"
        "  incoming: %s\n"
        "  reply:    %s%s\n"
        "-----------------------------",
        preview.chat_name,
        preview.chat_id,
        preview.language or "?",
        preview.incoming,
        preview.reply,
        note,
    )


def describe(message: Any) -> Optional[str]:
    """Human-readable text for a message, or ``None`` for service messages."""
    # content_type is a str-enum (ContentType); compare by value, not str().
    content_type = getattr(message, "content_type", None)
    caption = (getattr(message, "caption", None) or "").strip()

    if content_type == "text":
        return message.text
    if content_type == "voice":
        return "[voice message]"
    if content_type == "video_note":
        return "[video note]"
    if content_type == "sticker":
        emoji = getattr(getattr(message, "sticker", None), "emoji", "") or ""
        return f"[sticker] {emoji}".strip()
    if content_type == "animation":
        return f"[gif] {caption}".strip()
    if content_type == "photo":
        return f"[photo] {caption}".strip()
    if content_type == "video":
        return f"[video] {caption}".strip()
    if content_type == "audio":
        return f"[audio] {caption}".strip()
    if content_type == "document":
        return f"[document] {caption}".strip()
    if content_type == "location":
        return "[location]"
    if content_type == "contact":
        return "[contact]"
    if content_type == "poll":
        question = getattr(getattr(message, "poll", None), "question", "") or ""
        return f"[poll] {question}".strip()
    return None  # new_chat_members and other service events


def can_reply(connection: Any) -> bool:
    """Whether the bot is allowed to reply on this business connection."""
    rights = getattr(connection, "rights", None)
    if rights is not None:
        return bool(getattr(rights, "can_reply", False))
    return bool(getattr(connection, "can_reply", False))


_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def read_profile(path: Path) -> str:
    """Read ``profile.md`` fresh, stripping HTML comments."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return _COMMENT_RE.sub("", text).strip()


def _detectable_text(message: Any) -> str:
    if getattr(message, "content_type", None) == "text":
        return message.text or ""
    return (getattr(message, "caption", None) or "").strip()


def _chat_name(message: Any) -> str:
    chat = getattr(message, "chat", None)
    title = getattr(chat, "title", None) if chat else None
    if title:
        return str(title)
    user = getattr(message, "from_user", None)
    if user is not None:
        return str(getattr(user, "full_name", None) or getattr(user, "first_name", "") or "Someone")
    return "Someone"


def _sender_name(message: Any) -> str:
    user = getattr(message, "from_user", None)
    if user is None:
        return "Someone"
    return str(getattr(user, "full_name", None) or getattr(user, "first_name", "") or "Someone")


def _chunks(text: str, size: int = MAX_TELEGRAM_CHARS):
    for start in range(0, len(text), size):
        yield text[start : start + size]


class Assistant:
    def __init__(
        self,
        settings: Settings,
        generator: Generator,
        *,
        clock: Callable[[], float] = time.time,
        preview_sink: Callable[[Preview], None] = print_preview,
    ) -> None:
        self.settings = settings
        self._generator = generator
        self._clock = clock
        self._preview_sink = preview_sink

        data_dir = settings.data_dir
        ensure_private_dir(data_dir)
        self.history = HistoryStore(data_dir / "history.json", settings.history_limit)
        self.seen = SeenMessages(data_dir / "seen.json")
        self.state = RuntimeState(data_dir / "state.json")
        self._previews_path = data_dir / "previews.jsonl"

        self._connections: Dict[str, Any] = {}
        self._takeover: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self.outcomes: Deque[Outcome] = deque(maxlen=100)
        self._last_error_notify = 0.0

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _record(self, outcome: Outcome) -> Outcome:
        self.outcomes.append(outcome)
        return outcome

    def _takeover_active(self, key: str) -> bool:
        expiry = self._takeover.get(key, 0.0)
        return expiry > self._clock()

    def _arm_takeover(self, key: str) -> None:
        minutes = self.settings.owner_takeover_minutes
        if minutes > 0:
            self._takeover[key] = self._clock() + minutes * 60

    async def _fetch_connection(self, bot: Any, connection_id: str) -> Optional[Any]:
        try:
            connection = await bot.get_business_connection(connection_id)
        except TelegramAPIError as exc:
            log.warning("Could not fetch business connection %s: %s", connection_id, exc)
            return None
        self._connections[connection_id] = connection
        return connection

    def _owns(self, connection: Any) -> bool:
        owner = getattr(getattr(connection, "user", None), "id", None)
        return owner == self.settings.owner_id

    async def _notify_owner(self, bot: Any, text: str) -> None:
        if self.settings.owner_id is None:
            return
        try:
            await bot.send_message(self.settings.owner_id, text)
        except TelegramAPIError as exc:
            log.warning("Could not notify owner: %s", exc)

    def _write_preview(self, preview: Preview) -> None:
        ensure_private_dir(self._previews_path.parent)
        record = {
            "ts": self._clock(),
            "connection_id": preview.connection_id,
            "chat_id": preview.chat_id,
            "chat_name": preview.chat_name,
            "language": preview.language,
            "incoming": preview.incoming,
            "reply": preview.reply,
            "note": preview.note,
        }
        with self._previews_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            os.chmod(self._previews_path, 0o600)
        except (OSError, NotImplementedError):
            pass

    # ------------------------------------------------------------------ #
    # main entry
    # ------------------------------------------------------------------ #
    async def on_business_message(self, bot: Any, message: Any) -> Outcome:
        connection_id = getattr(message, "business_connection_id", None)
        if not connection_id:
            return self._record(Outcome.NO_CONNECTION)

        chat_id = message.chat.id
        message_id = message.message_id

        # (2) De-duplicate BEFORE any await so re-deliveries never double-process.
        seen_key = f"{connection_id}:{chat_id}:{message_id}"
        if not self.seen.first_time(seen_key):
            return self._record(Outcome.DUPLICATE)

        # (3) Never answer a bot (including our own sent replies).
        if getattr(message, "sender_business_bot", None) is not None:
            return self._record(Outcome.IGNORED_BOT)
        from_user = getattr(message, "from_user", None)
        if from_user is not None and getattr(from_user, "is_bot", False):
            return self._record(Outcome.IGNORED_BOT)

        # (4) Resolve the connection; only the owner's, only when enabled.
        connection = self._connections.get(connection_id)
        if connection is None:
            connection = await self._fetch_connection(bot, connection_id)
            if connection is None:
                return self._record(Outcome.NO_CONNECTION)
        if not self._owns(connection):
            return self._record(Outcome.FOREIGN_CONNECTION)
        if not getattr(connection, "is_enabled", True):
            return self._record(Outcome.CONNECTION_DISABLED)

        # (5) Only message types we can describe.
        incoming = describe(message)
        if incoming is None:
            return self._record(Outcome.UNSUPPORTED)

        history_key = HistoryStore.key(connection_id, chat_id)
        now = self._clock()

        # (6) The owner's own message: record it, arm takeover, never reply.
        if from_user is not None and getattr(from_user, "id", None) == self.settings.owner_id:
            self.history.add(
                connection_id,
                chat_id,
                role="owner",
                name="You",
                text=incoming,
                lang=detect_language(_detectable_text(message)),
                ts=now,
            )
            self._arm_takeover(history_key)
            return self._record(Outcome.OWNER_MESSAGE)

        # (7) Detect language (fall back to the chat's last language).
        language = detect_language(_detectable_text(message))
        if language is None:
            language = self.history.last_language(connection_id, chat_id)
        self.history.add(
            connection_id,
            chat_id,
            role="other",
            name=_sender_name(message),
            text=incoming,
            lang=language,
            ts=now,
        )

        chat_name = _chat_name(message)

        async with self._lock(history_key):
            # (8) Blocks.
            if self.state.paused:
                return self._record(Outcome.PAUSED)
            if self.state.limit_until > self._clock():
                return self._record(Outcome.LIMITED)
            if self._takeover_active(history_key):
                return self._record(Outcome.OWNER_ACTIVE)

            # (9) Generate.
            system_prompt = build_system_prompt(
                read_profile(self.settings.profile_path),
                _dt.datetime.fromtimestamp(self._clock()),
            )
            user_prompt = build_user_prompt(
                self.history.get(connection_id, chat_id), language
            )
            try:
                reply = await self._generator.generate(system_prompt, user_prompt)
            except ClaudeLimitError as exc:
                resets = exc.resets_at
                if resets and resets > self._clock():
                    self.state.limit_until = float(resets)
                else:
                    self.state.limit_until = self._clock() + self.settings.limit_cooldown_minutes * 60
                log.warning("Claude usage limit hit: %s", exc)
                if self.settings.auto_send:
                    await self._notify_owner(
                        bot, f"⚠️ {exc}. Assistant paused until the limit resets."
                    )
                return self._record(Outcome.LIMITED)
            except (ClaudeAuthError, ClaudeTimeout, ClaudeError) as exc:
                log.error("Claude generation failed (%s): %s", type(exc).__name__, exc)
                if self.settings.auto_send and (self._clock() - self._last_error_notify) >= _ERROR_NOTIFY_INTERVAL:
                    self._last_error_notify = self._clock()
                    await self._notify_owner(bot, f"⚠️ Assistant error: {exc}")
                return self._record(Outcome.ERROR)

            reply = (reply or "").strip()

            # (10) Nothing to say.
            if not reply or reply == SKIP_TOKEN:
                return self._record(Outcome.SKIPPED)

            # (11) Preview mode: show, log, do NOT send, do NOT store the reply.
            if not self.settings.auto_send:
                preview = Preview(
                    connection_id=connection_id,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    language=language,
                    incoming=incoming,
                    reply=reply,
                )
                self._preview_sink(preview)
                self._write_preview(preview)
                return self._record(Outcome.PREVIEWED)

            # (12) Auto-send mode.
            # Re-check blocks: the owner may have replied during generation.
            if self.state.paused:
                return self._record(Outcome.PAUSED)
            if self.state.limit_until > self._clock():
                return self._record(Outcome.LIMITED)
            if self._takeover_active(history_key):
                self._preview_sink(
                    Preview(
                        connection_id, chat_id, chat_name, language, incoming, reply,
                        note="not sent: the owner replied during generation",
                    )
                )
                return self._record(Outcome.OWNER_ACTIVE)

            # Re-fetch the connection from Telegram (not the cache).
            fresh = await self._fetch_connection(bot, connection_id)
            if fresh is None or not self._owns(fresh) or not getattr(fresh, "is_enabled", True) or not can_reply(fresh):
                self._preview_sink(
                    Preview(
                        connection_id, chat_id, chat_name, language, incoming, reply,
                        note="not sent: no permission to reply on this connection",
                    )
                )
                return self._record(Outcome.PERMISSION_DENIED)

            try:
                for chunk in _chunks(reply):
                    await bot.send_message(
                        chat_id, chunk, business_connection_id=connection_id
                    )
            except TelegramAPIError as exc:
                log.error("Failed to send reply: %s", exc)
                return self._record(Outcome.ERROR)

            self.history.add(
                connection_id,
                chat_id,
                role="owner",
                name="You",
                text=reply,
                lang=language,
                ts=self._clock(),
            )
            return self._record(Outcome.SENT)

    async def on_business_connection(self, connection: Any) -> None:
        """Cache a connection update and log it. Never sends a message."""
        connection_id = getattr(connection, "id", None)
        if connection_id:
            self._connections[connection_id] = connection
        if not self._owns(connection):
            log.warning(
                "Business connection from a non-owner account (user=%s) ignored.",
                getattr(getattr(connection, "user", None), "id", None),
            )
            return
        log.info(
            "Business connection %s: user=%s enabled=%s can_reply=%s",
            connection_id,
            getattr(getattr(connection, "user", None), "id", None),
            getattr(connection, "is_enabled", None),
            can_reply(connection),
        )

    # ------------------------------------------------------------------ #
    # owner controls
    # ------------------------------------------------------------------ #
    def pause(self) -> None:
        self.state.paused = True

    def resume(self) -> None:
        self.state.paused = False
        self.state.limit_until = 0.0
        self._takeover.clear()

    def status_text(self) -> str:
        mode = (
            "AUTO-SEND"
            if self.settings.auto_send
            else "PREVIEW (replies are only shown in the logs)"
        )
        now = self._clock()
        if self.state.limit_until > now:
            when = _dt.datetime.fromtimestamp(self.state.limit_until).strftime(
                "%Y-%m-%d %H:%M"
            )
            limit_line = f"Usage limit active until {when}"
        else:
            limit_line = "No active usage limit"
        return (
            f"Mode: {mode}\n"
            f"State: {'PAUSED' if self.state.paused else 'running'}\n"
            f"{limit_line}\n"
            f"Chats with history: {self.history.chat_count()}"
        )

    def start_text(self) -> str:
        """Owner-facing onboarding text, in Uzbek."""
        if self.settings.auto_send:
            mode_line = "Hozirgi rejim: AVTO-YUBORISH — javoblar o'zi yuboriladi."
        else:
            mode_line = (
                "Hozirgi rejim: PREVIEW — javoblar yuborilmaydi, faqat loglarda "
                "ko'rsatiladi. Ishonsangiz, Railway'da AUTO_SEND=true qiling."
            )
        state_line = (
            "Holat: PAUZADA (javob bermayapti)."
            if self.state.paused
            else "Holat: ishlayapti."
        )
        return (
            "Assalomu alaykum! Men sizning shaxsiy yordamchi botingizman. "
            "Telegram Business orqali ulanganimda, sizga kimdir yozsa, men "
            "sizning uslubingizda (profile.md asosida) javob tayyorlayman.\n\n"
            f"{mode_line}\n{state_line}\n\n"
            "Qanday ulanaman:\n"
            "Telegram → Settings → Telegram Business → Chatbots → bot "
            "username'ini tanlang → qaysi chatlarni ko'rishimni belgilang → "
            "javob berish ruxsatini yoqing. (Buning uchun Telegram Premium "
            "kerak.)\n\n"
            "Buyruqlar:\n"
            "/pause — meni to'xtatish\n"
            "/resume — qayta ishga tushirish\n"
            "/status — hozirgi holat\n\n"
            "Bilib qo'ying:\n"
            f"• O'zingiz yozsangiz, men o'sha chatda "
            f"{self.settings.owner_takeover_minutes} daqiqa jim turaman.\n"
            "• Pul, qarz yoki uchrashuvga hech qachon rozilik bermayman — "
            "\"o'zim keyin yozaman\" deyman.\n"
            "• Yaxshi javob berishim uchun profile.md ni to'ldirib qo'ying.\n"
            "• Preview rejimida bo'lsa, javoblarni yuborishim uchun "
            "AUTO_SEND=true qilish kerak."
        )


# --------------------------------------------------------------------------- #
# factories
# --------------------------------------------------------------------------- #
def make_claude_cli(settings: Settings) -> ClaudeCLI:
    return ClaudeCLI(
        settings.claude_bin,
        workdir=settings.claude_run_dir,
        model=settings.claude_model,
        effort=settings.claude_effort,
        timeout=settings.claude_timeout_seconds,
        oauth_token=settings.claude_oauth_token,
        auth_label=settings.claude_auth_mode,
    )


def build_assistant(settings: Settings, **kwargs: Any) -> Assistant:
    generator = kwargs.pop("generator", None)
    if generator is None:
        generator = make_claude_cli(settings)
    return Assistant(settings, generator, **kwargs)

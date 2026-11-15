"""Offline test harness: a fake Telegram session, a fake Claude, a fake clock.

No network, no real Claude, no real Telegram. Updates are fed through a real
aiogram ``Dispatcher`` and the real router, exactly as in production.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BusinessConnection, Message, Update

from assistant.config import load_settings
from assistant.handlers import build_router
from assistant.service import Assistant, Preview, build_assistant

TEST_TOKEN = "123456:TESTTOKEN"


class Clock:
    """Deterministic clock passed to the assistant."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start

    def time(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class RecordingSession(BaseSession):
    """A :class:`BaseSession` that records calls and returns canned results."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: List[Any] = []
        self._connection: Optional[BusinessConnection] = None
        self._raise_connection = False
        self._counter = 1000

    # -- test controls --------------------------------------------------
    def set_connection(self, connection: Optional[BusinessConnection]) -> None:
        self._connection = connection
        self._raise_connection = connection is None

    def fail_connection(self) -> None:
        self._connection = None
        self._raise_connection = True

    # -- BaseSession ----------------------------------------------------
    async def close(self) -> None:  # pragma: no cover - nothing to close
        return None

    async def stream_content(self, *args: Any, **kwargs: Any):
        raise AssertionError("stream_content must not be called in tests")

    async def make_request(self, bot: Bot, method: Any, timeout: Optional[int] = None):
        name = type(method).__name__
        self.requests.append(method)

        if name == "GetBusinessConnection":
            if self._connection is not None:
                return self._connection
            raise TelegramBadRequest(method=method, message="connection not found")
        if name == "SendMessage":
            return self._fake_message(method)
        if name == "GetMe":
            return self._fake_me()
        return True

    # -- helpers --------------------------------------------------------
    def _next(self) -> int:
        self._counter += 1
        return self._counter

    def _fake_message(self, method: Any) -> Message:
        return Message.model_validate(
            {
                "message_id": self._next(),
                "date": int(time.time()),
                "chat": {"id": getattr(method, "chat_id", 0), "type": "private"},
                "text": getattr(method, "text", None),
            }
        )

    def _fake_me(self):
        from aiogram.types import User

        return User.model_validate(
            {"id": 42, "is_bot": True, "first_name": "Assistant", "username": "assistant_bot"}
        )

    def requests_of(self, name: str) -> List[Any]:
        return [r for r in self.requests if type(r).__name__ == name]


class FakeGenerator:
    """Yields a scripted sequence of replies or exceptions; records every call."""

    def __init__(self, responses: Optional[List[Any]] = None, hook=None) -> None:
        self.responses = list(responses or [])
        self.calls: List[Dict[str, str]] = []
        self.hook = hook

    async def generate(self, system_prompt: str, conversation: str) -> str:
        self.calls.append({"system": system_prompt, "conversation": conversation})
        if self.hook is not None:
            result = self.hook()
            if asyncio.iscoroutine(result):
                await result
        if not self.responses:
            return "default reply"
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        return item


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        owner_id: int = 111,
        auto_send: bool = False,
        overrides: Optional[Dict[str, str]] = None,
        responses: Optional[List[Any]] = None,
        hook=None,
        clock: Optional[Clock] = None,
    ) -> None:
        self.owner_id = owner_id
        self.data_dir = tmp_path / "data"
        self.profile_path = tmp_path / "profile.md"
        self.profile_path.write_text("# Profile\n- Name: Tester\n", encoding="utf-8")
        self.clock = clock or Clock()
        self._update_id = 1

        merged = {
            "TELEGRAM_BOT_TOKEN": TEST_TOKEN,
            "OWNER_ID": str(owner_id),
            "AUTO_SEND": "true" if auto_send else "false",
            "DATA_DIR": str(self.data_dir),
            "PROFILE_PATH": str(self.profile_path),
        }
        if overrides:
            merged.update(overrides)
        self.settings = load_settings(
            tmp_path / ".env", require_telegram=True, overrides=merged
        )

        self.session = RecordingSession()
        self.bot = Bot(TEST_TOKEN, session=self.session)
        self.previews: List[Preview] = []
        self.generator = FakeGenerator(responses, hook)
        self.assistant = self._new_assistant(self.generator)
        self.dp = self._new_dispatcher(self.assistant)

    # -- wiring ---------------------------------------------------------
    def _new_assistant(self, generator: FakeGenerator) -> Assistant:
        return build_assistant(
            self.settings,
            generator=generator,
            clock=self.clock.time,
            preview_sink=self.previews.append,
        )

    def _new_dispatcher(self, assistant: Assistant) -> Dispatcher:
        dp = Dispatcher()
        dp.include_router(build_router(assistant))
        return dp

    def restart(self, responses: Optional[List[Any]] = None, hook=None) -> Assistant:
        """Rebuild the assistant against the same data dir (as after a restart)."""
        self.generator = FakeGenerator(responses, hook)
        self.assistant = self._new_assistant(self.generator)
        self.dp = self._new_dispatcher(self.assistant)
        return self.assistant

    # -- payload builders ----------------------------------------------
    def _connection_payload(
        self,
        *,
        user_id: Optional[int] = None,
        is_enabled: bool = True,
        can_reply: bool = True,
        connection_id: str = "conn1",
    ) -> Dict[str, Any]:
        uid = self.owner_id if user_id is None else user_id
        return {
            "id": connection_id,
            "user": {"id": uid, "is_bot": False, "first_name": "Owner"},
            "user_chat_id": uid,
            "date": int(self.clock.time()),
            "is_enabled": is_enabled,
            "can_reply": can_reply,  # deprecated field, kept for old aiogram
            "rights": {"can_reply": can_reply},
        }

    def connection(self, **kwargs: Any) -> BusinessConnection:
        return BusinessConnection.model_validate(self._connection_payload(**kwargs))

    def set_connection(self, **kwargs: Any) -> BusinessConnection:
        """Change the Telegram-side connection without feeding an update."""
        conn = self.connection(**kwargs)
        self.session.set_connection(conn)
        return conn

    async def connect(self, **kwargs: Any) -> BusinessConnection:
        """Set the connection AND feed a business_connection update (caches it)."""
        conn = self.set_connection(**kwargs)
        await self.feed({"business_connection": self._connection_payload(**kwargs)})
        return conn

    async def feed(self, body: Dict[str, Any]) -> None:
        data = {"update_id": self._update_id, **body}
        self._update_id += 1
        update = Update.model_validate(data, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, update)

    async def business_message(
        self,
        text: Optional[str] = None,
        *,
        from_id: int = 222,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
        connection_id: str = "conn1",
        is_bot: bool = False,
        via_business_bot: bool = False,
        name: str = "Stranger",
        extra: Optional[Dict[str, Any]] = None,
    ):
        if chat_id is None:
            chat_id = from_id
        if message_id is None:
            message_id = self._update_id * 10
        msg: Dict[str, Any] = {
            "message_id": message_id,
            "date": int(self.clock.time()),
            "chat": {"id": chat_id, "type": "private", "first_name": name},
            "from": {"id": from_id, "is_bot": is_bot, "first_name": name},
            "business_connection_id": connection_id,
        }
        if text is not None:
            msg["text"] = text
        if via_business_bot:
            msg["sender_business_bot"] = {
                "id": 9999,
                "is_bot": True,
                "first_name": "BizBot",
            }
        if extra:
            msg.update(extra)
        await self.feed({"business_message": msg})
        return self.assistant.outcomes[-1] if self.assistant.outcomes else None

    async def direct_message(self, text: str, *, from_id: int):
        msg: Dict[str, Any] = {
            "message_id": self._update_id * 10,
            "date": int(self.clock.time()),
            "chat": {"id": from_id, "type": "private", "first_name": "User"},
            "from": {"id": from_id, "is_bot": False, "first_name": "User"},
            "text": text,
        }
        if text.startswith("/"):
            command = text.split()[0]
            msg["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(command)}
            ]
        await self.feed({"message": msg})


@pytest.fixture
def make_harness(tmp_path):
    created: List[Harness] = []

    def _factory(**kwargs: Any) -> Harness:
        harness = Harness(tmp_path, **kwargs)
        created.append(harness)
        return harness

    yield _factory

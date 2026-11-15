"""End-to-end behaviour tests driven through the real dispatcher/router."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from aiogram.types import Message

from assistant.claude_cli import ClaudeAuthError, ClaudeLimitError, ClaudeTimeout
from assistant.service import Outcome, describe

OWNER = 111
STRANGER = 222

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="POSIX file-permission semantics"
)


def sends(h):
    return h.session.requests_of("SendMessage")


def lookups(h):
    return h.session.requests_of("GetBusinessConnection")


# --------------------------------------------------------------------------- #
# preview mode
# --------------------------------------------------------------------------- #
async def test_preview_shows_but_sends_nothing(make_harness):
    h = make_harness(responses=["salom"])
    await h.connect()
    out = await h.business_message("Salom", from_id=STRANGER)
    assert out == Outcome.PREVIEWED
    assert len(h.previews) == 1 and h.previews[0].reply == "salom"
    assert sends(h) == []
    # unsent reply is NOT stored in history
    history = h.assistant.history.get("conn1", STRANGER)
    assert history[-1]["role"] == "other"


@POSIX_ONLY
async def test_previews_file_is_private(make_harness):
    h = make_harness(responses=["salom"])
    await h.connect()
    await h.business_message("Salom", from_id=STRANGER)
    path = h.data_dir / "previews.jsonl"
    assert path.exists()
    import stat

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert line["reply"] == "salom"


async def test_known_connection_makes_no_telegram_calls(make_harness):
    h = make_harness(responses=["x"])
    await h.connect()
    before = len(h.session.requests)
    await h.business_message("hi", from_id=STRANGER)
    assert len(h.session.requests) == before  # no lookup, no send


async def test_unknown_connection_is_looked_up_once(make_harness):
    h = make_harness(responses=["a", "b"])
    h.set_connection()  # Telegram knows it, but it is not cached
    await h.business_message("hi", from_id=STRANGER, message_id=1)
    await h.business_message("hi again", from_id=STRANGER, message_id=2)
    assert len(lookups(h)) == 1  # cached after the first


async def test_lookup_failure_is_no_connection(make_harness):
    h = make_harness(responses=["a"])
    # no set_connection -> GetBusinessConnection raises
    out = await h.business_message("hi", from_id=STRANGER)
    assert out == Outcome.NO_CONNECTION
    assert h.generator.calls == []


# --------------------------------------------------------------------------- #
# gatekeeping
# --------------------------------------------------------------------------- #
async def test_foreign_account_ignored(make_harness):
    h = make_harness(responses=["a"])
    await h.connect(user_id=999)  # someone else's account
    out = await h.business_message("hi", from_id=STRANGER)
    assert out == Outcome.FOREIGN_CONNECTION
    assert h.generator.calls == []


async def test_disabled_connection_ignored(make_harness):
    h = make_harness(responses=["a"])
    await h.connect(is_enabled=False)
    out = await h.business_message("hi", from_id=STRANGER)
    assert out == Outcome.CONNECTION_DISABLED
    assert h.generator.calls == []


async def test_any_chat_gets_a_reply(make_harness):
    h = make_harness(responses=["1", "2"])
    await h.connect()
    assert await h.business_message("hi", from_id=555, chat_id=555) == Outcome.PREVIEWED
    assert await h.business_message("hi", from_id=777, chat_id=777) == Outcome.PREVIEWED


async def test_bot_messages_ignored(make_harness):
    h = make_harness(responses=["a"])
    await h.connect()
    assert await h.business_message("x", via_business_bot=True) == Outcome.IGNORED_BOT
    assert await h.business_message("x", from_id=333, is_bot=True) == Outcome.IGNORED_BOT
    assert h.generator.calls == []


# --------------------------------------------------------------------------- #
# owner messages & takeover
# --------------------------------------------------------------------------- #
async def test_owner_message_recorded_not_answered(make_harness):
    h = make_harness(responses=["a"])
    await h.connect()
    out = await h.business_message("my own note", from_id=OWNER, chat_id=STRANGER)
    assert out == Outcome.OWNER_MESSAGE
    assert h.generator.calls == []
    history = h.assistant.history.get("conn1", STRANGER)
    assert history[-1]["role"] == "owner"


async def test_takeover_silences_then_resumes(make_harness):
    h = make_harness(responses=["r1", "r2"])
    await h.connect()
    # stranger -> replied
    assert await h.business_message("hi", from_id=STRANGER, chat_id=STRANGER) == Outcome.PREVIEWED
    # owner replies in that chat -> arms takeover
    await h.business_message("got it", from_id=OWNER, chat_id=STRANGER)
    # stranger again -> silenced
    assert await h.business_message("you there?", from_id=STRANGER, chat_id=STRANGER) == Outcome.OWNER_ACTIVE
    assert len(h.generator.calls) == 1
    # time passes beyond takeover window
    h.clock.advance(16 * 60)
    assert await h.business_message("still?", from_id=STRANGER, chat_id=STRANGER) == Outcome.PREVIEWED
    assert len(h.generator.calls) == 2


# --------------------------------------------------------------------------- #
# de-duplication
# --------------------------------------------------------------------------- #
async def test_duplicate_sequential(make_harness):
    h = make_harness(responses=["a"])
    await h.connect()
    assert await h.business_message("hi", from_id=STRANGER, message_id=5) == Outcome.PREVIEWED
    assert await h.business_message("hi", from_id=STRANGER, message_id=5) == Outcome.DUPLICATE
    assert len(h.generator.calls) == 1


async def test_duplicate_parallel(make_harness):
    h = make_harness(responses=["a"])
    await h.connect()
    await asyncio.gather(
        h.business_message("hi", from_id=STRANGER, message_id=9),
        h.business_message("hi", from_id=STRANGER, message_id=9),
    )
    assert len(h.generator.calls) == 1
    assert len(h.previews) == 1


async def test_duplicate_across_restart(make_harness):
    h = make_harness(responses=["a"])
    await h.connect()
    assert await h.business_message("hi", from_id=STRANGER, message_id=7) == Outcome.PREVIEWED
    h.restart(responses=["b"])
    # dedup is checked before the connection lookup, so no connect() needed
    assert await h.business_message("hi", from_id=STRANGER, message_id=7) == Outcome.DUPLICATE


# --------------------------------------------------------------------------- #
# message description
# --------------------------------------------------------------------------- #
async def test_service_message_unsupported(make_harness):
    h = make_harness(responses=["a"])
    await h.connect()
    out = await h.business_message(
        None,
        from_id=STRANGER,
        extra={"new_chat_members": [{"id": 2, "is_bot": False, "first_name": "Y"}]},
    )
    assert out == Outcome.UNSUPPORTED
    assert h.generator.calls == []


def test_describe_sticker_and_voice():
    sticker = Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 1, "type": "private"},
            "sticker": {
                "file_id": "f",
                "file_unique_id": "u",
                "type": "regular",
                "width": 512,
                "height": 512,
                "is_animated": False,
                "is_video": False,
                "emoji": "😀",
            },
        }
    )
    assert describe(sticker) == "[sticker] 😀"
    voice = Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 1, "type": "private"},
            "voice": {"file_id": "f", "file_unique_id": "u", "duration": 3},
        }
    )
    assert describe(voice) == "[voice message]"


# --------------------------------------------------------------------------- #
# history separation & language hints
# --------------------------------------------------------------------------- #
async def test_history_is_per_chat(make_harness):
    h = make_harness(responses=["a", "b"])
    await h.connect()
    await h.business_message("secret-222", from_id=222, chat_id=222)
    await h.business_message("hello-333", from_id=333, chat_id=333)
    convo = h.generator.calls[-1]["conversation"]
    assert "hello-333" in convo
    assert "secret-222" not in convo


async def test_language_hints(make_harness):
    h = make_harness(responses=["1", "2", "3"])
    await h.connect()
    await h.business_message("Привет, как дела?", from_id=STRANGER, chat_id=STRANGER)
    assert "Russian" in h.generator.calls[-1]["conversation"]
    await h.business_message("Hello there friend", from_id=333, chat_id=333)
    assert "English" in h.generator.calls[-1]["conversation"]
    await h.business_message("Salom qalesan", from_id=444, chat_id=444)
    assert "Uzbek" in h.generator.calls[-1]["conversation"]


async def test_emoji_uses_last_language(make_harness):
    h = make_harness(responses=["1", "2"])
    await h.connect()
    await h.business_message("Привет", from_id=STRANGER, chat_id=STRANGER)
    await h.business_message("👍", from_id=STRANGER, chat_id=STRANGER)
    assert "Russian" in h.generator.calls[-1]["conversation"]


async def test_skip_is_not_shown(make_harness):
    h = make_harness(responses=["[SKIP]"])
    await h.connect()
    out = await h.business_message("ok", from_id=STRANGER)
    assert out == Outcome.SKIPPED
    assert h.previews == []
    assert sends(h) == []


# --------------------------------------------------------------------------- #
# auto-send
# --------------------------------------------------------------------------- #
async def test_auto_send_sends_and_stores(make_harness):
    h = make_harness(auto_send=True, responses=["hello reply"])
    await h.connect()
    out = await h.business_message("hi", from_id=STRANGER, chat_id=STRANGER)
    assert out == Outcome.SENT
    # re-fetched the connection, then sent
    assert len(lookups(h)) == 1
    send = sends(h)
    assert len(send) == 1
    assert send[0].business_connection_id == "conn1"
    assert send[0].text == "hello reply"
    # stored as owner
    assert h.assistant.history.get("conn1", STRANGER)[-1]["role"] == "owner"


async def test_auto_send_chunks_long_reply(make_harness):
    long = "X" * 9000
    h = make_harness(auto_send=True, responses=[long])
    await h.connect()
    await h.business_message("hi", from_id=STRANGER)
    send = sends(h)
    assert len(send) == 3
    assert all(len(s.text) <= 4000 for s in send)
    assert "".join(s.text for s in send) == long


async def test_auto_send_blocked_without_can_reply(make_harness):
    h = make_harness(auto_send=True, responses=["r"])
    await h.connect()
    h.set_connection(can_reply=False)  # Telegram now forbids replying
    out = await h.business_message("hi", from_id=STRANGER)
    assert out == Outcome.PERMISSION_DENIED
    assert sends(h) == []


async def test_auto_send_blocked_when_disabled(make_harness):
    h = make_harness(auto_send=True, responses=["r"])
    await h.connect()
    h.set_connection(is_enabled=False)
    out = await h.business_message("hi", from_id=STRANGER)
    assert out == Outcome.PERMISSION_DENIED
    assert sends(h) == []


async def test_auto_send_owner_replies_during_generation(make_harness):
    h = make_harness(auto_send=True)

    async def hook():
        await h.business_message("mine", from_id=OWNER, chat_id=STRANGER)

    h.generator.hook = hook
    h.generator.responses = ["too late"]
    await h.connect()
    out = await h.business_message("hi", from_id=STRANGER, chat_id=STRANGER)
    assert out == Outcome.OWNER_ACTIVE
    assert sends(h) == []


# --------------------------------------------------------------------------- #
# owner commands
# --------------------------------------------------------------------------- #
async def test_pause_resume_owner_only(make_harness):
    h = make_harness(responses=["a", "b"])
    await h.connect()

    await h.direct_message("/pause", from_id=OWNER)
    assert h.assistant.state.paused is True
    assert await h.business_message("hi", from_id=STRANGER) == Outcome.PAUSED

    await h.direct_message("/resume", from_id=OWNER)
    assert h.assistant.state.paused is False

    # a stranger cannot control the bot
    before = len(sends(h))
    await h.direct_message("/pause", from_id=STRANGER)
    assert h.assistant.state.paused is False
    assert len(sends(h)) == before  # no answer to the stranger


async def test_pause_persists_across_restart(make_harness):
    h = make_harness()
    await h.direct_message("/pause", from_id=OWNER)
    h.restart()
    assert h.assistant.state.paused is True


async def test_start_owner_vs_stranger(make_harness):
    h = make_harness(auto_send=True)
    await h.direct_message("/start", from_id=OWNER)
    owner_text = sends(h)[-1].text
    assert "Chatbots" in owner_text
    assert "/pause" in owner_text
    assert "AVTO-YUBORISH" in owner_text

    await h.direct_message("/start", from_id=STRANGER)
    stranger_text = sends(h)[-1].text
    assert "Chatbots" not in stranger_text
    assert "Tester" not in stranger_text  # no profile leakage
    assert h.generator.calls == []


async def test_start_shows_paused(make_harness):
    h = make_harness()
    await h.direct_message("/pause", from_id=OWNER)
    await h.direct_message("/start", from_id=OWNER)
    assert "PAUZADA" in sends(h)[-1].text


# --------------------------------------------------------------------------- #
# usage limit
# --------------------------------------------------------------------------- #
async def test_limit_waits_then_resumes(make_harness):
    reset = 1_000_000 + 3600
    h = make_harness(responses=[ClaudeLimitError("limit", resets_at=reset), "back"])
    await h.connect()
    assert await h.business_message("hi", from_id=STRANGER, message_id=1) == Outcome.LIMITED
    assert len(h.generator.calls) == 1
    # before reset: blocked without calling Claude
    assert await h.business_message("hi", from_id=STRANGER, message_id=2) == Outcome.LIMITED
    assert len(h.generator.calls) == 1
    # after reset: works again
    h.clock.advance(3601)
    assert await h.business_message("hi", from_id=STRANGER, message_id=3) == Outcome.PREVIEWED
    assert len(h.generator.calls) == 2


async def test_limit_cooldown_without_reset(make_harness):
    h = make_harness(responses=[ClaudeLimitError("limit", resets_at=None)])
    await h.connect()
    await h.business_message("hi", from_id=STRANGER)
    expected = h.clock.time() + 30 * 60
    assert abs(h.assistant.state.limit_until - expected) < 2


async def test_limit_persists_across_restart(make_harness):
    reset = 1_000_000 + 3600
    h = make_harness(responses=[ClaudeLimitError("limit", resets_at=reset)])
    await h.connect()
    await h.business_message("hi", from_id=STRANGER, message_id=1)
    h.restart(responses=["x"])
    await h.connect()
    assert await h.business_message("hi", from_id=STRANGER, message_id=2) == Outcome.LIMITED


async def test_limit_notifies_owner_in_auto_send(make_harness):
    h = make_harness(auto_send=True, responses=[ClaudeLimitError("limit", resets_at=1_000_000 + 10)])
    await h.connect()
    await h.business_message("hi", from_id=STRANGER)
    owner_sends = [s for s in sends(h) if s.chat_id == OWNER]
    assert len(owner_sends) == 1


async def test_resume_clears_limit(make_harness):
    reset = 1_000_000 + 3600
    h = make_harness(responses=[ClaudeLimitError("limit", resets_at=reset), "back"])
    await h.connect()
    await h.business_message("hi", from_id=STRANGER, message_id=1)
    await h.direct_message("/resume", from_id=OWNER)
    assert h.assistant.state.limit_until == 0.0
    assert await h.business_message("hi", from_id=STRANGER, message_id=2) == Outcome.PREVIEWED


# --------------------------------------------------------------------------- #
# timeout / auth errors
# --------------------------------------------------------------------------- #
async def test_timeout_sends_nothing(make_harness):
    h = make_harness(responses=[ClaudeTimeout("t")])
    await h.connect()
    out = await h.business_message("hi", from_id=STRANGER)
    assert out == Outcome.ERROR
    assert sends(h) == []
    assert h.previews == []


async def test_auth_error_does_not_spam_owner(make_harness):
    h = make_harness(
        auto_send=True,
        responses=[ClaudeAuthError("bad", detail="x"), ClaudeAuthError("bad", detail="x")],
    )
    await h.connect()
    await h.business_message("hi", from_id=STRANGER, message_id=1)
    await h.business_message("hi", from_id=333, chat_id=333, message_id=2)
    owner_sends = [s for s in sends(h) if s.chat_id == OWNER]
    assert len(owner_sends) == 1  # throttled to one DM

"""Entry point for the Telegram Business personal-assistant bot.

Modes:
    python bot.py                     run the bot (reads .env)
    python bot.py --settings-from-env run the bot from environment variables
                                      (container / Railway); the variables are
                                      scrubbed from os.environ after loading
    python bot.py --check-claude      verify Claude auth/isolation, no Telegram
    python bot.py --try "text"        draft a reply to an invented message

``--check-claude`` and ``--try`` do not require Telegram settings.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import sys
from pathlib import Path

from assistant.claude_cli import ClaudeError
from assistant.config import (
    ConfigError,
    SETTING_KEYS,
    Settings,
    env_file_warning,
    load_settings,
)
from assistant.handlers import ALLOWED_UPDATES, build_router
from assistant.language import detect_language
from assistant.prompts import build_system_prompt, build_user_prompt
from assistant.service import build_assistant, make_claude_cli, read_profile
from assistant.store import ensure_private_dir

log = logging.getLogger("bot")

ENV_FILE = Path(__file__).resolve().parent / ".env"

# apiKeySource values that mean "not the subscription" -> --check-claude fails.
_BAD_API_KEY_SOURCES = {"ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key"}
_OK_API_PROVIDERS = {"firstParty", None}


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def settings_from_process_env(*, require_telegram: bool) -> Settings:
    """Read settings from the process environment, then scrub those keys.

    The scrub happens even on error so the ``claude`` child process can never
    inherit the bot token or the Claude token from ``os.environ``.
    """
    source = {key: os.environ.get(key) for key in SETTING_KEYS}
    try:
        return load_settings(ENV_FILE, require_telegram=require_telegram, source=source)
    finally:
        for key in SETTING_KEYS:
            os.environ.pop(key, None)


def _build_settings(args: argparse.Namespace, *, require_telegram: bool) -> Settings:
    if args.settings_from_env:
        return settings_from_process_env(require_telegram=require_telegram)
    return load_settings(ENV_FILE, require_telegram=require_telegram)


async def check_claude(settings: Settings) -> int:
    cli = make_claude_cli(settings)
    try:
        info = await cli.inspect()
    except ClaudeError as exc:
        print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    printable = {k: v for k, v in info.items() if k != "raw_init"}
    print(json.dumps(printable, indent=2, ensure_ascii=False))

    init = info["init"]
    problems = []
    if not info.get("raw_init"):
        problems.append("no init event from the CLI")
    for key in ("tools", "mcp_servers", "plugins", "skills"):
        if init.get(key):
            problems.append(f"{key} is not empty: {init.get(key)}")
    api_key_source = init.get("apiKeySource")
    if api_key_source in _BAD_API_KEY_SOURCES:
        problems.append(f"apiKeySource is {api_key_source!r} (an API key, not the subscription)")
    api_provider = init.get("apiProvider")
    if api_provider not in _OK_API_PROVIDERS:
        problems.append(f"apiProvider is {api_provider!r} (expected firstParty)")

    if problems:
        print("FAILED:\n- " + "\n- ".join(problems), file=sys.stderr)
        return 1

    print(
        f"OK — using {info['auth']} "
        f"(apiKeySource={api_key_source!r}, apiProvider={api_provider!r})."
    )
    return 0


async def try_reply(settings: Settings, text: str) -> int:
    language = detect_language(text)
    cli = make_claude_cli(settings)
    system_prompt = build_system_prompt(
        read_profile(settings.profile_path), _dt.datetime.now()
    )
    items = [{"role": "other", "name": "Tester", "text": text, "lang": language}]
    user_prompt = build_user_prompt(items, language)
    try:
        reply = await cli.generate(system_prompt, user_prompt)
    except ClaudeError as exc:
        print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {"language": language, "message": text, "reply": reply},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


async def run_bot(settings: Settings) -> int:
    from aiogram import Bot, Dispatcher
    from aiogram.exceptions import TelegramUnauthorizedError

    ensure_private_dir(settings.data_dir)
    ensure_private_dir(settings.claude_run_dir)

    if not settings.from_environment:
        warning = env_file_warning(ENV_FILE)
        if warning:
            log.warning(warning)

    bot = Bot(settings.bot_token)
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        print(
            "Telegram rejected TELEGRAM_BOT_TOKEN (unauthorized). "
            "Check the token from @BotFather.",
            file=sys.stderr,
        )
        await bot.session.close()
        return 2

    assistant = build_assistant(settings)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(assistant))

    mode = "AUTO-SEND" if settings.auto_send else "PREVIEW"
    log.info("Bot @%s is running.", me.username)
    if settings.auto_send:
        log.warning("Mode: AUTO-SEND — replies are sent automatically.")
    else:
        log.info("Mode: PREVIEW — replies are only shown in the logs.")
    log.info("Claude auth: %s", settings.claude_auth_mode)
    log.info("Claude binary: %s", settings.claude_bin)
    if assistant.state.paused:
        log.warning("The assistant starts PAUSED (/resume to re-enable).")
    if not read_profile(settings.profile_path):
        log.warning("profile.md is empty — fill it in for good replies.")

    try:
        await dispatcher.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        await bot.session.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Telegram Business assistant bot.")
    parser.add_argument(
        "--settings-from-env",
        action="store_true",
        help="read settings from environment variables (container mode)",
    )
    parser.add_argument(
        "--check-claude",
        action="store_true",
        help="verify Claude auth and isolation, then exit",
    )
    parser.add_argument(
        "--try",
        dest="try_text",
        metavar="TEXT",
        help="draft a reply to an invented message, then exit",
    )
    args = parser.parse_args(argv)

    configure_logging()

    try:
        if args.check_claude:
            settings = _build_settings(args, require_telegram=False)
            return asyncio.run(check_claude(settings))
        if args.try_text is not None:
            settings = _build_settings(args, require_telegram=False)
            return asyncio.run(try_reply(settings, args.try_text))
        settings = _build_settings(args, require_telegram=True)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    return asyncio.run(run_bot(settings))


if __name__ == "__main__":
    sys.exit(main())

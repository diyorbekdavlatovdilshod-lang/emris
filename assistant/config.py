"""Settings loading and validation.

Settings come from exactly one source, never a mix:

* **Local**: the ``.env`` file only, parsed with :func:`dotenv_values`. The
  real shell environment is ignored on purpose, so a stray exported variable
  can never leak into the bot.
* **Container (Railway)**: ``bot.py --settings-from-env`` reads the environment
  variables, passes them in as ``source=`` and then wipes them from
  ``os.environ`` (see ``bot.py``). In this mode ``.env`` is not read at all.

We never write to ``os.environ`` here, so secrets never spread to child
processes except where we explicitly pass them (see ``claude_cli``).
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional

from dotenv import dotenv_values

# Project root = the folder that contains the ``assistant`` package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Every key bot.py reads from the process environment and then scrubs.
SETTING_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "OWNER_ID",
    "AUTO_SEND",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_BIN",
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "CLAUDE_TIMEOUT_SECONDS",
    "LIMIT_COOLDOWN_MINUTES",
    "OWNER_TAKEOVER_MINUTES",
    "HISTORY_LIMIT",
    "DATA_DIR",
    "PROFILE_PATH",
]

_BOOL_TRUE = {"true", "1", "yes", "on"}
_BOOL_FALSE = {"false", "0", "no", "off"}
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")


class ConfigError(Exception):
    """Raised for any invalid or missing setting. Never carries a secret."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(default="", repr=False)
    owner_id: Optional[int] = None
    auto_send: bool = False
    claude_oauth_token: Optional[str] = field(default=None, repr=False)
    claude_bin: str = "claude"
    claude_model: Optional[str] = None
    claude_effort: str = "low"
    claude_timeout_seconds: int = 120
    limit_cooldown_minutes: int = 30
    owner_takeover_minutes: int = 15
    history_limit: int = 30
    data_dir: Path = PROJECT_ROOT / "data"
    profile_path: Path = PROJECT_ROOT / "profile.md"
    from_environment: bool = False

    @property
    def claude_auth_mode(self) -> str:
        if not self.claude_oauth_token:
            return "this machine's claude login"
        if self.from_environment:
            return "subscription token from environment variables"
        return "subscription token from .env"

    @property
    def claude_run_dir(self) -> Path:
        return self.data_dir / "claude-run"


def _clean(value: Optional[object]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _location(from_environment: bool, env_file: Optional[Path]) -> str:
    if from_environment:
        return "the environment variables"
    return str(env_file) if env_file is not None else ".env"


def _resolve_path(value: Optional[str], base: Path, default: Path) -> Path:
    if not value:
        return default
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def load_settings(
    env_file,
    *,
    require_telegram: bool = True,
    overrides: Optional[Mapping[str, object]] = None,
    source: Optional[Mapping[str, object]] = None,
) -> Settings:
    """Load and validate settings from ``.env`` or an explicit ``source`` map."""
    env_path = Path(env_file) if env_file is not None else None
    from_environment = source is not None
    env_missing = False

    raw: Dict[str, Optional[str]] = {}
    if from_environment:
        for key in SETTING_KEYS:
            raw[key] = _clean(source.get(key))  # type: ignore[union-attr]
    else:
        if env_path is None:
            env_missing = True
        elif not env_path.exists():
            env_missing = True
        else:
            parsed = dotenv_values(str(env_path))
            for key in SETTING_KEYS:
                raw[key] = _clean(parsed.get(key))

    if overrides:
        for key, value in overrides.items():
            raw[key] = _clean(value)

    location = _location(from_environment, env_path)

    def missing(key: str) -> ConfigError:
        msg = f"{key} is not set. Set it in {location}."
        if env_missing and not from_environment:
            msg += (
                " The .env file does not exist yet — run: "
                "cp .env.example .env && chmod 600 .env"
            )
        return ConfigError(msg)

    # --- TELEGRAM_BOT_TOKEN -------------------------------------------------
    bot_token = raw.get("TELEGRAM_BOT_TOKEN")
    if require_telegram:
        if not bot_token:
            raise missing("TELEGRAM_BOT_TOKEN")
        if not _BOT_TOKEN_RE.match(bot_token):
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN is malformed: expected <digits>:<text>. "
                f"Check it in {location}."
            )
    bot_token = bot_token or ""

    # --- OWNER_ID -----------------------------------------------------------
    owner_raw = raw.get("OWNER_ID")
    owner_id: Optional[int] = None
    if require_telegram:
        if not owner_raw:
            raise missing("OWNER_ID")
        if not owner_raw.isdigit():
            raise ConfigError(
                f"OWNER_ID must be a number (your Telegram user id). Check {location}."
            )
        owner_id = int(owner_raw)
    elif owner_raw:
        if not owner_raw.isdigit():
            raise ConfigError(
                f"OWNER_ID must be a number (your Telegram user id). Check {location}."
            )
        owner_id = int(owner_raw)

    # --- AUTO_SEND ----------------------------------------------------------
    auto_send = _parse_bool(raw.get("AUTO_SEND"), "AUTO_SEND", location, default=False)

    # --- CLAUDE_CODE_OAUTH_TOKEN -------------------------------------------
    claude_token = raw.get("CLAUDE_CODE_OAUTH_TOKEN")
    if claude_token:
        if claude_token.startswith("sk-ant-api"):
            raise ConfigError(
                "CLAUDE_CODE_OAUTH_TOKEN looks like an API key (sk-ant-api...), "
                "not a subscription token. Use the token from `claude setup-token` "
                f"(sk-ant-oat...). Check {location}."
            )
        if (not claude_token.startswith("sk-ant-oat")) or any(
            ch.isspace() for ch in claude_token
        ):
            raise ConfigError(
                "CLAUDE_CODE_OAUTH_TOKEN is malformed: it must start with "
                "sk-ant-oat and contain no spaces or line breaks (paste it as a "
                f"single line). Check {location}."
            )

    # --- CLAUDE_BIN ---------------------------------------------------------
    claude_bin = raw.get("CLAUDE_BIN")
    if not claude_bin:
        found = shutil.which("claude")
        if not found:
            # Fall back to the installer's default location. On Windows the
            # binary is claude.exe and is often not on PATH in a fresh shell.
            for candidate in (
                Path.home() / ".local" / "bin" / "claude.exe",
                Path.home() / ".local" / "bin" / "claude",
            ):
                if candidate.exists():
                    found = str(candidate)
                    break
        claude_bin = found or str(Path.home() / ".local" / "bin" / "claude")

    # --- CLAUDE_MODEL -------------------------------------------------------
    claude_model = raw.get("CLAUDE_MODEL")  # None => CLI default

    # --- CLAUDE_EFFORT ------------------------------------------------------
    effort = (raw.get("CLAUDE_EFFORT") or "low").lower()
    if effort not in _EFFORTS:
        raise ConfigError(
            "CLAUDE_EFFORT must be one of low, medium, high, xhigh, max. "
            f"Check {location}."
        )

    # --- numeric settings ---------------------------------------------------
    timeout = _parse_int(
        raw.get("CLAUDE_TIMEOUT_SECONDS"), "CLAUDE_TIMEOUT_SECONDS", location, 120, 1
    )
    cooldown = _parse_int(
        raw.get("LIMIT_COOLDOWN_MINUTES"), "LIMIT_COOLDOWN_MINUTES", location, 30, 1
    )
    takeover = _parse_int(
        raw.get("OWNER_TAKEOVER_MINUTES"), "OWNER_TAKEOVER_MINUTES", location, 15, 0
    )
    history_limit = _parse_int(
        raw.get("HISTORY_LIMIT"), "HISTORY_LIMIT", location, 30, 1
    )

    # --- paths --------------------------------------------------------------
    base = env_path.parent if env_path is not None else PROJECT_ROOT
    data_dir = _resolve_path(raw.get("DATA_DIR"), base, PROJECT_ROOT / "data")
    profile_path = _resolve_path(
        raw.get("PROFILE_PATH"), base, PROJECT_ROOT / "profile.md"
    )

    return Settings(
        bot_token=bot_token,
        owner_id=owner_id,
        auto_send=auto_send,
        claude_oauth_token=claude_token or None,
        claude_bin=claude_bin,
        claude_model=claude_model,
        claude_effort=effort,
        claude_timeout_seconds=timeout,
        limit_cooldown_minutes=cooldown,
        owner_takeover_minutes=takeover,
        history_limit=history_limit,
        data_dir=data_dir,
        profile_path=profile_path,
        from_environment=from_environment,
    )


def _parse_bool(value: Optional[str], key: str, location: str, *, default: bool) -> bool:
    if value is None:
        return default
    lowered = value.lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    raise ConfigError(
        f"{key} must be one of true/false/1/0/yes/no/on/off. Check {location}."
    )


def _parse_int(
    value: Optional[str], key: str, location: str, default: int, minimum: int
) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(f"{key} must be an integer. Check {location}.") from None
    if number < minimum:
        raise ConfigError(f"{key} must be >= {minimum}. Check {location}.")
    return number


def env_file_warning(path: Path) -> Optional[str]:
    """Return a chmod warning if ``.env`` is readable by group/other, else None."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & 0o077:
        return (
            f"{path} is readable by other users. It holds secret tokens — "
            f"run: chmod 600 {path}"
        )
    return None

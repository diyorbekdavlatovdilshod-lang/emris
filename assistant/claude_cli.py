"""Run Claude Code (``claude -p``) as an isolated subprocess, once per reply.

Design goals:

* Use the owner's **subscription** (OAuth token or local ``claude`` login),
  never an Anthropic API key.
* Keep the subprocess sandboxed: no tools, no MCP, no plugins, no hooks, no
  session persistence, no slash commands.
* Never let a secret reach ``argv``, ``ps``, logs or exceptions. The incoming
  message travels only on stdin; the system prompt (with the profile) is a
  0600 temp file; tokens are redacted from all captured output.

The process is started in its own session/group so a timeout can kill the
whole tree (including any grandchildren). This is POSIX-accurate and degrades
gracefully on Windows.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .store import ensure_private_dir

_IS_POSIX = os.name == "posix"

# Builtin plugins to force-disable. The authoritative list is read at runtime
# from the stream-json ``init`` event (see ``inspect``); these are the known
# defaults we disable up front.
KNOWN_PLUGINS = [
    "cc-plugin-agents-md@builtin",
    "cc-plugin-telemetry@builtin",
    "cc-plugin-privacy@builtin",
    "cc-plugin-plugin-authoring@builtin",
]

ISOLATION_SETTINGS: Dict[str, Any] = {
    "disableAllHooks": True,
    "enabledPlugins": {name: False for name in KNOWN_PLUGINS},
}

_LIMIT_RE = re.compile(
    r"usage limit|hit your (usage )?limit|limit reached|rate[ _-]?limit|"
    r"out of (extra )?usage|quota",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"not logged in|/login|invalid api key|authentication|unauthori[sz]ed|oauth token",
    re.IGNORECASE,
)
_EPOCH_RE = re.compile(r"\|(\d{9,})")
_RESET_TEXT_RE = re.compile(r"resets?\s+([^\n|]{1,48})", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class ClaudeError(Exception):
    """Base error; its message is safe to show to the owner."""


class ClaudeTimeout(ClaudeError):
    """The subprocess exceeded its timeout and was killed."""


class ClaudeAuthError(ClaudeError):
    """Not authenticated (bad/expired token or missing login)."""

    def __init__(self, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message)
        self.detail = detail


class ClaudeLimitError(ClaudeError):
    """The subscription usage limit was reached."""

    def __init__(
        self,
        detail: str,
        resets_at: Optional[int] = None,
        reset_text: Optional[str] = None,
    ) -> None:
        self.detail = detail
        self.resets_at = resets_at
        self.reset_text = reset_text
        super().__init__(self._format())

    def _format(self) -> str:
        if self.reset_text:
            when = self.reset_text.strip()
        elif self.resets_at:
            when = _dt.datetime.fromtimestamp(self.resets_at).strftime(
                "%Y-%m-%d %H:%M"
            )
        else:
            when = "unknown"
        return f"Claude subscription usage limit reached (resets {when})"


# --------------------------------------------------------------------------- #
# Pure helpers (classify / parse) — unit tested on their own
# --------------------------------------------------------------------------- #
def classify_failure(message: str, status: Optional[int] = None) -> ClaudeError:
    """Map a failure message (+ optional HTTP status) to an exception.

    Returns the exception; it does not raise. Never inspects secrets.
    """
    text = message or ""

    is_limit = status == 429 or bool(_LIMIT_RE.search(text))
    if is_limit:
        resets_at: Optional[int] = None
        epoch_match = _EPOCH_RE.search(text)
        if epoch_match:
            resets_at = int(epoch_match.group(1))
        reset_text = None
        reset_match = _RESET_TEXT_RE.search(text)
        if reset_match:
            reset_text = reset_match.group(1).strip()
        detail = _EPOCH_RE.sub("", text).strip()
        return ClaudeLimitError(detail=detail[:500], resets_at=resets_at, reset_text=reset_text)

    is_auth = status in (401, 403) or bool(_AUTH_RE.search(text))
    if is_auth:
        return ClaudeAuthError(
            "Claude is not logged in. On a machine with a browser run `claude` "
            "and then `/login` (or set CLAUDE_CODE_OAUTH_TOKEN).",
            detail=text[:300],
        )

    return ClaudeError(text[:500] or "Claude failed with no output.")


def _find_status(obj: Any) -> Optional[int]:
    """Best-effort HTTP status extraction from a parsed JSON result."""
    if isinstance(obj, dict):
        for key in ("status", "status_code", "api_error_status", "statusCode"):
            value = obj.get(key)
            if isinstance(value, int):
                return value
        for value in obj.values():
            found = _find_status(value)
            if found is not None:
                return found
    return None


def parse_result(returncode: int, stdout: str, stderr: str) -> str:
    """Parse a ``--output-format json`` run; return the reply or raise."""
    last_line = ""
    for line in (stdout or "").splitlines():
        if line.strip():
            last_line = line.strip()

    if last_line:
        try:
            parsed = json.loads(last_line)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            is_error = bool(parsed.get("is_error"))
            subtype = parsed.get("subtype")
            result_text = parsed.get("result")
            if is_error or (subtype is not None and subtype != "success"):
                combined = f"{result_text or ''}\n{stderr or ''}".strip()
                raise classify_failure(combined, _find_status(parsed))
            if not result_text:
                raise ClaudeError("Claude returned an empty result.")
            return str(result_text).strip()

    # Not JSON: build a message from whatever we have.
    message = (stderr or "").strip() or (stdout or "").strip()
    if not message:
        message = f"exit code {returncode}"
    raise classify_failure(message)


# --------------------------------------------------------------------------- #
# Command / environment building
# --------------------------------------------------------------------------- #
def build_argv(
    claude_bin: str,
    *,
    system_prompt_file: str,
    effort: str,
    model: Optional[str],
    output_format: str,
) -> List[str]:
    """Build the exact ``claude`` command line (order matters)."""
    argv = [
        claude_bin,
        "-p",
        "--safe-mode",
        "--settings",
        json.dumps(ISOLATION_SETTINGS),
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt-file",
        system_prompt_file,
        "--output-format",
        output_format,
        "--effort",
        effort,
    ]
    if output_format == "stream-json":
        argv.append("--verbose")
    if model:
        argv += ["--model", model]
    return argv


_STRIP_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SIMPLE",
)


def _proc_group_kwargs() -> Dict[str, Any]:
    if _IS_POSIX:
        return {"start_new_session": True}
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return {"creationflags": flags}


# --------------------------------------------------------------------------- #
# The CLI wrapper
# --------------------------------------------------------------------------- #
class ClaudeCLI:
    def __init__(
        self,
        claude_bin: str,
        *,
        workdir: Path,
        model: Optional[str],
        effort: str,
        timeout: int,
        oauth_token: Optional[str],
        auth_label: Optional[str] = None,
    ) -> None:
        self._claude_bin = claude_bin
        self._workdir = Path(workdir)
        self._model = model
        self._effort = effort
        self._timeout = int(timeout)
        self._oauth_token = oauth_token or None
        self._auth_label = auth_label

    def __repr__(self) -> str:  # never leaks the token
        return (
            f"ClaudeCLI(claude_bin={self._claude_bin!r}, model={self._model!r}, "
            f"effort={self._effort!r}, timeout={self._timeout})"
        )

    @property
    def auth_mode(self) -> str:
        if self._auth_label:
            return self._auth_label
        return (
            "subscription token" if self._oauth_token else "this machine's claude login"
        )

    def build_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("ANTHROPIC_"):
                del env[key]
        for key in _STRIP_ENV:
            env.pop(key, None)
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
        if self._oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self._oauth_token
        return env

    def _redact(self, text: str) -> str:
        if self._oauth_token and self._oauth_token in text:
            text = text.replace(self._oauth_token, "[REDACTED]")
        return text

    def _kill(self, proc: "asyncio.subprocess.Process") -> None:
        if proc.returncode is not None:
            return
        try:
            if _IS_POSIX:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                # Kill the whole tree on Windows.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                )
        except (ProcessLookupError, OSError):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass

    async def _spawn(
        self, argv: List[str], stdin_text: str
    ) -> Tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._workdir),
                env=self.build_env(),
                **_proc_group_kwargs(),
            )
        except FileNotFoundError as exc:
            raise ClaudeError(
                f"The `claude` binary was not found at {self._claude_bin!r}. "
                "Install Claude Code or set CLAUDE_BIN."
            ) from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(stdin_text.encode("utf-8")), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            self._kill(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            raise ClaudeTimeout(
                f"Claude timed out after {self._timeout}s and was stopped."
            )
        except asyncio.CancelledError:
            self._kill(proc)
            raise

        stdout = self._redact(stdout_b.decode("utf-8", errors="replace"))
        stderr = self._redact(stderr_b.decode("utf-8", errors="replace"))
        return proc.returncode, stdout, stderr

    async def _run_with_prompt(
        self, system_prompt: str, stdin_text: str, output_format: str
    ) -> Tuple[int, str, str]:
        ensure_private_dir(self._workdir)
        fd, sp_path = tempfile.mkstemp(
            dir=str(self._workdir), prefix="sys-", suffix=".txt"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(system_prompt)
            try:
                os.chmod(sp_path, 0o600)
            except (OSError, NotImplementedError):
                pass
            argv = build_argv(
                self._claude_bin,
                system_prompt_file=sp_path,
                effort=self._effort,
                model=self._model,
                output_format=output_format,
            )
            return await self._spawn(argv, stdin_text)
        finally:
            try:
                os.unlink(sp_path)
            except OSError:
                pass

    async def generate(self, system_prompt: str, conversation: str) -> str:
        """Run one reply generation; return the reply text or raise."""
        returncode, stdout, stderr = await self._run_with_prompt(
            system_prompt, conversation, "json"
        )
        try:
            return parse_result(returncode, stdout, stderr)
        except ClaudeAuthError as exc:
            if self._oauth_token:
                raise ClaudeAuthError(
                    "The subscription token was rejected or has expired. On a "
                    "machine with a browser run `claude setup-token` and update "
                    f"CLAUDE_CODE_OAUTH_TOKEN. CLI said: {exc.detail or ''}".strip(),
                    detail=exc.detail,
                ) from exc
            raise

    async def inspect(self) -> Dict[str, Any]:
        """Send a tiny probe and report auth + the init event + the reply."""
        returncode, stdout, stderr = await self._run_with_prompt(
            "Reply with exactly: OK", "ping", "stream-json"
        )
        init: Dict[str, Any] = {}
        reply = ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                init = event
            elif event.get("type") == "result":
                reply = str(event.get("result") or "").strip()

        return {
            "auth": self.auth_mode,
            "init": {
                "tools": init.get("tools", []),
                "mcp_servers": init.get("mcp_servers", []),
                "plugins": init.get("plugins", []),
                "skills": init.get("skills", init.get("slash_commands_skills", [])),
                "apiKeySource": init.get("apiKeySource"),
                "apiProvider": init.get("apiProvider"),
                "model": init.get("model"),
            },
            "reply": reply,
            "returncode": returncode,
            "stderr": stderr[:500],
            "raw_init": bool(init),
        }

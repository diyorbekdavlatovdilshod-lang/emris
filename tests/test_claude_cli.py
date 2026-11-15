"""Tests for the Claude subprocess wrapper, using a fake ``claude`` binary.

The fake is a small Python program invoked exactly like the real CLI. It
records argv, stdin, env, cwd, the run directory contents, its pid and the
system-prompt file (with permissions), then emits output according to
``FAKE_MODE``. On POSIX it is an executable script; on Windows a ``.cmd``
wrapper forwards to the Python interpreter (argv, empty strings and JSON all
survive, verified separately).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from assistant.claude_cli import (
    ClaudeAuthError,
    ClaudeCLI,
    ClaudeError,
    ClaudeLimitError,
    ClaudeTimeout,
    classify_failure,
    parse_result,
)

POSIX = os.name == "posix"
POSIX_ONLY = pytest.mark.skipif(not POSIX, reason="POSIX permission/kill semantics")

FAKE_SRC = r'''
import json, os, stat, subprocess, sys, time

def getflag(name):
    a = sys.argv
    for i, v in enumerate(a):
        if v == name and i + 1 < len(a):
            return a[i + 1]
    return None

mode = os.environ.get("FAKE_MODE", "ok")
record_path = os.environ.get("FAKE_RECORD")
data = sys.stdin.read()
argv = sys.argv[1:]
sp_path = getflag("--system-prompt-file")
out_fmt = getflag("--output-format") or "json"

sp_content = sp_mode = None
if sp_path and os.path.exists(sp_path):
    try:
        with open(sp_path, encoding="utf-8") as fh:
            sp_content = fh.read()
        sp_mode = oct(stat.S_IMODE(os.stat(sp_path).st_mode))
    except OSError:
        pass

WATCH = ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "TELEGRAM_BOT_TOKEN",
         "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_USE_BEDROCK",
         "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE",
         "CLAUDE_CODE_ENTRYPOINT", "HOME", "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
         "CLAUDE_CODE_DISABLE_CLAUDE_MDS"]

if record_path:
    rec = {"argv": argv, "stdin": data, "cwd": os.getcwd(), "pid": os.getpid(),
           "dir_entries": sorted(os.listdir(os.getcwd())),
           "sysprompt_path": sp_path, "sysprompt_mode": sp_mode,
           "sysprompt_content": sp_content,
           "env": {k: os.environ.get(k) for k in WATCH}, "mode": mode}
    try:
        with open(record_path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
    except OSError:
        pass

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

if mode == "ok":
    if out_fmt == "stream-json":
        emit({"type": "system", "subtype": "init", "tools": [], "mcp_servers": [],
              "plugins": [], "skills": [], "apiKeySource": "none",
              "apiProvider": "firstParty", "model": "claude-test"})
        emit({"type": "result", "subtype": "success", "is_error": False, "result": "OK"})
    else:
        emit({"type": "result", "subtype": "success", "is_error": False,
              "result": "  OK reply  "})
elif mode == "empty":
    emit({"type": "result", "subtype": "success", "is_error": False, "result": ""})
elif mode == "limit_epoch":
    emit({"type": "result", "subtype": "error", "is_error": True,
          "result": "You have reached your usage limit. |1760000000"})
elif mode == "limit_text":
    emit({"type": "result", "subtype": "error", "is_error": True,
          "result": "Usage limit reached, resets 3pm"})
elif mode == "auth":
    emit({"type": "result", "subtype": "error", "is_error": True,
          "result": "Invalid API key / not logged in. token sk-ant-oat01-SECRETVALUE"})
elif mode == "crash":
    sys.stderr.write("boom: something broke\n")
    sys.exit(3)
elif mode == "echo_token":
    emit({"type": "result", "subtype": "success", "is_error": False,
          "result": "token=" + os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "<none>")})
elif mode == "sleep":
    marker = os.environ.get("FAKE_MARKER")
    if marker:
        subprocess.Popen([sys.executable, "-c",
            "import time,sys;time.sleep(3);open(sys.argv[1],'w').write('x')", marker])
    time.sleep(8)
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "late"})
else:
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "OK reply"})
'''


class Fake:
    def __init__(self, bin_path: str, record: Path, workdir: Path) -> None:
        self.bin = bin_path
        self.record = record
        self.workdir = workdir

    def read(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    home = tmp_path / "fake"
    home.mkdir()
    script = home / "fake_claude.py"
    record = tmp_path / "record.json"
    workdir = tmp_path / "run"

    if POSIX:
        script.write_text("#!" + sys.executable + "\n" + FAKE_SRC, encoding="utf-8")
        os.chmod(script, 0o755)
        bin_path = str(script)
    else:
        script.write_text(FAKE_SRC, encoding="utf-8")
        cmd = home / "claude.cmd"
        cmd.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
        bin_path = str(cmd)

    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("FAKE_MODE", "ok")
    return Fake(bin_path, record, workdir)


def make_cli(fake: Fake, *, token=None, timeout=120, model=None) -> ClaudeCLI:
    return ClaudeCLI(
        fake.bin,
        workdir=fake.workdir,
        model=model,
        effort="low",
        timeout=timeout,
        oauth_token=token,
    )


SENTINEL = "PROFILE-SENTINEL-9ac3"
SYSTEM = f"You are a test. {SENTINEL}"


# --------------------------------------------------------------------------- #
# happy path & argv/stdin hygiene
# --------------------------------------------------------------------------- #
async def test_result_is_stripped(fake_claude):
    reply = await make_cli(fake_claude).generate(SYSTEM, "hi")
    assert reply == "OK reply"


async def test_message_only_on_stdin_profile_in_file(fake_claude):
    await make_cli(fake_claude).generate(SYSTEM, "SECRET-CONVERSATION")
    rec = fake_claude.read()
    # conversation is on stdin, not argv
    assert rec["stdin"] == "SECRET-CONVERSATION"
    assert "SECRET-CONVERSATION" not in " ".join(rec["argv"])
    # profile/system prompt is in the temp file, not argv
    assert SENTINEL in rec["sysprompt_content"]
    assert SENTINEL not in " ".join(rec["argv"])


@POSIX_ONLY
async def test_system_prompt_file_is_0600(fake_claude):
    await make_cli(fake_claude).generate(SYSTEM, "hi")
    assert fake_claude.read()["sysprompt_mode"] == "0o600"


async def test_temp_file_removed_after_run(fake_claude):
    await make_cli(fake_claude).generate(SYSTEM, "hi")
    assert os.listdir(fake_claude.workdir) == []


async def test_temp_file_removed_on_error(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "crash")
    with pytest.raises(ClaudeError):
        await make_cli(fake_claude).generate(SYSTEM, "hi")
    assert os.listdir(fake_claude.workdir) == []


async def test_no_shell_injection(fake_claude):
    await make_cli(fake_claude).generate(SYSTEM, "$(touch pwned) `touch pwned2`")
    assert not (fake_claude.workdir / "pwned").exists()
    assert not (fake_claude.workdir / "pwned2").exists()


# --------------------------------------------------------------------------- #
# isolation flags & settings
# --------------------------------------------------------------------------- #
async def test_isolation_flags_and_order(fake_claude):
    await make_cli(fake_claude).generate(SYSTEM, "hi")
    argv = fake_claude.read()["argv"]
    assert argv[0] == "-p"
    for flag in (
        "--safe-mode",
        "--settings",
        "--tools",
        "--strict-mcp-config",
        "--setting-sources",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt-file",
        "--output-format",
        "--effort",
    ):
        assert flag in argv
    # relative order of a few key flags
    assert argv.index("--safe-mode") < argv.index("--system-prompt-file")
    assert argv.index("--system-prompt-file") < argv.index("--output-format")
    # never present
    assert "--mcp-config" not in argv
    assert "--bare" not in argv
    # settings JSON disables hooks and all plugins
    settings_json = json.loads(argv[argv.index("--settings") + 1])
    assert settings_json["disableAllHooks"] is True
    assert settings_json["enabledPlugins"]
    assert all(v is False for v in settings_json["enabledPlugins"].values())


async def test_model_flag_added_only_when_set(fake_claude):
    await make_cli(fake_claude, model="claude-x").generate(SYSTEM, "hi")
    argv = fake_claude.read()["argv"]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "claude-x"


# --------------------------------------------------------------------------- #
# child environment
# --------------------------------------------------------------------------- #
async def test_child_env_is_scrubbed(fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-PARENT")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:PARENT")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-PARENT")
    monkeypatch.setenv("CLAUDE_CODE_SIMPLE", "1")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("CLAUDE_CODE_USE_FOUNDRY", "1")
    monkeypatch.setenv("HOME", str(fake_claude.workdir.parent))

    await make_cli(fake_claude).generate(SYSTEM, "hi")
    env = fake_claude.read()["env"]

    assert env["ANTHROPIC_API_KEY"] is None
    assert env["ANTHROPIC_BASE_URL"] is None
    assert env["TELEGRAM_BOT_TOKEN"] is None
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] is None  # parent token not inherited
    assert env["CLAUDE_CODE_SIMPLE"] is None
    assert env["CLAUDE_CODE_USE_BEDROCK"] is None
    assert env["CLAUDE_CODE_USE_VERTEX"] is None
    assert env["CLAUDE_CODE_USE_FOUNDRY"] is None
    # HOME preserved, isolation vars set
    assert env["HOME"] == str(fake_claude.workdir.parent)
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


async def test_configured_token_only_in_child_env(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "echo_token")
    token = "sk-ant-oat01-CONFIGURED"
    cli = make_cli(fake_claude, token=token)
    reply = await cli.generate(SYSTEM, "hi")  # redacted in output
    assert "[REDACTED]" in reply
    assert token not in reply
    rec = fake_claude.read()
    # token lives only in the child env, never in argv/stdin/prompt/repr
    assert rec["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == token
    assert token not in " ".join(rec["argv"])
    assert token not in rec["stdin"]
    assert token not in (rec["sysprompt_content"] or "")
    assert token not in repr(cli)


async def test_no_token_means_no_token_in_child(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "echo_token")
    await make_cli(fake_claude, token=None).generate(SYSTEM, "hi")
    assert fake_claude.read()["env"]["CLAUDE_CODE_OAUTH_TOKEN"] is None


# --------------------------------------------------------------------------- #
# token redaction in errors
# --------------------------------------------------------------------------- #
async def test_rejected_token_message(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "auth")
    token = "sk-ant-oat01-SECRETVALUE"
    with pytest.raises(ClaudeAuthError) as exc:
        await make_cli(fake_claude, token=token).generate(SYSTEM, "hi")
    message = str(exc.value)
    assert "setup-token" in message
    assert "CLI said:" in message
    assert token not in message
    assert "[REDACTED]" in (exc.value.detail or "")


# --------------------------------------------------------------------------- #
# inspect()
# --------------------------------------------------------------------------- #
async def test_inspect_reports_auth_and_init(fake_claude):
    cli = ClaudeCLI(
        fake_claude.bin,
        workdir=fake_claude.workdir,
        model=None,
        effort="low",
        timeout=120,
        oauth_token="sk-ant-oat01-X",
        auth_label="subscription token from .env",
    )
    info = await cli.inspect()
    assert info["auth"] == "subscription token from .env"
    assert info["init"]["tools"] == []
    assert info["init"]["apiProvider"] == "firstParty"
    assert info["reply"] == "OK"


# --------------------------------------------------------------------------- #
# run directory
# --------------------------------------------------------------------------- #
async def test_runs_in_own_dir(fake_claude):
    await make_cli(fake_claude).generate(SYSTEM, "hi")
    rec = fake_claude.read()
    assert Path(rec["cwd"]).resolve() == fake_claude.workdir.resolve()
    # Only the system-prompt temp file is present during the run.
    assert all(name.startswith("sys-") for name in rec["dir_entries"])


@POSIX_ONLY
async def test_run_dir_is_0700(fake_claude):
    await make_cli(fake_claude).generate(SYSTEM, "hi")
    mode = stat.S_IMODE(os.stat(fake_claude.workdir).st_mode)
    assert mode == 0o700


# --------------------------------------------------------------------------- #
# timeout kills the whole tree
# --------------------------------------------------------------------------- #
async def test_timeout_kills_process_tree(fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "sleep")
    marker = tmp_path / "grandchild_marker"
    monkeypatch.setenv("FAKE_MARKER", str(marker))

    start = time.monotonic()
    with pytest.raises(ClaudeTimeout):
        await make_cli(fake_claude, timeout=1).generate(SYSTEM, "hi")
    elapsed = time.monotonic() - start
    assert elapsed < 5

    # Grandchild would write the marker at +3s if still alive; it was killed.
    await asyncio.sleep(5)
    assert not marker.exists()


# --------------------------------------------------------------------------- #
# failure modes
# --------------------------------------------------------------------------- #
async def test_limit_epoch(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "limit_epoch")
    with pytest.raises(ClaudeLimitError) as exc:
        await make_cli(fake_claude).generate(SYSTEM, "hi")
    assert exc.value.resets_at == 1760000000


async def test_limit_text(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "limit_text")
    with pytest.raises(ClaudeLimitError) as exc:
        await make_cli(fake_claude).generate(SYSTEM, "hi")
    assert "3pm" in (exc.value.reset_text or "")


async def test_crash(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "crash")
    with pytest.raises(ClaudeError):
        await make_cli(fake_claude).generate(SYSTEM, "hi")


async def test_empty(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "empty")
    with pytest.raises(ClaudeError):
        await make_cli(fake_claude).generate(SYSTEM, "hi")


async def test_binary_not_found(tmp_path):
    cli = ClaudeCLI(
        str(tmp_path / "does-not-exist"),
        workdir=tmp_path / "run",
        model=None,
        effort="low",
        timeout=10,
        oauth_token=None,
    )
    with pytest.raises(ClaudeError) as exc:
        await cli.generate(SYSTEM, "hi")
    assert "not found" in str(exc.value)


# --------------------------------------------------------------------------- #
# classify_failure / parse_result units
# --------------------------------------------------------------------------- #
def test_classify_limit_epoch():
    err = classify_failure("usage limit hit |1760000000 extra")
    assert isinstance(err, ClaudeLimitError)
    assert err.resets_at == 1760000000
    assert "1760000000" not in err.detail


def test_classify_limit_by_status():
    assert isinstance(classify_failure("anything", status=429), ClaudeLimitError)


def test_classify_auth_text():
    assert isinstance(classify_failure("you are not logged in"), ClaudeAuthError)


def test_classify_auth_by_status():
    assert isinstance(classify_failure("nope", status=401), ClaudeAuthError)


def test_classify_generic():
    err = classify_failure("some random failure")
    assert type(err) is ClaudeError


def test_parse_result_success():
    text = parse_result(
        0, json.dumps({"subtype": "success", "is_error": False, "result": "  hey  "}), ""
    )
    assert text == "hey"


def test_parse_result_error_raises():
    out = json.dumps({"subtype": "error", "is_error": True, "result": "not logged in"})
    with pytest.raises(ClaudeAuthError):
        parse_result(1, out, "")


def test_parse_result_empty_result():
    out = json.dumps({"subtype": "success", "is_error": False, "result": ""})
    with pytest.raises(ClaudeError):
        parse_result(0, out, "")


def test_parse_result_non_json():
    with pytest.raises(ClaudeError):
        parse_result(1, "not json at all", "")

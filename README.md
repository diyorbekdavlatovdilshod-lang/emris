# Telegram Business personal assistant bot

A personal assistant that connects to **your own** Telegram account through
Telegram Business ("Chatbots"). When someone messages you, the bot drafts a
reply **in your voice**, using facts from `profile.md`. Replies are produced by
the **Claude Code CLI** (`claude -p`) running next to the bot, on **your Claude
subscription** — never an Anthropic API key.

> **Preview mode is the default.** Out of the box the bot only *logs* the reply
> it would send; it sends nothing until you set `AUTO_SEND=true`.
>
> **The bot does not keep its own chat allowlist.** Which chats it can see is
> decided entirely by Telegram, under *Settings → Telegram Business → Chatbots*.
> There is no `ALLOWED_CHAT_IDS` in the code.

## What it does

- Connects via **long polling** — no domain, webhook or open port needed.
- Reads the recent conversation, asks Claude for a reply in your style, and
  either previews it (default) or sends it on your behalf.
- Keeps short per-chat history, a pause switch and a usage-limit cooldown, all
  on disk so they survive restarts and redeploys.

## Behaviour

| Situation | What happens |
|---|---|
| A message on someone else's connected account | Ignored (`foreign_connection`). |
| Which chats the bot sees | Decided by Telegram's *Business → Chatbots* screen, not by the code. Any chat Telegram forwards is handled. |
| You reply in a chat yourself | The bot records it and stays **silent in that chat for `OWNER_TAKEOVER_MINUTES`** (default 15), then resumes. |
| A message from a bot (incl. the bot's own sent reply) | Ignored (`ignored_bot`) — no reply loops. |
| The same update delivered twice | Processed once (`duplicate`); de-dup persists across restarts. |
| A service message (joins, etc.) | `unsupported` — Claude is not called. |
| `/pause` | Stops drafting until `/resume`; persists across restarts. |
| Subscription usage limit | Backs off until the reported reset time, else `LIMIT_COOLDOWN_MINUTES`; persists across restarts. |
| Timeout / login error | Nothing is sent to the chat; you are not spammed (at most one DM per 10 min, auto-send only). |
| Claude replies `[SKIP]` | Nothing is sent (the conversation needs no reply). |
| Auto-send, just before sending | The connection is **re-checked from Telegram** (owner, enabled, can-reply); otherwise `permission_denied`. |

**Owner commands (your private chat with the bot):** `/pause`, `/resume`,
`/status`, and `/start` (an Uzbek how-to). Strangers who write to the bot
directly get a short Uzbek note and nothing else — Claude is never called and
no profile detail is revealed.

## How Claude is called

Each reply is one `claude -p` subprocess, started with
`asyncio.create_subprocess_exec` (**never a shell**), fully sandboxed:

```
claude -p --safe-mode --settings <isolation json> --tools "" \
  --strict-mcp-config --setting-sources "" --disable-slash-commands \
  --no-session-persistence --system-prompt-file <tmp> \
  --output-format json --effort <effort> [--model <model>]
```

- The isolation settings set `disableAllHooks` and disable every builtin
  plugin. `--output-format stream-json` adds `--verbose` (used by
  `--check-claude`).
- **`--bare` is never used**: it ignores the OAuth login and would break the
  subscription.
- The **conversation goes only on stdin**; the **system prompt + profile** go
  into a `0600` temp file (so nothing sensitive shows up in `ps` or `argv`).
- The child environment is scrubbed: everything starting with `ANTHROPIC_`,
  plus `TELEGRAM_BOT_TOKEN`, the parent's Claude token, `CLAUDE_CODE_SIMPLE`
  and the Bedrock/Vertex/Foundry switches are removed; `HOME` is kept. The
  subscription token is injected **only** when configured, and is redacted
  (`[REDACTED]`) from all captured output and errors.
- Verify everything with `python bot.py --check-claude` (see below).

## Deploy to Railway

1. **Get a Claude token.** On a computer with a browser run `claude setup-token`
   → copy the `sk-ant-oat...` value (valid ~1 year). It is a subscription
   token, **not** an API key.
2. **Create the bot.** In @BotFather: `/newbot`, then *Bot Settings → Business
   Mode* → enable. Get your own numeric id from @userinfobot.
3. **Private GitHub repo.** Push this code (the repo must be **private** —
   `profile.md` is baked into the Docker image).
4. **Railway → New Project → Deploy from GitHub Repository →** pick the repo.
   The included `railway.json` builds the `Dockerfile`.
5. **Service → Variables → Raw Editor:**
   ```
   TELEGRAM_BOT_TOKEN=...
   OWNER_ID=...
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
   AUTO_SEND=false
   ```
   Optional: `CLAUDE_MODEL`, `CLAUDE_EFFORT`, etc. You do **not** need
   `CLAUDE_BIN`, `DATA_DIR` or `PROFILE_PATH`.
6. **Add a Volume.** On the canvas right-click (or Cmd/Ctrl+K) → *Volume* →
   select the service → mount path **exactly `/app/data`** → Deploy. Without it,
   history, pause and limit state are lost on every deploy.
7. **Check the logs.** You should see `Bot @... is running`, `Mode: PREVIEW`,
   and `Claude auth: subscription token from environment variables`. Red text on
   Railway just means stderr — it is not an error by itself.
8. **Connect the account.** Message the bot `/start`, then on your phone:
   *Settings → Telegram Business → Chatbots* (needs **Telegram Premium**) →
   choose the bot's username, pick which chats it sees, and allow replies. The
   log shows `Business connection ...: user=<id> enabled=True`.
9. **Watch previews.** When someone writes, a `PREVIEW, not sent` block appears
   in the logs. Happy with them? Set `AUTO_SEND=true`.

- **Run the bot in only one place.** Two pollers on one bot token conflict —
  don't run `python bot.py` locally while Railway is live.
- **Every push redeploys.** Editing `profile.md` also means commit + push.
- **If a token leaks or expires:** re-run `claude setup-token` and update the
  Railway variable. If the bot token leaks: `/revoke` in @BotFather.

## Local development

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env && chmod 600 .env   # then fill in .env with an editor
python bot.py --check-claude    # verify Claude auth + isolation
python bot.py --try "Salom, qalesan?"    # draft a reply to a test message
python bot.py                   # run the bot (PREVIEW by default)
```

If `CLAUDE_CODE_OAUTH_TOKEN` is left empty, this machine's own `claude` login
is used (handy for local testing).

## Commands & exit codes

| Command | Purpose |
|---|---|
| `python bot.py` | Run the bot from `.env`. |
| `python bot.py --settings-from-env` | Run from environment variables (container mode); the variables are scrubbed from the process after loading. |
| `python bot.py --check-claude` | Verify Claude auth/isolation and exit. **Exit 1** if there is no init event, if tools/MCP/plugins/skills are non-empty, if `apiKeySource` is an API key, or if `apiProvider` is not `firstParty`. |
| `python bot.py --try "text"` | Draft a reply to an invented message and exit. |

`ConfigError` → exit **2**. A rejected Telegram token → exit **2**.
`--check-claude` / `--try` do not require Telegram settings.

## Tests

```bash
pytest
```

All tests run offline (no network, no real Claude, no real Telegram). A fake
`claude` binary exercises the subprocess wrapper; a fake aiogram session and a
fake clock drive the behaviour tests.

## Files

```
bot.py                       entry point / CLI
assistant/config.py          settings loading & validation
assistant/claude_cli.py      the isolated `claude -p` wrapper
assistant/language.py        uz / ru / en detection (a hint only)
assistant/prompts.py         system & user prompt building
assistant/store.py           history, de-dup, runtime state (atomic, private)
assistant/service.py         the decision pipeline (preview / send)
assistant/handlers.py        aiogram router
profile.md                   your profile template (fill it in)
Dockerfile / railway.json    Railway deploy
tests/                       offline test suite
```

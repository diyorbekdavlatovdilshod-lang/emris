# Debian slim (glibc) — the Claude Code installer needs glibc, not Alpine/musl.
FROM python:3.12-slim

# Claude Code version the flags and builtin-plugin list were verified against
# (locally: claude 2.1.325). Override with --build-arg CLAUDE_VERSION=x.y.z.
ARG CLAUDE_VERSION=2.1.325

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    PATH="/root/.local/bin:${PATH}"

# Install Claude Code (official installer) plus its runtime deps.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates bash \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_VERSION}" \
    && claude --version

WORKDIR /app

# Install Python deps first so they are cached across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Then the application code (profile.md is baked into the image too).
COPY . .

# Container mode: read settings from env vars, then scrub them.
CMD ["python", "bot.py", "--settings-from-env"]

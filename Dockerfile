# Image for the headless Steps 1-2 cron (daily search + idempotent capture).
#
# The A-day search uses the Claude Agent SDK, which spawns the Claude Code CLI
# as a subprocess, so the image carries Node.js + @anthropic-ai/claude-code in
# addition to Python. The B-day (Perplexity) path is pure HTTP and needs only
# Python; both agents share this one image and the day-of-year parity picks one.
FROM python:3.14-slim-bookworm

ENV TZ=America/New_York \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

# The Claude Agent SDK spawns the Claude Code CLI with --dangerously-skip-permissions,
# which the CLI hard-refuses under root/sudo. Run as a non-root user so that guard is
# satisfied the way it asks to be (rather than relying on the undocumented IS_SANDBOX
# escape hatch). --create-home gives the CLI a writable HOME (~/.claude) and uv a cache.
RUN useradd --create-home --uid 1001 appuser

# Node 20 (for the Claude Code CLI) + tzdata for the ET-anchored A/B parity.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg tzdata \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# uv, copied from its published image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (cached until the lockfile changes).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the source and the prompt template, and install the project itself.
COPY src/ ./src/
COPY deep_research_prompt.md ./
RUN uv sync --frozen --no-dev

# Hand the build artifacts (venv, source, prompt) to the non-root user, then drop to it.
# uv's runtime sync check and the Claude CLI's config writes both need this ownership.
RUN chown -R appuser:appuser /app
USER appuser

# One run per machine start; the scheduled machine stops when the command exits.
# `jsa cron` self-gates to Mon/Wed/Fri with a weekday-sized window (see cli.py),
# so the Fly machine can wake on a plain fuzzy `--schedule daily`.
ENTRYPOINT ["uv", "run", "--no-dev", "jsa", "cron"]

# Image for the headless Steps 1-2 cron (daily search + idempotent capture).
#
# The A-day search uses the Claude Agent SDK, which spawns the Claude Code CLI
# as a subprocess, so the image carries Node.js + @anthropic-ai/claude-code in
# addition to Python. The B-day (Perplexity) path is pure HTTP and needs only
# Python; both agents share this one image and the day-of-year parity picks one.
FROM python:3.12-slim-bookworm

ENV TZ=America/New_York \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

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

# One run per machine start; the scheduled machine stops when the search exits.
ENTRYPOINT ["uv", "run", "--no-dev", "jsa", "search"]

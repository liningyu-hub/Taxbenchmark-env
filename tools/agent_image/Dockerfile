# The real agent's machine: Claude Code and Python, and nothing from this project.
# The one-file tab client is mounted read-only at run time (/opt/tab/tab_client.py); `tab` is a wrapper for it.
# Build: docker build -t tab-agent:2.1.212 --build-arg CLAUDE_CODE_VERSION=2.1.212 tools/agent_image
FROM node:22-slim

ARG CLAUDE_CODE_VERSION=2.1.212
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && printf '#!/bin/sh\nexec python3 /opt/tab/tab_client.py "$@"\n' > /usr/local/bin/tab \
 && chmod 755 /usr/local/bin/tab

# No update checks, telemetry or error reports: the sandbox lets only the model API through.
ENV DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

USER node
WORKDIR /home/node/work

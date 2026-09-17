FROM python:3.12-slim

RUN useradd -m -u 1000 bridge

# The `claude` CLI itself must be available on PATH inside the container.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://claude.ai/install.sh | bash -s -- --yes \
    && mv /root/.local/bin/claude /usr/local/bin/claude 2>/dev/null || true \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY vk_bridge.py .

# Optional: uncomment for local voice transcription.
# RUN pip install --no-cache-dir faster-whisper

USER bridge
ENTRYPOINT ["python", "vk_bridge.py"]

ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends chromium fonts-noto-core fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/tiktok/skill
COPY requirements.txt ./requirements.txt
RUN python3 -m venv /opt/tiktok/venv && /opt/tiktok/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY scripts ./scripts
COPY SKILL.md ./SKILL.md
COPY references ./references
ENV PYTHONUNBUFFERED=1 HOME=/tmp/tiktok TIKTOK_CHROMIUM_NO_SANDBOX=1
USER 10001:10001
ENTRYPOINT ["/opt/tiktok/venv/bin/python", "/opt/tiktok/skill/scripts/download.py"]
CMD ["--help"]

ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}
USER root
WORKDIR /opt/tiktok/skill
COPY requirements.txt ./requirements.txt
RUN python3 -m venv /opt/tiktok/venv && /opt/tiktok/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY scripts ./scripts
COPY SKILL.md ./SKILL.md
COPY references ./references
ENV PYTHONUNBUFFERED=1 HOME=/tmp/tiktok
USER 10001:10001
ENTRYPOINT ["/opt/tiktok/venv/bin/python", "/opt/tiktok/skill/scripts/download.py"]
CMD ["--help"]

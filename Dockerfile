FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates curl bash \
    && rm -rf /var/lib/apt/lists/*
# Claude Code CLI (the agent runs as `claude -p`)
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}"
WORKDIR /app
COPY pyproject.toml README.md ./
COPY askthecow ./askthecow
RUN pip install --no-cache-dir .
ENV ASKTHECOW_DATA_DIR=/data ASKTHECOW_HOST=0.0.0.0 ASKTHECOW_PORT=8000
VOLUME /data
EXPOSE 8000
CMD ["askthecow", "serve"]

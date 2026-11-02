FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY askthecow ./askthecow
RUN pip install --no-cache-dir .
ENV ASKTHECOW_DATA_DIR=/data ASKTHECOW_HOST=0.0.0.0 ASKTHECOW_PORT=8000
VOLUME /data
EXPOSE 8000
CMD ["askthecow", "serve"]

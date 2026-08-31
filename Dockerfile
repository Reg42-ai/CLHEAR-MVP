# clhear-workers: the scheduled ingestion fleet (SQS consumer).
# EventBridge cron -> SQS AdapterRunRequested -> this container runs the
# full ingest pipeline and publishes the corpus snapshot for the explorer.
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt boto3

# CPU sidecar (`python -m app.clhear.platform.ollama_sidecar`) needs the
# ollama binary; 4b/9b weights are restored from S3 / pulled at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://ollama.com/install.sh | sh \
    && test -x /usr/local/bin/ollama \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY migrations ./migrations

ENTRYPOINT ["python", "-m", "app.clhear.workers"]

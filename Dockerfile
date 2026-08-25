# clhear-workers: the scheduled ingestion fleet (SQS consumer).
# EventBridge cron -> SQS AdapterRunRequested -> this container runs the
# full ingest pipeline and publishes the corpus snapshot for the explorer.
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt boto3

COPY app ./app
COPY migrations ./migrations

ENTRYPOINT ["python", "-m", "app.clhear.workers"]

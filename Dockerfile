# clhear-workers: the scheduled ingestion fleet (SQS consumer).
# EventBridge cron -> SQS AdapterRunRequested -> this container runs the
# full ingest pipeline and publishes the corpus snapshot for the explorer.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/srv

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

# No model runtime in the image: inference is Reg42 Infer on Bedrock (HLD v2 I6).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY migrations ./migrations
COPY clhear-evals/l1/boundary ./clhear-evals/l1/boundary

# Run the fleet's existing offline boundary evaluator inside the built image.
# These fixtures test parser behavior; they are not imported into the corpus.
RUN --network=none python -c 'import json; from app.clhear.platform.evals import l1_boundary_f1; scores, passed = l1_boundary_f1(None, None); print(json.dumps({"suite": "l1_boundary_f1", "cases": scores.get("cases", 0), "passed": passed})); assert passed and scores.get("cases", 0) > 0, "Worker image lacks a passing boundary golden set"'

ENTRYPOINT ["python", "-m", "app.clhear.workers"]

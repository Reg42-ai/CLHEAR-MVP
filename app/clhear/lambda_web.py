"""Lambda entrypoint for the public Sources Explorer.

# ARCH: HLD §5 serves the UI from the existing reg42-os web service behind the
# existing ALB (host rule clhear.reg42.ai). That infra is not accessible from
# this repo, so the MVP ships the same FastAPI app behind a Lambda Function URL
# — near-zero idle cost, swaps out cleanly when reg42-infra is wired.

The read-only corpus (SQLite built by the ingestion run) is fetched from the
deploy bucket to /tmp at cold start; clause text still flows through the
clauses_public discipline exactly as everywhere else.
"""
import os

DB_LOCAL_PATH = "/tmp/clhear.db"


def _prepare_db() -> None:
    uri = os.environ.get("CLHEAR_DB_S3_URI", "")
    if uri.startswith("s3://") and not os.path.exists(DB_LOCAL_PATH):
        import boto3

        bucket, key = uri[len("s3://") :].split("/", 1)
        boto3.client("s3").download_file(bucket, key, DB_LOCAL_PATH)
    if os.path.exists(DB_LOCAL_PATH):
        os.environ["DATABASE_URL"] = f"sqlite:///{DB_LOCAL_PATH}"


_prepare_db()

from mangum import Mangum  # noqa: E402

from app.main import app  # noqa: E402

handler = Mangum(app)

"""Fetch UK legislation.gov.uk artifacts from a non-AWS egress and write
last-good objects under s3://…/public-ok/_http_cache/ so ECS workers can
parse on TNA 202 without an empty first ingest.
"""
import hashlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clhear.l1 import http  # noqa: E402
from app.clhear.l1.registry_etoro import S, wave1_entries  # noqa: E402
from app.clhear.settings import get_settings  # noqa: E402

log = logging.getLogger("clhear.bootstrap_uk")
BASE = "https://www.legislation.gov.uk"


def _urls() -> list[str]:
    urls = [
        f"{BASE}/uksi/2017/692/data.xml",
        f"{BASE}/uksi/2017/692/made/data.xml",
        f"{BASE}/uksi/2017/692/2020-01-09/data.xml",
        f"{BASE}/uksi/1986/1711/contents",
    ]
    for entry in wave1_entries():
        if entry["adapter"] != "uk_legislation":
            continue
        doc = (entry.get("fetch") or {}).get("doc") or entry["key"]
        urls.append(f"{BASE}/{doc}/data.xml")
    # Dedup, keep order
    seen, out = set(), []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    import boto3

    s3 = boto3.client("s3", region_name=settings.aws_region)
    bucket = settings.clhear_datalake_bucket
    ok = fail = 0
    for url in _urls():
        try:
            content = http.get(url, timeout=90.0)
        except Exception:
            log.exception("fetch failed %s", url)
            fail += 1
            continue
        digest = hashlib.sha256(url.encode()).hexdigest()[:24]
        key = f"public-ok/_http_cache/{digest}.bin"
        s3.put_object(Bucket=bucket, Key=key, Body=content, ContentType="application/octet-stream")
        log.info("cached %s -> s3://%s/%s (%d bytes)", url, bucket, key, len(content))
        ok += 1
    print({"cached": ok, "failed": fail, "bucket": bucket})
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

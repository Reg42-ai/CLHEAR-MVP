"""Polite HTTP fetching for adapters (HLD §7.2, §8.7).

- Identifying UA string; backoff-and-retry on 429/5xx; never hammer endpoints.
- Fixture modes so adapters run green offline:
    CLHEAR_HTTP_MODE=replay   read recorded fixtures only (tests; the default)
    CLHEAR_HTTP_MODE=record   fetch live once, save fixture, then replay
    CLHEAR_HTTP_MODE=live     fetch live with an on-disk cache (real ingestion)
  Fixture/cache dir: CLHEAR_HTTP_FIXTURES (default tests/fixtures/http).

Fixture file = sha256(url)[:24].json.gz: {"url", "status", "content_b64"}.
"""
import base64
import gzip
import hashlib
import json
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("clhear.l1.http")

USER_AGENT = "CLHEAR/0.1 (regulatory corpus builder; contact clhear@reg42.ai)"
DEFAULT_FIXTURES_DIR = "tests/fixtures/http"


class FixtureMissing(RuntimeError):
    pass


def _mode() -> str:
    return os.environ.get("CLHEAR_HTTP_MODE", "replay")


def _fixtures_dir() -> Path:
    return Path(os.environ.get("CLHEAR_HTTP_FIXTURES", DEFAULT_FIXTURES_DIR))


def _fixture_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    return _fixtures_dir() / f"{digest}.json.gz"


def _read_fixture(path: Path) -> bytes:
    record = json.loads(gzip.decompress(path.read_bytes()))
    return base64.b64decode(record["content_b64"])


def _write_fixture(path: Path, url: str, status: int, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"url": url, "status": status, "content_b64": base64.b64encode(content).decode()}
    path.write_bytes(gzip.compress(json.dumps(record).encode()))


def _fetch_live(url: str, timeout: float, headers: dict | None = None, attempts: int = 6) -> bytes:
    delay = 2.0
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": USER_AGENT, **(headers or {})},
                timeout=timeout,
                follow_redirects=True,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} from {url}", request=resp.request, response=resp
                )
            # legislation.gov.uk often answers 202 Accepted with an empty body
            # while it materializes data.xml — treat as retryable, not success.
            if resp.status_code == 202 or not resp.content:
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} empty/accepted from {url}",
                    request=resp.request,
                    response=resp,
                )
            resp.raise_for_status()
            return resp.content
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # 202 / empty body (TNA materializing XML) is retryable, like 429.
            if status is not None and status not in (202, 429) and status < 500:
                raise  # other 4xx: retrying will not help
            last_error = exc
            if attempt < attempts - 1:
                log.warning("fetch %s failed (%s); backing off %.0fs", url, exc, delay)
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"fetch failed after {attempts} attempts: {url}") from last_error


def get(url: str, timeout: float = 60.0, headers: dict | None = None) -> bytes:
    """Fetch url as bytes honoring CLHEAR_HTTP_MODE (replay/record/live)."""
    mode = _mode()
    path = _fixture_path(url)
    if path.exists():
        return _read_fixture(path)
    if mode == "replay":
        raise FixtureMissing(f"no recorded fixture for {url} (CLHEAR_HTTP_MODE=replay)")
    content = _fetch_live(url, timeout, headers)
    # record mode saves committed fixtures; live mode caches to avoid re-hitting
    # official endpoints within a run. Same file format either way.
    _write_fixture(path, url, 200, content)
    return content

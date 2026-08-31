"""CPU Ollama sidecar: restore the S3 cache, serve, pull 4b/9b only.

Never pulls 27b on Fargate — that weight is GPU-only. Manifests for 4b/9b
are restored from the shared cache; 27b blobs stay on S3 for the nightly box.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any, Callable, Iterable
from urllib.error import URLError
from urllib.request import Request, urlopen

log = logging.getLogger("clhear.ollama_sidecar")

CPU_MODELS = ("qwen3.5:4b", "qwen3.5:9b")
GPU_ONLY_MODELS = ("qwen3.6:27b",)
OLLAMA_HOST_DEFAULT = "0.0.0.0:11434"
DATA_DIR_DEFAULT = "/root/.ollama"
MANIFEST_PREFIX = "manifests/registry.ollama.ai/library/"


def cpu_models() -> tuple[str, ...]:
    raw = os.environ.get("CLHEAR_OLLAMA_CPU_MODELS", "")
    if raw.strip():
        models = tuple(m.strip() for m in raw.split(",") if m.strip())
        return tuple(m for m in models if not is_gpu_only(m))
    return CPU_MODELS


def is_gpu_only(model: str) -> bool:
    name = (model or "").lower()
    return "27b" in name or name in GPU_ONLY_MODELS


def manifest_key_for(model: str, prefix: str = "") -> str:
    name, _, tag = model.partition(":")
    rel = f"{MANIFEST_PREFIX}{name}/{tag}"
    if prefix:
        return f"{prefix.rstrip('/')}/{rel}"
    return rel


def is_cpu_manifest_key(key: str) -> bool:
    """True for qwen3.5 4b/9b manifests; false for 27b or unrelated keys."""
    k = key.replace("\\", "/").lower()
    if "27b" in k:
        return False
    return any(k.endswith(f"/qwen3.5/{tag}") or k.endswith(f"/qwen3.5/{tag}.json") for tag in ("4b", "9b"))


def blob_names_from_manifest(doc: dict | list | str) -> list[str]:
    """Collect sha256-* blob filenames referenced by an Ollama/OCI manifest."""
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except json.JSONDecodeError:
            return []
    digests: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            digest = node.get("digest")
            if isinstance(digest, str) and digest.startswith("sha256:"):
                digests.append("sha256-" + digest.split(":", 1)[1])
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(doc)
    # Preserve order, unique.
    seen: set[str] = set()
    out: list[str] = []
    for name in digests:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def parse_s3_uri(uri: str) -> tuple[str, str]:
    raw = uri[len("s3://") :] if uri.startswith("s3://") else uri
    bucket, _, key = raw.partition("/")
    return bucket, key


def select_cpu_cache_keys(keys: Iterable[str], prefix: str = "") -> list[str]:
    """Keep CPU manifests plus (later) their blobs — never 27b manifests."""
    return [k for k in keys if is_cpu_manifest_key(k) or (
        prefix and k.startswith(prefix) and is_cpu_manifest_key(k[len(prefix):].lstrip("/"))
    )]


def restore_cpu_cache(
    cache_uri: str,
    dest: str,
    *,
    s3_client=None,
) -> dict:
    """Download 4b/9b manifests + referenced blobs from the shared S3 cache."""
    if not cache_uri or not cache_uri.startswith("s3://"):
        return {"restored": False, "reason": "no cache uri", "files": 0}
    bucket, prefix = parse_s3_uri(cache_uri)
    if s3_client is None:
        import boto3

        s3_client = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or "us-east-1")
    keys: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    kwargs: dict[str, Any] = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix if prefix.endswith("/") else prefix + "/"
        # also allow prefix without trailing slash for the first page
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []) or []:
            key = obj.get("Key") or ""
            if key and not key.endswith("/") and not key.endswith(".keep"):
                keys.append(key)
    manifests = [k for k in keys if is_cpu_manifest_key(k)]
    downloaded = 0
    blob_names: set[str] = set()
    os.makedirs(dest, exist_ok=True)
    for key in manifests:
        rel = key[len(prefix):].lstrip("/") if prefix and key.startswith(prefix) else key
        if "27b" in rel:
            continue
        path = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        s3_client.download_file(bucket, key, path)
        downloaded += 1
        try:
            with open(path, encoding="utf-8") as fh:
                blob_names.update(blob_names_from_manifest(fh.read()))
        except OSError:
            log.exception("could not parse restored manifest %s", key)
    blob_prefix = f"{prefix.rstrip('/')}/blobs/" if prefix else "blobs/"
    for key in keys:
        base = key.rsplit("/", 1)[-1]
        if base in blob_names and (key.startswith(blob_prefix) or "/blobs/" in key):
            rel = key[len(prefix):].lstrip("/") if prefix and key.startswith(prefix) else key
            path = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            s3_client.download_file(bucket, key, path)
            downloaded += 1
    log.info("cpu ollama cache restored files=%s manifests=%s blobs=%s", downloaded, len(manifests), len(blob_names))
    return {"restored": True, "files": downloaded, "manifests": len(manifests), "blobs": len(blob_names)}


def _http_json(url: str, *, timeout: float = 5, data: bytes | None = None) -> tuple[int, Any]:
    req = Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            parsed = {}
        return int(resp.status), parsed


def tags_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/api/tags"


def model_present(tags_payload: Any, model: str) -> bool:
    models = []
    if isinstance(tags_payload, dict):
        models = tags_payload.get("models") or []
    names = []
    for item in models:
        if isinstance(item, dict):
            names.append(str(item.get("name") or item.get("model") or ""))
        else:
            names.append(str(item))
    return model in names or any(n.startswith(model) for n in names)


def wait_http_tags(base_url: str, *, timeout_s: float = 600, sleeper=time.sleep, http_get=None) -> bool:
    deadline = time.monotonic() + timeout_s
    getter = http_get or (lambda url: _http_json(url)[0])
    while time.monotonic() < deadline:
        try:
            status = getter(tags_url(base_url))
            if isinstance(status, tuple):
                status = status[0]
            if int(status) == 200:
                return True
        except (URLError, TimeoutError, OSError, ValueError):
            pass
        sleeper(2)
    return False


def pull_cpu_models(
    base_url: str,
    models: Iterable[str] | None = None,
    *,
    http_post=None,
    tags_payload: Any | None = None,
) -> dict:
    """Pull missing CPU models. Refuses anything 27b-sized."""
    wanted = [m for m in (models or cpu_models()) if not is_gpu_only(m)]
    skipped_gpu = [m for m in (models or ()) if is_gpu_only(m)]
    pulled: list[str] = []
    already: list[str] = []
    if tags_payload is None:
        try:
            _, tags_payload = _http_json(tags_url(base_url), timeout=15)
        except Exception:
            tags_payload = {}
    for model in wanted:
        if model_present(tags_payload, model):
            already.append(model)
            continue
        if http_post:
            http_post(base_url.rstrip("/") + "/api/pull", json.dumps({"name": model, "stream": False}).encode())
        else:
            _http_json(
                base_url.rstrip("/") + "/api/pull",
                timeout=3600,
                data=json.dumps({"name": model, "stream": False}).encode(),
            )
        pulled.append(model)
    if skipped_gpu:
        log.warning("refusing to pull GPU-only models on CPU sidecar: %s", skipped_gpu)
    return {"pulled": pulled, "already": already, "refused": skipped_gpu}


def ensure_cpu_models(base_url: str, **kwargs) -> dict:
    return pull_cpu_models(base_url, **kwargs)


def serve_command() -> list[str]:
    return ["ollama", "serve"]


def sidecar_base_url() -> str:
    host = os.environ.get("OLLAMA_HOST") or OLLAMA_HOST_DEFAULT
    if "://" in host:
        return host.rstrip("/")
    return f"http://{host}"


def run_sidecar(
    *,
    cache_uri: str | None = None,
    dest: str | None = None,
    restorer: Callable[..., dict] | None = None,
    server: Callable[..., Any] | None = None,
    waiter: Callable[..., bool] | None = None,
    puller: Callable[..., dict] | None = None,
    wait_forever: bool = True,
) -> dict:
    """Restore → serve → pull 4b/9b. Injected callables keep this unit-testable."""
    cache_uri = cache_uri if cache_uri is not None else os.environ.get("CLHEAR_OLLAMA_MODEL_CACHE_S3", "")
    # Ollama home is ~/.ollama; models live in ~/.ollama/models. Do not set
    # OLLAMA_MODELS to the home dir or restores from S3 (models/manifests/...) miss.
    dest = dest or os.environ.get("OLLAMA_HOME") or DATA_DIR_DEFAULT
    os.environ.setdefault("OLLAMA_MODELS", os.path.join(dest, "models"))
    restored = (restorer or restore_cpu_cache)(cache_uri, dest) if cache_uri else {"restored": False, "reason": "no cache"}
    proc = (server or (lambda: subprocess.Popen(serve_command())))()
    base = sidecar_base_url()
    ready = (waiter or wait_http_tags)(base)
    pulled = {"pulled": [], "already": [], "refused": []}
    if ready:
        pulled = (puller or pull_cpu_models)(base)
    else:
        log.error("ollama serve did not become ready; CPU models not pulled")
    if wait_forever and proc is not None and hasattr(proc, "wait"):
        proc.wait()
    return {"restored": restored, "ready": ready, "models": pulled}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log.info("cpu ollama sidecar starting models=%s", ",".join(cpu_models()))
    run_sidecar()


if __name__ == "__main__":
    main()

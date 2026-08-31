"""Nightly ephemeral GPU: g6.xlarge spot, S3 model cache, 4h fuse, 5h orphan guard.

The worker launches a VPC-internal spot instance at the start of the nightly
job, boots Ollama from the S3 cache (no nightly re-downloads), and terminates
the instance at job end. User-data also `shutdown -h +240`. A CloudWatch
metric `GpuOrphanCount` plus this module's `orphan_guard` catch anything
older than 5 hours.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import gpu_sessions
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.gpu")

GPU_TAG = "clhear:role"
GPU_TAG_VALUE = "gpu-nightly"
ORPHAN_HOURS = 5.0
SPOT_USD_PER_HOUR = 0.50  # conservative g6.xlarge spot estimate
WAIT_INSTANCE_S = 300
WAIT_OLLAMA_S = 1200
OLLAMA_POLL_S = 5


def userdata_script(*, cache_uri: str, region: str) -> str:
    """Instance user-data: restore Ollama models from S3, start serve, 4h fuse."""
    bucket_key = cache_uri[len("s3://"):] if cache_uri.startswith("s3://") else cache_uri
    return f"""#!/bin/bash
set -euxo pipefail
shutdown -h +240 || true
dnf install -y docker awscli || yum install -y docker awscli || true
systemctl start docker || true
mkdir -p /opt/ollama
if [ -n "{cache_uri}" ]; then
  aws s3 sync s3://{bucket_key} /opt/ollama --region {region} || true
fi
docker run -d --gpus all --name ollama -p 11434:11434 -v /opt/ollama:/root/.ollama ollama/ollama
# Pull only if the cache missed (first night).
docker exec ollama ollama list | grep -q qwen3.5:4b || docker exec ollama ollama pull qwen3.5:4b
docker exec ollama ollama list | grep -q qwen3.5:9b || docker exec ollama ollama pull qwen3.5:9b
docker exec ollama ollama list | grep -q qwen3.6:27b || docker exec ollama ollama pull qwen3.6:27b
# Persist any newly pulled weights back to the cache.
if [ -n "{cache_uri}" ]; then
  aws s3 sync /opt/ollama s3://{bucket_key} --region {region} || true
fi
"""


def is_gpu_open(engine: Engine) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(gpu_sessions)
            .where(gpu_sessions.c.status.in_(("running", "launching")))
            .order_by(gpu_sessions.c.started_at.desc())
            .limit(1)
        ).first()
    return row is not None


def current_session(engine: Engine) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(gpu_sessions).order_by(gpu_sessions.c.started_at.desc()).limit(1)
        ).first()
    if row is None:
        return None
    return {
        "id": row.id,
        "instance_id": row.instance_id,
        "status": row.status,
        "started_at": str(row.started_at),
        "ended_at": str(row.ended_at) if row.ended_at else None,
        "est_cost_usd": float(row.est_cost_usd) if row.est_cost_usd is not None else None,
        "detail": row.detail if isinstance(row.detail, dict) else {},
    }


def _ec2(region: str, client_factory: Callable | None = None):
    if client_factory:
        return client_factory("ec2", region)
    import boto3

    return boto3.client("ec2", region_name=region)


def _http_get(url: str, timeout: float = 5) -> tuple[int, bytes]:
    from urllib.request import Request, urlopen

    req = Request(url, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        return int(resp.status), resp.read()


def wait_instance_ip(
    ec2,
    instance_id: str,
    *,
    timeout_s: float = WAIT_INSTANCE_S,
    sleeper: Callable[[float], None] | None = None,
) -> dict:
    """Block until the instance is running and has a PrivateIpAddress."""
    sleeper = sleeper or time.sleep
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        for rsv in resp.get("Reservations", []):
            for inst in rsv.get("Instances", []):
                last = inst
                state = (inst.get("State") or {}).get("Name")
                ip = inst.get("PrivateIpAddress")
                if state == "running" and ip:
                    return inst
        sleeper(5)
    raise TimeoutError(f"instance {instance_id} never reported PrivateIp (last={last})")


def _update_session(engine: Engine, session_id: str, **values: Any) -> None:
    with engine.begin() as conn:
        conn.execute(gpu_sessions.update().where(gpu_sessions.c.id == session_id).values(**values))


def wait_for_ollama(
    engine: Engine,
    session_id: str | None,
    *,
    http_get: Callable[[str], tuple[int, bytes]] | None = None,
    sleeper: Callable[[float], None] | None = None,
    timeout_s: float = WAIT_OLLAMA_S,
) -> dict:
    """Poll http://<private_ip>:11434/api/tags until Ollama answers (cap ~20 min)."""
    if not session_id:
        return {"ready": False, "reason": "no session"}
    session = None
    with engine.connect() as conn:
        row = conn.execute(sa.select(gpu_sessions).where(gpu_sessions.c.id == session_id)).first()
    if row is None:
        return {"ready": False, "reason": "unknown session"}
    detail = row.detail if isinstance(row.detail, dict) else {}
    url = detail.get("ollama_url")
    if not url:
        return {"ready": False, "reason": "no ollama_url"}
    getter = http_get or _http_get
    sleeper = sleeper or time.sleep
    deadline = time.monotonic() + timeout_s
    tags_endpoint = f"{str(url).rstrip('/')}/api/tags"
    last_err = ""
    while time.monotonic() < deadline:
        try:
            status, body = getter(tags_endpoint)
            if int(status) == 200:
                merged = {**detail, "ollama_ready": True}
                _update_session(engine, session_id, status="running", detail=merged)
                return {"ready": True, "url": url, "session_id": session_id, "body": body}
        except Exception as exc:
            last_err = str(exc)[:200]
        sleeper(OLLAMA_POLL_S)
    merged = {**detail, "ollama_ready": False, "wait_error": last_err or "timeout"}
    _update_session(engine, session_id, status=row.status, detail=merged)
    log.error("Ollama on %s not ready after %.0fs: %s", url, timeout_s, last_err or "timeout")
    return {"ready": False, "reason": last_err or "timeout", "url": url, "session_id": session_id}


def attach_router(llm: Any, url: str):
    """Point the in-process Router at the GPU Ollama URL for the nightly window."""
    from app.clhear.platform.gateway import OllamaProvider
    from app.clhear.platform.router import Router

    if not url or not isinstance(llm, Router):
        return None
    previous = (llm.providers.get("ollama"), llm._gpu_open)
    llm.providers["ollama"] = OllamaProvider(url)
    llm._gpu_open = True
    return previous


def detach_router(llm: Any, previous) -> None:
    from app.clhear.platform.router import Router

    if previous is None or not isinstance(llm, Router):
        return
    provider, gpu_open = previous
    if provider is None:
        llm.providers.pop("ollama", None)
    else:
        llm.providers["ollama"] = provider
    llm._gpu_open = gpu_open


def launch_nightly_gpu(
    engine: Engine,
    *,
    client_factory: Callable | None = None,
    now: datetime | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> dict:
    """Launch a g6.xlarge spot (or record a dry-run when infra vars are empty)."""
    settings = get_settings()
    started = now or datetime.now(timezone.utc)
    session_id = f"gpu-{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    detail: dict[str, Any] = {"instance_type": settings.clhear_gpu_instance_type}
    instance_id = ""
    status = "dry-run"
    if settings.clhear_gpu_subnet_id and settings.clhear_gpu_security_group_id:
        try:
            ec2 = _ec2(settings.aws_region, client_factory)
            user_data = userdata_script(
                cache_uri=settings.clhear_ollama_model_cache_s3, region=settings.aws_region
            )
            params: dict[str, Any] = {
                "ImageId": settings.clhear_gpu_ami_id or _default_ami(ec2),
                "InstanceType": settings.clhear_gpu_instance_type,
                "MinCount": 1,
                "MaxCount": 1,
                "UserData": user_data,
                "NetworkInterfaces": [
                    {
                        "DeviceIndex": 0,
                        "SubnetId": settings.clhear_gpu_subnet_id,
                        "Groups": [settings.clhear_gpu_security_group_id],
                        "AssociatePublicIpAddress": True,
                    }
                ],
                "InstanceMarketOptions": {
                    "MarketType": "spot",
                    "SpotOptions": {"SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate"},
                },
                "TagSpecifications": [
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "Name", "Value": f"{session_id}"},
                            {"Key": GPU_TAG, "Value": GPU_TAG_VALUE},
                            {"Key": "clhear:fuse_hours", "Value": str(int(settings.clhear_gpu_max_hours))},
                        ],
                    }
                ],
            }
            if settings.clhear_gpu_instance_profile:
                params["IamInstanceProfile"] = {"Name": settings.clhear_gpu_instance_profile}
            try:
                resp = ec2.run_instances(**params)
            except Exception as spot_exc:
                # Spot needs AWSServiceRoleForEC2Spot; on-demand still works.
                if "InstanceMarketOptions" in params and (
                    "Spot" in str(spot_exc) or "spot" in str(spot_exc).lower()
                    or "ServiceLinkedRole" in str(spot_exc)
                ):
                    log.warning("spot launch failed (%s); retrying on-demand", spot_exc)
                    detail["spot_error"] = str(spot_exc)[:300]
                    params.pop("InstanceMarketOptions", None)
                    resp = ec2.run_instances(**params)
                    detail["market"] = "on-demand"
                else:
                    raise
            instance_id = resp["Instances"][0]["InstanceId"]
            inst = wait_instance_ip(ec2, instance_id, sleeper=sleeper)
            private_ip = inst.get("PrivateIpAddress") or ""
            ollama_url = f"http://{private_ip}:11434" if private_ip else ""
            status = "launching"
            detail["run_instances"] = {"instance_id": instance_id}
            detail["private_ip"] = private_ip
            detail["ollama_url"] = ollama_url
        except Exception as exc:
            log.exception("GPU launch failed")
            status = "failed"
            detail["error"] = str(exc)[:400]
    else:
        detail["note"] = "GPU subnet/SG not configured — recorded dry-run (owner: set terraform outputs)"
    with engine.begin() as conn:
        conn.execute(
            gpu_sessions.insert().values(
                id=session_id,
                instance_id=instance_id,
                instance_type=settings.clhear_gpu_instance_type,
                status=status,
                started_at=started,
                detail=detail,
            )
        )
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine,
            kind="gpu_lifecycle",
            layer="L0",
            fleet="gpu",
            reasoning=f"nightly GPU {status} {started.strftime('%H:%M')} ({settings.clhear_gpu_instance_type} spot)",
            detail={"session_id": session_id, "instance_id": instance_id, "status": status, **{k: detail.get(k) for k in ("private_ip", "ollama_url")}},
        )
    except Exception:
        log.exception("gpu ai_ops failed")
    return {"id": session_id, "instance_id": instance_id, "status": status, "detail": detail}


def terminate_gpu(
    engine: Engine,
    session_id: str | None = None,
    *,
    client_factory: Callable | None = None,
    now: datetime | None = None,
) -> dict:
    ended = now or datetime.now(timezone.utc)
    with engine.connect() as conn:
        if session_id:
            row = conn.execute(sa.select(gpu_sessions).where(gpu_sessions.c.id == session_id)).first()
        else:
            row = conn.execute(
                sa.select(gpu_sessions)
                .where(gpu_sessions.c.status.in_(("running", "launching", "dry-run")))
                .order_by(gpu_sessions.c.started_at.desc())
                .limit(1)
            ).first()
    if row is None:
        return {"terminated": False, "reason": "no open session"}
    hours = max(0.01, (ended - _as_dt(row.started_at)).total_seconds() / 3600)
    est = round(hours * SPOT_USD_PER_HOUR, 4)
    if row.instance_id:
        try:
            ec2 = _ec2(get_settings().aws_region, client_factory)
            ec2.terminate_instances(InstanceIds=[row.instance_id])
        except Exception:
            log.exception("GPU terminate API failed for %s", row.instance_id)
    with engine.begin() as conn:
        conn.execute(
            gpu_sessions.update()
            .where(gpu_sessions.c.id == row.id)
            .values(status="terminated", ended_at=ended, est_cost_usd=est)
        )
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine,
            kind="gpu_lifecycle",
            layer="L0",
            fleet="gpu",
            reasoning=f"nightly GPU terminated after {hours * 60:.0f} min, est. ${est:.2f}",
            detail={"session_id": row.id, "hours": hours, "est_cost_usd": est},
        )
    except Exception:
        log.exception("gpu terminate ai_ops failed")
    return {"terminated": True, "id": row.id, "hours": hours, "est_cost_usd": est}


def orphan_guard(engine: Engine, *, client_factory: Callable | None = None, now: datetime | None = None) -> dict:
    """Terminate any clhear GPU instance older than 5h; publish orphan metric."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=ORPHAN_HOURS)
    orphans: list[str] = []
    settings = get_settings()
    # Ledger-side: sessions left running.
    with engine.connect() as conn:
        stale = conn.execute(
            sa.select(gpu_sessions)
            .where(gpu_sessions.c.status.in_(("running", "launching")))
            .where(gpu_sessions.c.started_at <= cutoff)
        ).all()
    for row in stale:
        terminate_gpu(engine, row.id, client_factory=client_factory, now=now)
        orphans.append(row.id)
    # EC2-side: tagged instances the ledger may have lost.
    if settings.clhear_gpu_subnet_id:
        try:
            ec2 = _ec2(settings.aws_region, client_factory)
            resp = ec2.describe_instances(
                Filters=[
                    {"Name": f"tag:{GPU_TAG}", "Values": [GPU_TAG_VALUE]},
                    {"Name": "instance-state-name", "Values": ["pending", "running"]},
                ]
            )
            old_ids = []
            for rsv in resp.get("Reservations", []):
                for inst in rsv.get("Instances", []):
                    launched = inst.get("LaunchTime")
                    if launched and launched.replace(tzinfo=timezone.utc) <= cutoff:
                        old_ids.append(inst["InstanceId"])
            if old_ids:
                ec2.terminate_instances(InstanceIds=old_ids)
                orphans.extend(old_ids)
        except Exception:
            log.exception("orphan EC2 describe failed")
    _put_orphan_metric(len(orphans), settings.aws_region)
    return {"orphans": orphans, "count": len(orphans)}


def _put_orphan_metric(count: int, region: str) -> None:
    try:
        import boto3

        boto3.client("cloudwatch", region_name=region).put_metric_data(
            Namespace="CLHEAR",
            MetricData=[{"MetricName": "GpuOrphanCount", "Value": float(count), "Unit": "Count"}],
        )
    except Exception:
        log.exception("could not publish GpuOrphanCount")


def _default_ami(ec2) -> str:
    """Resolve the newest Amazon Linux 2023 ECS GPU AMI (x86_64).

    Real names look like ``al2023-ami-ecs-gpu-hvm-2023.0.YYYYMMDD-kernel-6.1-x86_64-ebs``.
    A filter that ends at ``-x86_64`` (no trailing wildcard) matches nothing.
    """
    patterns = (
        "al2023-ami-ecs-gpu-hvm-*-x86_64-ebs",
        "al2023-ami-ecs-gpu-*",
        "amzn2-ami-ecs-gpu-hvm-*-x86_64-ebs",
    )
    images: list[dict] = []
    for name in patterns:
        resp = ec2.describe_images(
            Owners=["amazon"],
            Filters=[
                {"Name": "name", "Values": [name]},
                {"Name": "state", "Values": ["available"]},
                {"Name": "architecture", "Values": ["x86_64"]},
            ],
        )
        images = list(resp.get("Images") or [])
        if images:
            break
    images = sorted(images, key=lambda i: i.get("CreationDate", ""), reverse=True)
    if not images:
        raise RuntimeError("no Amazon Linux 2023 GPU AMI found")
    return images[0]["ImageId"]


def _as_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

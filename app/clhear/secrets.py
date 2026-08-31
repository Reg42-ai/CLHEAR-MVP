"""Resolve frontier keys from SSM at runtime (Lambda has no ECS valueFrom).

Terraform ignores SSM value changes, so baking `aws_ssm_parameter.*.value`
into Lambda env would leave CHANGEME forever. This is the runtime equivalent
of ECS `secrets { valueFrom = ... }`.
"""
from __future__ import annotations

import logging
import os
from typing import Callable

log = logging.getLogger("clhear.secrets")

SSM_ENV = {
    "ANTHROPIC_API_KEY": "/clhear/ANTHROPIC_API_KEY",
    "OPENAI_API_KEY": "/clhear/OPENAI_API_KEY",
    "XAI_API_KEY": "/clhear/XAI_API_KEY",
}


def _ssm_get(name: str, region: str | None = None) -> str:
    import boto3

    resp = boto3.client("ssm", region_name=region or os.environ.get("AWS_REGION") or "us-east-1").get_parameter(
        Name=name, WithDecryption=True
    )
    return str((resp.get("Parameter") or {}).get("Value") or "")


def hydrate_ssm_env(
    *,
    environ: dict | None = None,
    getter: Callable[[str], str] | None = None,
) -> dict[str, str]:
    """Fill empty/CHANGEME frontier env vars from SSM. No-op under CLHEAR_LLM_PROVIDER=fake."""
    env = environ if environ is not None else os.environ
    if str(env.get("CLHEAR_LLM_PROVIDER") or "").lower() == "fake":
        return {}
    filled: dict[str, str] = {}
    get = getter or _ssm_get
    for env_name, param in SSM_ENV.items():
        current = str(env.get(env_name) or "").strip()
        if current and current != "CHANGEME":
            continue
        try:
            value = (get(param) or "").strip()
        except Exception:
            log.info("ssm %s not readable; leaving %s unset", param, env_name)
            continue
        if value and value != "CHANGEME":
            env[env_name] = value
            filled[env_name] = param
    if filled:
        log.info("hydrated frontier secrets from SSM: %s", ",".join(sorted(filled)))
    return filled

"""Resolve runtime secrets from SSM (Lambda has no ECS valueFrom).

Terraform ignores SSM value changes, so baking `aws_ssm_parameter.*.value`
into Lambda env would leave CHANGEME forever. This is the runtime equivalent
of ECS `secrets { valueFrom = ... }`: the Infer token, and the record's
DATABASE_URL once the Aurora cutover has put a DSN in the parameter.
"""
from __future__ import annotations

import logging
import os
from typing import Callable

log = logging.getLogger("clhear.secrets")

SSM_ENV = {
    "INFER_TOKEN": "/clhear/INFER_TOKEN",
}
# Parameter name is itself configurable (infra/variables.tf database_url_ssm_param).
DATABASE_URL_PARAM_ENV = "CLHEAR_DATABASE_URL_SSM_PARAM"


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
    """Fill empty/CHANGEME env vars from SSM.

    Inference vars are skipped under CLHEAR_LLM_PROVIDER=fake; DATABASE_URL is
    looked up whenever CLHEAR_DATABASE_URL_SSM_PARAM names a parameter and the
    variable is not already set (fleets get it from ECS; local dev sets it).
    """
    env = environ if environ is not None else os.environ
    wanted = dict(SSM_ENV)
    if str(env.get("CLHEAR_LLM_PROVIDER") or "").lower() == "fake":
        wanted = {}
    if env.get(DATABASE_URL_PARAM_ENV):
        wanted["DATABASE_URL"] = str(env[DATABASE_URL_PARAM_ENV])
    filled: dict[str, str] = {}
    get = getter or _ssm_get
    for env_name, param in wanted.items():
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
        log.info("hydrated secrets from SSM: %s", ",".join(sorted(filled)))
    return filled

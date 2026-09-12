#!/usr/bin/env python3
"""Pull 4b/9b/27b into a local Ollama store and sync to the deploy S3 cache.

Night one should not be a multi-hour Hub pull. GPU userdata and the CPU
sidecar restore from s3://clhear-deploy-<account>/ollama-models.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

CPU_MODELS = ("qwen3.5:4b", "qwen3.5:9b")
GPU_MODELS = ("qwen3.6:27b",)
ALL_MODELS = CPU_MODELS + GPU_MODELS


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument(
        "--uri",
        default=os.environ.get(
            "CLHEAR_OLLAMA_MODEL_CACHE_S3",
            "s3://clhear-deploy-730649732189/ollama-models",
        ),
    )
    parser.add_argument("--data-dir", default=os.environ.get("OLLAMA_MODELS", os.path.expanduser("~/.ollama")))
    parser.add_argument("--models", default=",".join(ALL_MODELS))
    parser.add_argument("--skip-pull", action="store_true")
    args = parser.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    os.environ.setdefault("OLLAMA_MODELS", args.data_dir)
    if not args.skip_pull:
        for model in models:
            print(f"== ollama pull {model} ==", flush=True)
            subprocess.check_call(["ollama", "pull", model])
    print(f"== aws s3 sync {args.data_dir} {args.uri} ==", flush=True)
    subprocess.check_call(
        ["aws", "s3", "sync", args.data_dir, args.uri, "--region", args.region]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

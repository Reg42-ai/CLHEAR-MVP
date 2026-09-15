#!/usr/bin/env bash
# Compatibility entrypoint: deployments are reviewed GitHub Actions jobs.
# This script must never upload a local corpus or apply Terraform.
set -euo pipefail
echo 'Use the deploy-l1 workflow in Reg42-ai/CLHEAR-MVP on main.' >&2
echo 'See docs/L1_DEPLOYMENT.md for role setup, worker verification and recovery.' >&2
exit 1

#!/usr/bin/env bash
set -euo pipefail
PRO_SCRIPT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "${PRO_BOOTSTRAP_PYTHON:-python3}" "$PRO_SCRIPT_ROOT/scripts/pro/pro_bootstrap.py" "$@"

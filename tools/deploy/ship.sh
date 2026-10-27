#!/usr/bin/env bash
# Single receipt-bound release entry. No full-model replacement/key provisioning.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$here/ship-ledger-release.py" "$@"

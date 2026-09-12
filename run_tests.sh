#!/usr/bin/env bash
# Run the offline regression suite. No API key or network access required.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -v "$@"

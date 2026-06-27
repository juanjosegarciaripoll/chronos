#!/usr/bin/env bash
# Point git at the repo-tracked hooks in .githooks/ (one-time, per clone).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath .githooks
echo "core.hooksPath set to .githooks — ruff format/check will run on commit."

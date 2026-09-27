#!/usr/bin/env bash
# Run the lockfile-pinned Fallow analyzer over the full checkout, exactly as CI
# does. Full analysis avoids a stale upstream/main or an unrelated fork base
# silently excluding findings. No fetch, model credentials, or global binary
# is needed. Platform differences in clone detection can still occur.
set -euo pipefail

exec npx --no-install fallow --root . --format human --fail-on-issues

#!/bin/bash
# Prepare the repo root for `eb deploy` of one role.
#
#   scripts/deploy/apply_overlay.sh web
#   scripts/deploy/apply_overlay.sh worker
#
# Removes any deploy files left by a previous role, then copies
# deploy/common/ and deploy/<role>/ over the root. The Elastic Beanstalk CLI
# bundles the working tree (there is an .ebignore), so the copied files are
# picked up as-is. Hook scripts must be executable or EB skips them.
set -euo pipefail

ROLE="${1:?usage: apply_overlay.sh <web|worker>}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OVERLAY="$ROOT/deploy/$ROLE"

if [ ! -d "$OVERLAY" ]; then
    echo "Unknown role '$ROLE': no directory $OVERLAY" >&2
    exit 1
fi

cd "$ROOT"
rm -rf .ebextensions .platform Procfile

cp -R deploy/common/. .
cp -R "$OVERLAY"/. .

if [ -d .platform/hooks ]; then
    find .platform/hooks -type f -name '*.sh' -exec chmod +x {} +
fi

echo "Applied deploy overlay '$ROLE':"
find .ebextensions .platform Procfile -type f 2>/dev/null | sort

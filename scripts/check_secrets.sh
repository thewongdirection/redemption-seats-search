#!/usr/bin/env bash
# Fail if anything that looks like a credential is present in the working tree
# (tracked files plus untracked files that are not git-ignored).
# Run before every commit/push; also wired into CI.
set -euo pipefail
cd "$(dirname "$0")/.."

patterns=(
  'pro_[A-Za-z0-9]{20,}'                 # seats.aero Partner API keys
  'sk-[A-Za-z0-9_-]{20,}'                # OpenAI / Anthropic style keys
  'ghp_[A-Za-z0-9]{30,}'                 # GitHub PATs
  'github_pat_[A-Za-z0-9_]{30,}'
  'AKIA[0-9A-Z]{16}'                     # AWS access key IDs
  'xox[baprs]-[A-Za-z0-9-]{10,}'         # Slack tokens
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  '(SEATS_AERO_API_KEY|SEATS_API_KEY|Partner-Authorization)[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9_-]{16,}'
)

files=()
while IFS= read -r -d '' f; do
  [ -f "$f" ] && files+=("$f")
done < <(git ls-files -z --cached --others --exclude-standard)

if [ "${#files[@]}" -eq 0 ]; then
  echo "check_secrets: no files to scan"
  exit 0
fi

status=0
for pattern in "${patterns[@]}"; do
  if hits=$(grep -nIE -- "$pattern" "${files[@]}" 2>/dev/null); then
    echo "check_secrets: possible secret matching /$pattern/:"
    printf '%s\n' "$hits" | sed -E 's/([A-Za-z0-9_-]{4})[A-Za-z0-9_-]{12,}/\1<redacted>/g'
    status=1
  fi
done

if [ "$status" -eq 0 ]; then
  echo "check_secrets: clean (${#files[@]} files scanned)"
fi
exit "$status"

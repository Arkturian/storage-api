#!/usr/bin/env bash
# Deploy origin/main to the arkserver storage-api instance — the only instance
# serving tenant arkturian (api-storage.arkturian.com). Other instances
# (aiserver/oneal, pdrei) are never touched by this script.
#
# Why a local script instead of the CI job: the live directory is NOT a git
# checkout (see DEPLOYED_REVISION.txt there). Code is deployed by file copy,
# so this script copies exactly the files that changed between the deployed
# revision and the target, after verifying that the live files are byte-
# identical to the deployed revision (no unrecorded drift).
#
# Order: preflight → tests → backup → copy → restart → health → verify.
# Any failure after the copy restores the backed-up files and restarts.
#
# Usage: deploy-arkserver.sh [--dry-run] [--from <deployed-sha>]
#   --from      deployed revision, needed once while LIVE/.deployed-rev is missing
#   --dry-run   run every check and the tests, print the plan, change nothing
#
# Env: PYTEST_PYTHONPATH  extra PYTHONPATH that provides pytest (the live venv
#                         has none); tests are mandatory, missing pytest aborts.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"

LIVE="${STORAGE_LIVE_DIR:-/var/www/api-storage.arkturian.com}"
SERVICE="storage-api.service"
DB="/var/lib/storage-api/storage.db"
HEALTH_URL="http://127.0.0.1:8080/health"
BACKUP_ROOT="/var/backups/storage-api"
EXPECTED_HOST="arkserver.arkturian.com"

dry_run=false
from_rev=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) dry_run=true; shift ;;
    --from) from_rev="${2:?--from needs a revision}"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

die() { echo "❌ $*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

# ---------------------------------------------------------------- preflight
step "Preflight"
[[ "$(hostname -f 2>/dev/null || hostname)" == "$EXPECTED_HOST" ]] || die "runs only on $EXPECTED_HOST"
[[ -f "$LIVE/main.py" && -f "$LIVE/DEPLOYED_REVISION.txt" ]] || die "$LIVE does not look like the live storage-api"
cd "$REPO_ROOT"
[[ -z "$(git status --porcelain)" ]] || die "working tree not clean"
git fetch -q origin main
target="$(git rev-parse origin/main)"
short="$(git rev-parse --short "$target")"

if [[ -z "$from_rev" ]]; then
  [[ -f "$LIVE/.deployed-rev" ]] || die "no $LIVE/.deployed-rev yet — pass --from <deployed sha> once"
  from_rev="$(tr -d '[:space:]' < "$LIVE/.deployed-rev")"
fi
from_rev="$(git rev-parse --verify -q "${from_rev}^{commit}")" || die "unknown deployed revision"
git merge-base --is-ancestor "$from_rev" "$target" || die "deployed revision is not an ancestor of origin/main"
[[ "$from_rev" != "$target" ]] || { echo "Already deployed: $short"; exit 0; }

# Only runtime code is copied. Repo tooling, docs and tests stay out of LIVE.
mapfile -t changes < <(git diff --name-status --no-renames "$from_rev" "$target")
files=()
for line in "${changes[@]}"; do
  status="${line%%$'\t'*}"; path="${line#*$'\t'}"
  case "$path" in
    tests/*|docs/*|.github/*|.devops/*|devops|*.md|.gitignore|.env*.example) continue ;;
    requirements.txt) die "requirements.txt changed — LIVE keeps a deliberate local-path difference; deploy that by hand" ;;
  esac
  [[ "$status" == "M" || "$status" == "A" ]] || die "$status $path — deletions need a manual decision"
  files+=("$path")
done
[[ ${#files[@]} -gt 0 ]] || die "no runtime files changed between $from_rev and $short"

# Every modified file must be byte-identical to the deployed revision, else
# LIVE carries something unrecorded and copying would silently drop it.
for f in "${files[@]}"; do
  if git cat-file -e "$from_rev:$f" 2>/dev/null; then
    git show "$from_rev:$f" | cmp -s - "$LIVE/$f" || die "drift: $LIVE/$f differs from deployed $from_rev"
  else
    [[ ! -e "$LIVE/$f" ]] || die "drift: $LIVE/$f exists but is new in this release"
  fi
done
echo "deployed: $(git rev-parse --short "$from_rev")   target: $short"
printf '  %s\n' "${files[@]}"

# -------------------------------------------------------------------- tests
step "Tests on the exact target tree"
work="$(mktemp -d)"; chmod 700 "$work"
trap 'rm -rf "$work"' EXIT
git archive "$target" | tar -x -C "$work"
mkdir -p "$work/_sd" "$work/_su"; echo '{}' > "$work/_tc.json"
PYTHONPATH="${PYTEST_PYTHONPATH:-}" "$LIVE/venv/bin/python" -c 'import pytest' 2>/dev/null \
  || die "pytest not importable — set PYTEST_PYTHONPATH (tests are not optional)"
( cd "$work" && PYTHONPATH="${PYTEST_PYTHONPATH:-}" TENANT_CONFIG_PATH="$work/_tc.json" \
    STORAGE_DATA_DIR="$work/_sd" STORAGE_UPLOAD_DIR="$work/_su" \
    "$LIVE/venv/bin/python" -m pytest -q -p no:cacheprovider tests ) || die "tests failed — nothing deployed"

if [[ "$dry_run" == true ]]; then
  echo; echo "Dry run: all checks passed, nothing changed."; exit 0
fi

# ------------------------------------------------------------------- backup
step "Backup"
B="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)-$short"
sudo install -d -m 700 -o www-data -g www-data "$B"
for f in "${files[@]}"; do
  [[ -e "$LIVE/$f" ]] || continue
  sudo install -d -m 700 "$B/$(dirname "$f")"
  sudo cp -p "$LIVE/$f" "$B/$f"
done
sudo cp -p "$LIVE/DEPLOYED_REVISION.txt" "$B/"
# Online backup: consistent even while the service writes (WAL), mode 600.
sudo -u www-data python3 -I - "$DB" "$B/storage.db" <<'PY'
import os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
os.umask(0o077)
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
s.backup(d); d.close(); s.close()
ok = sqlite3.connect(dst).execute("pragma integrity_check").fetchone()[0]
sys.exit(0 if ok == "ok" else f"backup integrity: {ok}")
PY
echo "$B"

restore() {
  echo "↩️  restoring from $B" >&2
  for f in "${files[@]}"; do
    if [[ -e "$B/$f" ]]; then sudo cp -p "$B/$f" "$LIVE/$f"; else sudo rm -f "$LIVE/$f"; fi
  done
  sudo systemctl restart "$SERVICE" || true
}

# --------------------------------------------------------------------- copy
step "Copy"
for f in "${files[@]}"; do
  if [[ -e "$LIVE/$f" ]]; then
    # Write in place: keeps owner, mode and ACLs of the live file.
    git show "$target:$f" | sudo tee "$LIVE/$f" > /dev/null
  else
    sudo install -d "$LIVE/$(dirname "$f")"
    git show "$target:$f" | sudo install -m 644 /dev/stdin "$LIVE/$f"
  fi
done

# ------------------------------------------------------------------ restart
step "Restart $SERVICE"
since="$(date '+%Y-%m-%d %H:%M:%S')"
sudo systemctl restart "$SERVICE"
healthy=false
for _ in $(seq 1 60); do
  if [[ "$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL")" == "200" ]]; then healthy=true; break; fi
  sleep 1
done
if [[ "$healthy" != true ]]; then restore; die "health check failed — previous files restored"; fi
if sudo journalctl -u "$SERVICE" --since "$since" --no-pager | grep -q "Traceback"; then
  sudo journalctl -u "$SERVICE" --since "$since" --no-pager | tail -40 >&2
  restore; die "traceback after restart — previous files restored"
fi

# ------------------------------------------------------------------- verify
step "Verify"
for f in "${files[@]}"; do
  git show "$target:$f" | cmp -s - "$LIVE/$f" || { restore; die "verify: $f differs after copy"; }
done
sudo journalctl -u "$SERVICE" --since "$since" --no-pager | grep -i "migration" || true
echo "$target" | sudo tee "$LIVE/.deployed-rev" > /dev/null
note="origin/main @ $short ($(date +%F), deploy-arkserver.sh, backup $B; before: "
sudo sed -i "0,/origin\/main @ /s|origin/main @ |$note|" "$LIVE/DEPLOYED_REVISION.txt"
echo; echo "✅ $short live on arkserver ($SERVICE). Backup: $B"

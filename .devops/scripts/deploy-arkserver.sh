#!/usr/bin/env bash
# Deploy one exact released commit to the arkserver storage-api instance — the
# only instance serving tenant arkturian (api-storage.arkturian.com). Other
# instances (aiserver/oneal, pdrei) are never touched by this script.
#
# Why a local script instead of the CI job: the live directory is NOT a git
# checkout (see DEPLOYED_REVISION.txt there). Code is deployed by file copy,
# so this script copies exactly the files that changed between the deployed
# revision and the target, after verifying that the live files are byte-
# identical to the deployed revision (no unrecorded drift).
#
# Order: lock → preflight → tests → backup → copy → restart → health →
# verify → markers. From the first live mutation until the markers are
# written, ANY failure (copy, restart, health, verify, marker) rolls back:
# files and markers restored from the backup, service restarted, health
# checked. The original exit code is kept; a failed rollback is reported
# loudly. No success marker is written on any failure path.
#
# Usage: deploy-arkserver.sh --expect <sha> [--from <deployed-sha>] [--dry-run]
#   --expect    the exact commit to deploy; must equal origin/main (release.sh
#               passes the SHA it just pushed)
#   --from      deployed revision, needed once while LIVE/.deployed-rev is missing
#   --dry-run   run every check and the tests, print the plan, change nothing
#
# Env: PYTEST_PYTHONPATH  extra PYTHONPATH that provides pytest (the live venv
#                         has none); tests are mandatory, missing pytest aborts.
# Path overrides exist only for the isolated self-test (.devops/tests/):
#   STORAGE_LIVE_DIR STORAGE_DB_PATH STORAGE_BACKUP_ROOT DEPLOY_LOCK_FILE
#   DEPLOY_BACKUP_OWNER DEPLOY_BACKUP_GROUP DEPLOY_HEALTH_TRIES

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"

LIVE="${STORAGE_LIVE_DIR:-/var/www/api-storage.arkturian.com}"
DB="${STORAGE_DB_PATH:-/var/lib/storage-api/storage.db}"
BACKUP_ROOT="${STORAGE_BACKUP_ROOT:-/var/backups/storage-api}"
LOCK_FILE="${DEPLOY_LOCK_FILE:-/run/lock/storage-api-deploy.lock}"
BACKUP_OWNER="${DEPLOY_BACKUP_OWNER:-www-data}"
BACKUP_GROUP="${DEPLOY_BACKUP_GROUP:-www-data}"
HEALTH_TRIES="${DEPLOY_HEALTH_TRIES:-60}"
SERVICE="storage-api.service"
HEALTH_URL="http://127.0.0.1:8080/health"
EXPECTED_HOST="arkserver.arkturian.com"

dry_run=false
from_rev=""
expect=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) dry_run=true; shift ;;
    --from) from_rev="${2:?--from needs a revision}"; shift 2 ;;
    --expect) expect="${2:?--expect needs a revision}"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }

# ------------------------------------------------------- failure handling
# mutated=true from the first write into LIVE until the markers are written.
mutated=false
files=()
B=""
work=""

health_ok() {
  local i
  for ((i = 0; i < HEALTH_TRIES; i++)); do
    [[ "$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)" == "200" ]] && return 0
    sleep 1
  done
  return 1
}

rollback() {
  # Runs with errexit off: every step is attempted, every failure counted.
  local bad=0 f
  echo "↩️  rolling back from $B" >&2
  for f in "${files[@]}"; do
    if [[ -e "$B/live/$f" ]]; then
      sudo cp -p "$B/live/$f" "$LIVE/$f" || bad=1
      cmp -s "$B/live/$f" "$LIVE/$f" || { echo "   not restored: $f" >&2; bad=1; }
    else
      sudo rm -f "$LIVE/$f" || bad=1
    fi
  done
  sudo cp -p "$B/markers/DEPLOYED_REVISION.txt" "$LIVE/DEPLOYED_REVISION.txt" || bad=1
  if [[ -e "$B/markers/.deployed-rev" ]]; then
    sudo cp -p "$B/markers/.deployed-rev" "$LIVE/.deployed-rev" || bad=1
  else
    sudo rm -f "$LIVE/.deployed-rev" || bad=1
  fi
  sudo systemctl restart "$SERVICE" || { echo "   restart during rollback failed" >&2; bad=1; }
  health_ok || { echo "   health after rollback failed" >&2; bad=1; }
  return "$bad"
}

fail() {
  # fail <exit code> <message>: report, roll back if LIVE was touched, exit.
  local rc="$1"; shift
  trap - ERR
  set +e
  echo "❌ $*" >&2
  if [[ "$mutated" == true ]]; then
    if rollback; then
      echo "↩️  rollback complete: previous files and markers restored, service healthy" >&2
    else
      echo "‼️  ROLLBACK FAILED — LIVE may be inconsistent; backup: $B" >&2
    fi
  fi
  exit "$rc"
}
die() { fail 1 "$*"; }
trap 'fail $? "command failed (line $LINENO): $BASH_COMMAND"' ERR
trap 'if [[ -n "$work" ]]; then rm -rf "$work"; fi' EXIT

# --------------------------------------------------------------------- lock
# One lock for the whole run: two invocations cannot both pass the drift
# check before either mutates LIVE.
exec 9>>"$LOCK_FILE"
flock -n 9 || die "another deploy holds $LOCK_FILE"

# ---------------------------------------------------------------- preflight
step "Preflight"
[[ -n "$expect" ]] || die "--expect <sha> is required (the exact released commit)"
[[ "$(hostname -f 2>/dev/null || hostname)" == "$EXPECTED_HOST" ]] || die "runs only on $EXPECTED_HOST"
[[ -f "$LIVE/main.py" && -f "$LIVE/DEPLOYED_REVISION.txt" ]] || die "$LIVE does not look like the live storage-api"
cd "$REPO_ROOT"
[[ -z "$(git status --porcelain)" ]] || die "working tree not clean"
git fetch -q origin main
target="$(git rev-parse --verify -q "${expect}^{commit}")" || die "unknown commit $expect"
[[ "$(git rev-parse origin/main)" == "$target" ]] || die "origin/main is not $expect — refusing to deploy anything else"
short="$(git rev-parse --short "$target")"

if [[ -z "$from_rev" ]]; then
  [[ -f "$LIVE/.deployed-rev" ]] || die "no $LIVE/.deployed-rev yet — pass --from <deployed sha> once"
  from_rev="$(tr -d '[:space:]' < "$LIVE/.deployed-rev")"
fi
from_rev="$(git rev-parse --verify -q "${from_rev}^{commit}")" || die "unknown deployed revision"
[[ "$from_rev" != "$target" ]] || { echo "Already deployed: $short"; exit 0; }
git merge-base --is-ancestor "$from_rev" "$target" || die "deployed revision is not an ancestor of $short"

# Only runtime code is copied. Repo tooling, docs and tests stay out of LIVE.
mapfile -t changes < <(git diff --name-status --no-renames "$from_rev" "$target")
for line in "${changes[@]}"; do
  status="${line%%$'\t'*}"; path="${line#*$'\t'}"
  case "$path" in
    tests/*|docs/*|.github/*|.devops/*|devops|*.md|.gitignore|.env*.example) continue ;;
    requirements.txt) die "requirements.txt changed — LIVE keeps a deliberate local-path difference; deploy that by hand" ;;
  esac
  [[ "$status" == "M" || "$status" == "A" ]] || die "$status $path — deletions need a manual decision"
  files+=("$path")
done

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
if [[ ${#files[@]} -gt 0 ]]; then
  printf '  %s\n' "${files[@]}"
else
  echo "  no runtime file changed: only the markers advance, no copy, no restart"
fi

# -------------------------------------------------------------------- tests
step "Tests on the exact target tree"
work="$(mktemp -d)"; chmod 700 "$work"
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
sudo install -d -m 700 -o "$BACKUP_OWNER" -g "$BACKUP_GROUP" "$B"
sudo install -d -m 700 "$B/live" "$B/markers"
for f in "${files[@]}"; do
  [[ -e "$LIVE/$f" ]] || continue
  sudo install -d -m 700 "$B/live/$(dirname "$f")"
  sudo cp -p "$LIVE/$f" "$B/live/$f"
done
sudo cp -p "$LIVE/DEPLOYED_REVISION.txt" "$B/markers/"
[[ ! -e "$LIVE/.deployed-rev" ]] || sudo cp -p "$LIVE/.deployed-rev" "$B/markers/"
# Online backup: consistent even while the service writes (WAL), mode 600.
sudo -u "$BACKUP_OWNER" python3 -I - "$DB" "$B/storage.db" <<'PY'
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

# ---------------------------------------------- live mutation (rollback on)
mutated=true

if [[ ${#files[@]} -gt 0 ]]; then
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

  step "Restart $SERVICE"
  since="$(date '+%Y-%m-%d %H:%M:%S')"
  sudo systemctl restart "$SERVICE"
  health_ok || die "health check failed after restart"
  if sudo journalctl -u "$SERVICE" --since "$since" --no-pager | grep -q "Traceback"; then
    sudo journalctl -u "$SERVICE" --since "$since" --no-pager | tail -40 >&2
    die "traceback after restart"
  fi

  step "Verify"
  for f in "${files[@]}"; do
    git show "$target:$f" | cmp -s - "$LIVE/$f" || die "verify: $f differs after copy"
  done
  sudo journalctl -u "$SERVICE" --since "$since" --no-pager | grep -i "migration" || true

fi

step "Markers"
note="origin/main @ $short ($(date +%F), deploy-arkserver.sh, backup $B); before: origin/main @ "
echo "$target" | sudo tee "$LIVE/.deployed-rev.new" > /dev/null
sudo mv -f "$LIVE/.deployed-rev.new" "$LIVE/.deployed-rev"
sudo sed -i "0,/origin\/main @ /s|origin/main @ |$note|" "$LIVE/DEPLOYED_REVISION.txt"
grep -q "origin/main @ $short " "$LIVE/DEPLOYED_REVISION.txt" || die "DEPLOYED_REVISION.txt not updated"

mutated=false
trap - ERR
echo; echo "✅ $short live on arkserver ($SERVICE). Backup: $B"

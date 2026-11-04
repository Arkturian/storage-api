#!/usr/bin/env bash
# Isolated self-test for .devops/scripts/deploy-arkserver.sh.
#
# Never touches real services, files or databases: everything lives in a
# temp dir; sudo/systemctl/curl/journalctl/hostname/tee are fakes on PATH;
# git uses a local bare repo as origin; the "DB" is a throwaway SQLite file.
#
# Usage: PYTEST_PYTHONPATH=<dir with pytest> .devops/tests/test-deploy-arkserver.sh
#        (PYTHON=<interpreter> optional, default python3)

set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT="$HERE/../scripts/deploy-arkserver.sh"
PY="${PYTHON:-python3}"
T="$(mktemp -d)"; chmod 700 "$T"
trap 'sudo -n rm -rf "$T/backups" 2>/dev/null; rm -rf "$T"' EXIT
pass=0; failn=0
ok()  { echo "  ✓ $*"; pass=$((pass + 1)); }
bad() { echo "  ✗ $*"; failn=$((failn + 1)); }

# ------------------------------------------------------------------ fakes
mkdir -p "$T/bin"
cat > "$T/bin/sudo" <<'EOF'
#!/usr/bin/env bash
# Default: drop sudo, run as the invoking user. With FAKE_REAL_SUDO=1 (case K)
# file operations run under REAL sudo so the private backup is truly
# unreadable to the invoking user — but only an allowlist of commands, only
# on paths inside the test dir, and systemctl/journalctl always stay fakes.
user=""
while [[ "${1:-}" == -* ]]; do case "$1" in -u) user="$2"; shift 2 ;; *) shift ;; esac; done
case "$1" in systemctl|journalctl) exec "$FAKE_BIN/$1" "${@:2}" ;; esac
[[ "${FAKE_REAL_SUDO:-0}" == 1 ]] || exec "$@"
case "$1" in install|cp|rm|mv|tee|sed|test|cmp|python3) ;; *) echo "fake sudo: $1 not allowed" >&2; exit 97 ;; esac
for a in "${@:2}"; do
  if [[ "$a" == /* && "$a" != "$FAKE_ROOT"/* && "$a" != /dev/stdin ]]; then
    echo "fake sudo: path outside test dir: $a" >&2; exit 98
  fi
done
exec /usr/bin/sudo -n ${user:+-u "$user"} "$@"
EOF
cat > "$T/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
# Pops the next exit code from $FAKE/restart_rc (one per line; empty → 0).
echo "systemctl $*" >> "$FAKE/events"
rc=0
if [[ -s "$FAKE/restart_rc" ]]; then rc="$(head -n1 "$FAKE/restart_rc")"; sed -i 1d "$FAKE/restart_rc"; fi
exit "$rc"
EOF
cat > "$T/bin/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s' "$(cat "$FAKE/health" 2>/dev/null || echo 200)"
EOF
cat > "$T/bin/journalctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > "$T/bin/hostname" <<'EOF'
#!/usr/bin/env bash
echo arkserver.arkturian.com
EOF
cat > "$T/bin/tee" <<'EOF'
#!/usr/bin/env bash
# Fails (after a partial write) when the target matches $FAKE/fail_tee.
for a in "$@"; do
  if [[ -s "$FAKE/fail_tee" && "$a" == *"$(cat "$FAKE/fail_tee")" ]]; then
    echo "partial" > "$a"; echo "tee: simulated write error" >&2; exit 5
  fi
done
exec /usr/bin/tee "$@"
EOF
chmod 755 "$T/bin/"*

# -------------------------------------------------------- repo + "origin"
git init -q --bare "$T/origin.git"
git init -q -b main "$T/repo"
cd "$T/repo"
git config user.email t@t; git config user.name t
mkdir -p .devops/scripts storage tests
cp "$SCRIPT" .devops/scripts/
echo v1 > main.py; echo v1 > storage/routes.py; echo m > models.py
printf 'def test_ok():\n    assert True\n' > tests/test_ok.py
git add -A; git commit -qm c1; C1="$(git rev-parse HEAD)"
echo v2 > main.py; echo v2 > storage/routes.py
git commit -qam c2; C2="$(git rev-parse HEAD)"
git remote add origin "$T/origin.git"; git push -q origin main

# ------------------------------------------------------------ per-case env
fresh_live() {
  sudo -n rm -rf "$T/backups" 2>/dev/null
  rm -rf "$T/live" "$T/backups" "$T/fake"
  mkdir -p "$T/live/storage" "$T/live/venv/bin" "$T/backups" "$T/fake"
  echo v1 > "$T/live/main.py"; echo v1 > "$T/live/storage/routes.py"; echo m > "$T/live/models.py"
  printf 'Code == storage-api origin/main @ %s (seed).\n' "${C1:0:7}" > "$T/live/DEPLOYED_REVISION.txt"
  ln -s "$(command -v "$PY")" "$T/live/venv/bin/python"
  "$PY" -c "import sqlite3; c=sqlite3.connect('$T/db.sqlite'); c.execute('create table if not exists x(a)'); c.commit()"
}
run() {
  PATH="$T/bin:$PATH" FAKE="$T/fake" FAKE_BIN="$T/bin" FAKE_ROOT="$T" \
  STORAGE_LIVE_DIR="$T/live" STORAGE_DB_PATH="$T/db.sqlite" STORAGE_BACKUP_ROOT="$T/backups" \
  DEPLOY_LOCK_FILE="$T/lock" DEPLOY_BACKUP_OWNER="${BOWNER:-$(id -un)}" DEPLOY_BACKUP_GROUP="${BGROUP:-$(id -gn)}" \
  DEPLOY_HEALTH_TRIES=2 \
    "$T/repo/.devops/scripts/deploy-arkserver.sh" "$@" > "$T/out" 2>&1
}
live_is_v1() {
  [[ "$(cat "$T/live/main.py")" == v1 && "$(cat "$T/live/storage/routes.py")" == v1 ]]
}
no_marker() {
  [[ ! -e "$T/live/.deployed-rev" ]] && grep -q "(seed)" "$T/live/DEPLOYED_REVISION.txt" \
    && ! grep -q deploy-arkserver.sh "$T/live/DEPLOYED_REVISION.txt"
}

echo "A) restart fails (exit 42), rollback succeeds"
fresh_live; printf '42\n0\n' > "$T/fake/restart_rc"
run --expect "$C2" --from "$C1"; rc=$?
[[ $rc -eq 42 ]] && ok "original exit code 42 kept" || bad "exit $rc, expected 42"
live_is_v1 && ok "files restored" || bad "files not restored"
no_marker && ok "no success marker" || bad "marker written"
[[ "$(grep -c restart "$T/fake/events")" -eq 2 ]] && ok "rollback restarted the service" || bad "restart count $(grep -c restart "$T/fake/events")"
grep -q "rollback complete" "$T/out" && ok "rollback reported" || bad "no rollback report"

echo "B) mid-copy write error on the second file"
fresh_live; echo storage/routes.py > "$T/fake/fail_tee"
run --expect "$C2" --from "$C1"; rc=$?
[[ $rc -ne 0 ]] && ok "fails (exit $rc)" || bad "exit 0"
live_is_v1 && ok "first (already copied) and partial file restored" || bad "files: $(cat "$T/live/main.py")/$(cat "$T/live/storage/routes.py")"
no_marker && ok "no success marker" || bad "marker written"
grep -q "rollback complete" "$T/out" && ok "rollback reported" || bad "no rollback report"

echo "C) restart keeps failing: rollback failure surfaced"
fresh_live; printf '42\n42\n' > "$T/fake/restart_rc"
run --expect "$C2" --from "$C1"; rc=$?
[[ $rc -eq 42 ]] && ok "original exit code 42 kept" || bad "exit $rc"
grep -q "ROLLBACK FAILED" "$T/out" && ok "rollback failure reported" || bad "rollback failure hidden"
live_is_v1 && ok "files still restored" || bad "files not restored"
no_marker && ok "no success marker" || bad "marker written"

echo "D) health fails after restart"
fresh_live; echo 503 > "$T/fake/health"
run --expect "$C2" --from "$C1"; rc=$?
[[ $rc -ne 0 ]] && ok "fails (exit $rc)" || bad "exit 0"
live_is_v1 && ok "files restored" || bad "files not restored"
grep -q "ROLLBACK FAILED" "$T/out" && ok "unhealthy rollback reported as failed" || bad "rollback health not checked"
no_marker && ok "no success marker" || bad "marker written"

echo "E) --expect differs from origin/main"
fresh_live
run --expect "$C1" --from "$C1"; rc=$?
[[ $rc -ne 0 ]] && grep -q "refusing to deploy anything else" "$T/out" && ok "refused" || bad "exit $rc"
live_is_v1 && no_marker && [[ ! -e "$T/fake/events" ]] && ok "nothing touched" || bad "touched"

echo "F) lock held by another run"
fresh_live
exec 8>>"$T/lock"; flock -n 8
run --expect "$C2" --from "$C1"; rc=$?
exec 8>&-
[[ $rc -ne 0 ]] && grep -q "another deploy holds" "$T/out" && ok "refused while locked" || bad "exit $rc"
live_is_v1 && [[ ! -e "$T/fake/events" ]] && ok "nothing touched" || bad "touched"

echo "G) --expect missing"
fresh_live
run --from "$C1"; rc=$?
[[ $rc -ne 0 ]] && grep -q "expect <sha> is required" "$T/out" && ok "refused" || bad "exit $rc"

echo "H) success"
fresh_live
run --expect "$C2" --from "$C1"; rc=$?
[[ $rc -eq 0 ]] && ok "exit 0" || { bad "exit $rc"; tail -20 "$T/out"; }
[[ "$(cat "$T/live/main.py")" == v2 && "$(cat "$T/live/storage/routes.py")" == v2 ]] && ok "files are v2" || bad "files not v2"
[[ "$(cat "$T/live/.deployed-rev")" == "$C2" ]] && ok ".deployed-rev = target" || bad ".deployed-rev wrong"
grep -q "origin/main @ ${C2:0:7} " "$T/live/DEPLOYED_REVISION.txt" && ok "DEPLOYED_REVISION updated" || bad "DEPLOYED_REVISION not updated"
b="$(ls -d "$T"/backups/*)"; [[ "$(stat -c %a "$b")" == 700 && "$(stat -c %a "$b/storage.db")" == 600 ]] && ok "backup 700/600" || bad "backup modes $(stat -c %a "$b" "$b/storage.db" | tr '\n' ' ')"

echo "I) second run: already deployed, nothing touched"
: > "$T/fake/events"
run --expect "$C2"; rc=$?
[[ $rc -eq 0 ]] && grep -q "Already deployed" "$T/out" && [[ ! -s "$T/fake/events" ]] && ok "idempotent" || bad "exit $rc"

echo "J) tooling-only release: markers advance, no copy, no restart"
cd "$T/repo"; echo "# tool" >> .devops/scripts/deploy-arkserver.sh; git commit -qam c3; C3="$(git rev-parse HEAD)"; git push -q origin main; cd - > /dev/null
: > "$T/fake/events"
run --expect "$C3"; rc=$?
[[ $rc -eq 0 ]] && ok "exit 0" || { bad "exit $rc"; tail -5 "$T/out"; }
[[ ! -s "$T/fake/events" ]] && ok "no restart" || bad "service touched"
[[ "$(cat "$T/live/main.py")" == v2 && "$(cat "$T/live/.deployed-rev")" == "$C3" ]] && ok "files unchanged, marker = c3" || bad "state wrong"

echo "K) private backup unreadable to the invoking user (real sudo, root 700): restart fails, rollback restores"
if /usr/bin/sudo -n true 2>/dev/null; then
  cd "$T/repo"; echo v4 > main.py; echo v4 > storage/routes.py; git commit -qam c4; C4="$(git rev-parse HEAD)"; git push -q origin main; cd - > /dev/null
  fresh_live; echo "$C3" > "$T/live/.deployed-rev"
  echo v2 > "$T/live/main.py"; echo v2 > "$T/live/storage/routes.py"
  printf '42\n0\n' > "$T/fake/restart_rc"
  FAKE_REAL_SUDO=1 BOWNER=root BGROUP=root run --expect "$C4"; rc=$?
  b="$(sudo -n ls -d "$T"/backups/* 2>/dev/null | head -1)"
  [[ -n "$b" && ! -r "$b" ]] && ok "backup really unreadable to $(id -un)" || bad "backup readable or missing: $b"
  [[ $rc -eq 42 ]] && ok "original exit code 42 kept" || { bad "exit $rc"; tail -8 "$T/out"; }
  [[ "$(cat "$T/live/main.py" 2>/dev/null)" == v2 && "$(cat "$T/live/storage/routes.py" 2>/dev/null)" == v2 ]] \
    && ok "files restored from the private backup (not deleted)" || bad "files: $(cat "$T/live/main.py" 2>&1)/$(cat "$T/live/storage/routes.py" 2>&1)"
  [[ "$(cat "$T/live/.deployed-rev")" == "$C3" ]] && grep -q "(seed)" "$T/live/DEPLOYED_REVISION.txt" && ok "markers restored" || bad "markers wrong"
  grep -q "rollback complete" "$T/out" && ok "rollback reported" || bad "no rollback report"
else
  echo "  - skipped: needs passwordless sudo"; failn=$((failn + 1))
fi

echo; echo "passed: $pass  failed: $failn"
[[ $failn -eq 0 ]]

#!/usr/bin/env bash

set -euo pipefail

# Resolve repository root relative to this script so it works anywhere.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
DEV_BRANCH="dev"
MAIN_BRANCH="main"
CHECKOUT_SCRIPT="$SCRIPT_DIR/checkout-branch.sh"
BUILD_SCRIPT="$SCRIPT_DIR/build-local.sh"

usage() {
  cat <<'USAGE'
Usage: release.sh [--no-build] [--deploy arkserver] [--from <deployed-sha>]

Fast-forwards main from dev and pushes it. A push to main deploys nothing by
itself (deploy.yml runs only on explicit workflow_dispatch). By default the
local build script runs beforehand.

Options:
  --no-build           Skip running the local build before pushing
  --deploy arkserver   After the push, deploy main to the arkserver instance
                       via deploy-arkserver.sh (tests, backup, copy, health)
  --from <sha>         Passed to deploy-arkserver.sh (deployed revision, once)
USAGE
}

run_build=true
deploy_target=""
from_rev=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build)
      run_build=false
      shift
      ;;
    --deploy)
      deploy_target="${2:-}"
      [[ "$deploy_target" == "arkserver" ]] || { echo "Error: --deploy supports only 'arkserver'." >&2; exit 1; }
      shift 2
      ;;
    --from)
      from_rev="${2:?--from needs a revision}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

cd "$REPO_ROOT"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Error: working tree has uncommitted changes. Please commit or stash them first." >&2
  exit 1
fi

printf '\n==> Syncing %s branch\n' "$DEV_BRANCH"
"$CHECKOUT_SCRIPT" "$DEV_BRANCH"

git push origin "$DEV_BRANCH"

if [[ "$run_build" == true ]]; then
  printf '\n==> Running local production build\n'
  "$BUILD_SCRIPT"
fi

printf '\n==> Syncing %s branch\n' "$MAIN_BRANCH"
"$CHECKOUT_SCRIPT" "$MAIN_BRANCH"

printf '\n==> Fast-forwarding %s from %s\n' "$MAIN_BRANCH" "$DEV_BRANCH"
if ! git merge --ff-only "$DEV_BRANCH"; then
  echo "Error: $MAIN_BRANCH cannot fast-forward from $DEV_BRANCH. Resolve manually (likely $MAIN_BRANCH is ahead)." >&2
  exit 1
fi

git push origin "$MAIN_BRANCH"

printf '\n✅ Release pushed to %s. Nothing is deployed by the push itself.\n' "$MAIN_BRANCH"

if [[ "$deploy_target" == "arkserver" ]]; then
  printf '\n==> Deploying %s to arkserver\n' "$MAIN_BRANCH"
  "$SCRIPT_DIR/deploy-arkserver.sh" ${from_rev:+--from "$from_rev"}
fi

printf '\n==> Switching back to %s\n' "$DEV_BRANCH"
"$CHECKOUT_SCRIPT" "$DEV_BRANCH"

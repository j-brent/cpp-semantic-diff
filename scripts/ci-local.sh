#!/usr/bin/env bash
set -euo pipefail

#######################################
# Run CI on this machine, in the image CI runs in, against the working tree as it is now.
#
# Precondition:  docker reachable (on WSL, Docker Desktop's WSL integration enabled); git.
# Arguments:     step names passed to scripts/ci.sh; none runs every step.
# Side effects:  builds the image `cppsd-ci:local` (cached after the first build). The
#                working tree is copied into a throwaway container; nothing is written back.
# Postcondition: scripts/ci.sh passed in that container.
# Returns:       0 passed; 2 a precondition failed; otherwise scripts/ci.sh's exit code.
#######################################

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR/.." || exit 2

command -v git >/dev/null || { echo "precondition: git required" >&2; exit 2; }
command -v docker >/dev/null || { echo "precondition: docker required" >&2; exit 2; }
docker info >/dev/null 2>&1 || {
  echo "precondition: docker is installed but no daemon is reachable." >&2
  echo "On WSL: Docker Desktop > Settings > Resources > WSL integration > enable this distro." >&2
  exit 2
}

# The two pins, read from where they are written, so this script cannot disagree with CI.
base=$(sed -n 's/^ *image: *\(ubuntu:[^ ]*@sha256:[0-9a-f]\{64\}\) *$/\1/p' .github/workflows/ci.yml)
uv_version=$(sed -n 's/^required-version = "==\([0-9.]*\)"$/\1/p' pyproject.toml)
[[ -n "$base" ]] || { echo "precondition: no digest-pinned ubuntu image in ci.yml" >&2; exit 2; }
[[ -n "$uv_version" ]] || { echo "precondition: no exact uv required-version in pyproject.toml" >&2; exit 2; }

image=cppsd-ci:local
docker build --quiet \
  --build-arg "BASE=$base" \
  --build-arg "UV_VERSION=$uv_version" \
  --tag "$image" \
  --file scripts/ci.Dockerfile scripts >/dev/null

# What a checkout of the current state would contain: tracked files and new ones git does
# not ignore. A tracked file deleted in the working tree is skipped rather than failing tar.
# No .venv, caches or build output travel in, so nothing on this machine leaks into the run.
git ls-files -z --cached --others --exclude-standard \
  | while IFS= read -r -d '' path; do
      if [[ -e "$path" ]]; then printf '%s\0' "$path"; fi
    done \
  | tar --null --files-from=- --create --file=- \
  | docker run --rm --interactive \
      --env CI=true --env GITHUB_ACTIONS=true \
      "$image" \
      bash -c 'tar --extract --no-same-owner --file=- && exec scripts/ci.sh "$@"' ci "$@"

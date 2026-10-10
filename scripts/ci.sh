#!/usr/bin/env bash
set -euo pipefail

#######################################
# Run the project's checks: the one definition of CI, which `.github/workflows/ci.yml`
# calls step by step and `scripts/ci-local.sh` runs whole inside the same pinned image.
#
# Precondition:  uv on PATH; system packages from scripts/ci-deps.sh installed.
# Arguments:     step names to run, in order; none runs every step. Steps:
#                sync format lint mypy pyright test doctor guard
# Side effects:  `sync` creates or updates .venv. `test` writes pytest's cache. Nothing
#                outside the repository except uv's own cache and Python downloads.
# Postcondition: every requested step passed.
# Returns:       0 all passed; 2 bad usage or a precondition failed; otherwise the exit
#                code of the first step that failed.
#######################################

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR/.." || exit 2

ALL_STEPS=(sync format lint mypy pyright test doctor guard)

# Log readability only. Test results do not depend on these: tests/conftest.py removes them
# and every other ambient setting before a test runs.
export NO_COLOR=1 COLUMNS=160

step_sync() {
  # --locked: a lockfile that disagrees with pyproject.toml is a failure, not an update.
  uv sync --locked --all-groups
  uv --version
  uv run python --version
}

step_format() { uv run ruff format --check .; }
step_lint() { uv run ruff check .; }
step_mypy() { uv run mypy; }
step_pyright() { uv run pyright; }
step_test() { uv run pytest -q; }

# Library discovery is the code most likely to be tuned to one host by accident; this is
# where it meets a host that is not a developer's.
step_doctor() { uv run cppsd doctor; }

# A libclang without builtin headers returns a truncated AST and raises nothing, so prove the
# guard against it fires rather than trusting that it would. Aimed at the PyPI wheel's own
# library, which resolves its resource directory relative to site-packages and so lacks the
# headers whatever the host has installed.
step_guard() {
  local wheel
  wheel=$(uv run python -c \
    "import clang, pathlib; print(pathlib.Path(clang.__file__).parent / 'native' / 'libclang.so')")
  if [[ ! -f "$wheel" ]]; then
    echo "no bundled libclang at $wheel; nothing to guard against"
    return 0
  fi
  local status=0
  CPPSD_LIBCLANG="$wheel" uv run cppsd doctor || status=$?
  if [[ $status -eq 0 ]]; then
    echo "doctor accepted a libclang that cannot parse stddef.h: the silent-truncation" >&2
    echo "failure mode. Models would be built from partial ASTs, agree, and be wrong." >&2
    return 1
  fi
  # 5 is the toolchain refusal (ADR-033). Any other failure is the guard breaking for an
  # unrelated reason, which must not pass for the guard working.
  if [[ $status -ne 5 ]]; then
    echo "doctor failed with exit $status, expected 5 (no trustworthy parser)" >&2
    return 1
  fi
  echo "doctor correctly refused the truncating toolchain."
}

command -v uv >/dev/null || { echo "precondition: uv required (https://docs.astral.sh/uv/)" >&2; exit 2; }

steps=("$@")
[[ ${#steps[@]} -gt 0 ]] || steps=("${ALL_STEPS[@]}")
for step in "${steps[@]}"; do
  declare -F "step_$step" >/dev/null \
    || { echo "unknown step: $step (expected: ${ALL_STEPS[*]})" >&2; exit 2; }
done

for step in "${steps[@]}"; do
  echo "==> $step" >&2
  "step_$step"
done

#!/usr/bin/env bash
set -euo pipefail

#######################################
# Install the system packages CI needs: the one list, used by the CI workflow, by the
# local CI image (scripts/Dockerfile) and by the demo workflow.
#
# Precondition:  Debian/Ubuntu with apt-get; root, or sudo available.
# Arguments:     none.
# Side effects:  apt-get update and install. Idempotent.
# Postcondition: git, cmake, a C++ compiler, Ninja and LLVM 18 -- with the builtin
#                headers libclang needs (libclang-common-18-dev) -- are installed.
# Returns:       0 ok; 2 a precondition failed; 3 the postcondition failed; otherwise
#                apt-get's failure.
#######################################

# libclang-common-18-dev is the load-bearing one: without the builtin headers it carries,
# libclang returns truncated ASTs and raises nothing. The rest are installed explicitly
# rather than trusted to be on the image: GitHub's runner image happens to carry cmake and
# a compiler, a plain Ubuntu image does not, and the end-to-end tests skip without them.
packages=(
  ca-certificates
  git
  cmake
  ninja-build
  g++
  libclang1-18
  libclang-common-18-dev
  llvm-18
)

command -v apt-get >/dev/null || { echo "precondition: apt-get required" >&2; exit 2; }

as_root=()
if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null || { echo "precondition: run as root or install sudo" >&2; exit 2; }
  as_root=(sudo)
fi

"${as_root[@]}" apt-get update -qq
"${as_root[@]}" env DEBIAN_FRONTEND=noninteractive \
  apt-get install -y -qq --no-install-recommends "${packages[@]}"

# Postcondition: the builtin headers exist, which is the one failure that would otherwise
# surface only as a truncated parse.
compgen -G "/usr/lib/llvm-18/lib/clang/*/include/stddef.h" >/dev/null \
  || { echo "postcondition: libclang 18 builtin headers (stddef.h) missing" >&2; exit 3; }

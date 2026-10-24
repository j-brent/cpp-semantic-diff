# The image CI's job runs in, rebuilt locally by scripts/ci-local.sh.
#
# The ubuntu digest is written here, as a literal, because this is where Dependabot can see
# and update it; it cannot update a workflow's `container:` image. ci.yml repeats it, and
# `scripts/ci.sh pins` fails CI if the two disagree, so a bump of one alone cannot pass.
# UV_VERSION is passed in from `required-version` in pyproject.toml, where it is pinned.
ARG UV_VERSION

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac
COPY --from=uv /uv /uvx /usr/local/bin/
COPY ci-deps.sh /tmp/ci-deps.sh
RUN /tmp/ci-deps.sh && rm -rf /var/lib/apt/lists/* /tmp/ci-deps.sh
# The source is copied in, not bind-mounted, so uv cannot hard-link into its cache across
# filesystems; copy mode avoids the warning.
ENV UV_LINK_MODE=copy
WORKDIR /work

# The image CI's job runs in, rebuilt locally by scripts/ci-local.sh.
#
# BASE and UV_VERSION are passed in from where they are pinned -- the `container:` image in
# .github/workflows/ci.yml and `required-version` in pyproject.toml -- so each is written in
# one place and this file cannot drift from CI.
ARG BASE
ARG UV_VERSION

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM ${BASE}
COPY --from=uv /uv /uvx /usr/local/bin/
COPY ci-deps.sh /tmp/ci-deps.sh
RUN /tmp/ci-deps.sh && rm -rf /var/lib/apt/lists/* /tmp/ci-deps.sh
# The source is copied in, not bind-mounted, so hard links into uv's cache would cross
# nothing; copy mode avoids a warning when they cannot be made.
ENV UV_LINK_MODE=copy
WORKDIR /work

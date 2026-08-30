# syntax=docker/dockerfile:1
ARG DEBIAN_TAG=trixie-slim
ARG PYTHON_VERSION=3.12.7
ARG NODE_MAJOR=24

FROM debian:${DEBIAN_TAG} AS python-builder

ARG PYTHON_VERSION

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        libbz2-dev \
        libexpat1-dev \
        libffi-dev \
        libgdbm-compat-dev \
        libgdbm-dev \
        liblzma-dev \
        libmpdec-dev \
        libncurses-dev \
        libreadline-dev \
        libsqlite3-dev \
        libssl-dev \
        tar \
        tk-dev \
        uuid-dev \
        xz-utils \
        zlib1g-dev; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    curl -fsSL "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tgz" -o /tmp/python.tgz; \
    tar -xzf /tmp/python.tgz -C /tmp; \
    cd /tmp/Python-${PYTHON_VERSION}; \
    ./configure --prefix=/opt/python --enable-optimizations --with-ensurepip=install; \
    make -j"$(nproc)"; \
    make install; \
    rm -rf /tmp/python.tgz /tmp/Python-${PYTHON_VERSION}

FROM debian:${DEBIAN_TAG} AS runtime-base

ARG PYTHON_VERSION
ARG NODE_MAJOR
ARG PLAYWRIGHT_VERSION=1.60.0

ENV PATH="/opt/python/bin:${PATH}"
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY --from=python-builder /opt/python /opt/python

RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        ccache \
        clang \
        clang-format \
        cmake \
        curl \
        gdb \
        git \
        gnupg \
        gosu \
        less \
        libclang-rt-dev \
        libcurl4-openssl-dev \
        libssl-dev \
        lldb \
        ninja-build \
        openssh-client \
        pkg-config \
        ripgrep \
        tar \
        tini \
        tzdata; \
    curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    npx -y playwright@${PLAYWRIGHT_VERSION} install --with-deps chromium; \
    PYTHON_MINOR="${PYTHON_VERSION%.*}"; \
    ln -sf /opt/python/bin/python${PYTHON_MINOR} /usr/local/bin/python3; \
    ln -sf /opt/python/bin/python${PYTHON_MINOR} /usr/local/bin/python; \
    ln -sf /opt/python/bin/pip${PYTHON_MINOR} /usr/local/bin/pip3; \
    ln -sf /opt/python/bin/pip${PYTHON_MINOR} /usr/local/bin/pip; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Built from the repository root (docker build -f anvil/Dockerfile <repo>) so
# the shared swarmforge package can be copied in. Everything the entrypoint
# runs on the python side lives in that package and is invoked with `python3
# -m`, so /usr/local/lib/swarmforge is the import root and the package lands
# one level below it.
COPY anvil/entrypoint.sh /usr/local/bin/anvil-entrypoint.sh
COPY swarmforge/ /usr/local/lib/swarmforge/swarmforge/
RUN chmod +x /usr/local/bin/anvil-entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/anvil-entrypoint.sh"]

FROM runtime-base AS agent-runtime

ARG AGENT=opencode
ARG OPENCODE_VERSION=
ARG SWARMFORGE_HARNESS_INSTALL_BUST=0
# The package is already in the image from the runtime-base stage, and each
# harness ships the script that installs its binary inside its own package
# directory. An AGENT with no such script is not one this image can build, so
# the missing file fails the build rather than producing a binary-less image.
RUN set -eux; \
    install_sh="/usr/local/lib/swarmforge/swarmforge/harness/${AGENT}/install.sh"; \
    if [ ! -f "${install_sh}" ]; then \
      printf '%s\n' "Unsupported AGENT: ${AGENT}" >&2; \
      exit 1; \
    fi; \
    sh "${install_sh}"

ENV SWARMFORGE_AGENT_BIN=${AGENT}

FROM agent-runtime AS opencode-runtime

FROM agent-runtime AS claude-runtime

# The status line and the settings that turn it on ship only in this image:
# no other harness reads Claude's settings.json. The defaults are the lowest
# config layer, and they are baked in here rather than shipped from the
# checkout because they name a path only the image has.
COPY anvil/statusline.sh /usr/local/bin/swarmforge-statusline
COPY anvil/claude-settings.json /usr/local/share/swarmforge/claude-settings.json
RUN chmod +x /usr/local/bin/swarmforge-statusline

FROM agent-runtime AS grok-runtime

FROM agent-runtime AS codex-runtime

FROM opencode-runtime AS default

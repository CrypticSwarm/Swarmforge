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

COPY entrypoint.sh /usr/local/bin/opencode-entrypoint.sh
COPY merge_opencode_json.py /usr/local/lib/swarmforge/merge_opencode_json.py
COPY translate_agents.py /usr/local/lib/swarmforge/translate_agents.py
RUN chmod +x /usr/local/bin/opencode-entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/opencode-entrypoint.sh"]

FROM runtime-base AS agent-runtime

ARG AGENT=opencode
ARG OPENCODE_INSTALL_BUST=0
ARG OPENCODE_VERSION=
ARG CLAUDE_INSTALL_BUST=0
RUN set -eux; \
  case "${AGENT}" in \
    opencode) \
      echo "Installing OpenCode (cache bust: ${OPENCODE_INSTALL_BUST})"; \
      if [ -n "${OPENCODE_VERSION}" ]; then \
        curl -fsSL https://opencode.ai/install | bash -s -- --version "${OPENCODE_VERSION}"; \
      else \
        curl -fsSL https://opencode.ai/install | bash; \
      fi; \
      install -m 0755 /root/.opencode/bin/opencode /usr/local/bin/opencode; \
      ;; \
    claude) \
      echo "Installing Claude Code (cache bust: ${CLAUDE_INSTALL_BUST})"; \
      curl -fsSL https://claude.ai/install.sh | bash; \
      claude_bin="$(readlink -f /root/.local/bin/claude)"; \
      install -m 0755 "${claude_bin}" /usr/local/bin/claude; \
      rm -rf /root/.claude /root/.claude.json /root/.local/share/claude; \
      rm -f /root/.local/bin/claude; \
      ;; \
    *) \
      printf '%s\n' "Unsupported AGENT: ${AGENT}" >&2; \
      exit 1; \
      ;; \
  esac

ENV SWARMFORGE_AGENT_BIN=${AGENT}

FROM agent-runtime AS opencode-runtime

FROM agent-runtime AS claude-runtime

FROM opencode-runtime AS default

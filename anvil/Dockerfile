# syntax=docker/dockerfile:1
ARG DEBIAN_TAG=trixie-slim
ARG PYTHON_VERSION=3.12.7
ARG RUBY_VERSION=3.2.11
ARG RUBY_SHA256=b3eeabd6636f334531db3ffdc3229eb05e524740e6c84fdc043720573cf2f8b2
ARG BUNDLER_VERSION=2.6.9
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

FROM debian:${DEBIAN_TAG} AS ruby-builder

ARG RUBY_VERSION
ARG RUBY_SHA256
ARG BUNDLER_VERSION

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        libffi-dev \
        libgmp-dev \
        libssl-dev \
        libyaml-dev \
        tar \
        zlib1g-dev; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    curl -fsSL "https://cache.ruby-lang.org/pub/ruby/${RUBY_VERSION%.*}/ruby-${RUBY_VERSION}.tar.gz" -o /tmp/ruby.tgz; \
    printf '%s  %s\n' "${RUBY_SHA256}" /tmp/ruby.tgz | sha256sum -c -; \
    tar -xzf /tmp/ruby.tgz -C /tmp; \
    cd /tmp/ruby-${RUBY_VERSION}; \
    ./configure --prefix=/opt/ruby --enable-shared --disable-install-doc; \
    make -j"$(nproc)"; \
    make install; \
    /opt/ruby/bin/gem install --no-document bundler -v "${BUNDLER_VERSION}"; \
    rm -rf /tmp/ruby.tgz /tmp/ruby-${RUBY_VERSION}

FROM debian:${DEBIAN_TAG} AS runtime-base

ARG PYTHON_VERSION
ARG NODE_MAJOR
ARG PLAYWRIGHT_VERSION=1.60.0

# /opt/ruby is root's, so gems go under the anvil home; its bin trails PATH so it shadows nothing root runs.
ENV GEM_HOME=/home/anvil/.gem
ENV PATH="/opt/python/bin:/opt/ruby/bin:${PATH}:${GEM_HOME}/bin"
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# No session is there to answer corepack's download prompt, and its auto-pin would edit package.json.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 COREPACK_ENABLE_AUTO_PIN=0

COPY --from=python-builder /opt/python /opt/python
COPY --from=ruby-builder /opt/ruby /opt/ruby

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
        default-libmysqlclient-dev \
        gdb \
        git \
        gnupg \
        gosu \
        less \
        libclang-rt-dev \
        libcurl4-openssl-dev \
        libffi-dev \
        libgmp-dev \
        libssl-dev \
        libyaml-dev \
        lldb \
        ninja-build \
        openssh-client \
        pkg-config \
        ripgrep \
        tar \
        tini \
        tzdata \
        zlib1g-dev; \
    curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    corepack enable yarn; \
    npx -y playwright@${PLAYWRIGHT_VERSION} install --with-deps chromium; \
    PYTHON_MINOR="${PYTHON_VERSION%.*}"; \
    ln -sf /opt/python/bin/python${PYTHON_MINOR} /usr/local/bin/python3; \
    ln -sf /opt/python/bin/python${PYTHON_MINOR} /usr/local/bin/python; \
    ln -sf /opt/python/bin/pip${PYTHON_MINOR} /usr/local/bin/pip3; \
    ln -sf /opt/python/bin/pip${PYTHON_MINOR} /usr/local/bin/pip; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Built from the repo root (-f anvil/Dockerfile) so swarmforge/ is in the context.
COPY anvil/entrypoint.sh /usr/local/bin/anvil-entrypoint.sh
COPY swarmforge/ /usr/local/lib/swarmforge/swarmforge/
RUN chmod +x /usr/local/bin/anvil-entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/anvil-entrypoint.sh"]

FROM runtime-base AS agent-runtime

ARG AGENT=opencode
# Empty means the installer's latest; each harness's install.sh decides what a pin means.
ARG SWARMFORGE_HARNESS_VERSION=
ARG SWARMFORGE_HARNESS_INSTALL_BUST=0
# AGENT reaches a path here, so it is held to a bare name first.
RUN set -eux; \
    case "${AGENT}" in \
      ''|*[!a-z0-9_-]*) \
        printf '%s\n' "AGENT is not a harness name: ${AGENT}" >&2; \
        exit 1 ;; \
    esac; \
    install_sh="/usr/local/lib/swarmforge/swarmforge/harness/${AGENT}/install.sh"; \
    if [ ! -f "${install_sh}" ]; then \
      printf '%s\n' "Unsupported AGENT: ${AGENT}" >&2; \
      exit 1; \
    fi; \
    sh "${install_sh}"

ENV SWARMFORGE_AGENT_BIN=${AGENT}

FROM agent-runtime AS harness-runtime

ARG AGENT=opencode
# The harness's optional image.sh; AGENT reaches a path, so it is held to a bare name first.
RUN set -eux; \
    case "${AGENT}" in \
      ''|*[!a-z0-9_-]*) \
        printf '%s\n' "AGENT is not a harness name: ${AGENT}" >&2; \
        exit 1 ;; \
    esac; \
    image_sh="/usr/local/lib/swarmforge/swarmforge/harness/${AGENT}/image.sh"; \
    if [ -f "${image_sh}" ]; then sh "${image_sh}"; fi

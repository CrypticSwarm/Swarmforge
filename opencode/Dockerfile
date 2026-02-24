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

FROM debian:${DEBIAN_TAG}

ARG PYTHON_VERSION
ARG NODE_MAJOR
ARG BEADS_VERSION=v0.49.4

ENV PATH="/opt/python/bin:${PATH}"

COPY --from=python-builder /opt/python /opt/python

RUN set -eux; \
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
        tini; \
    curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    beads_arch="$(dpkg --print-architecture)"; \
    beads_archive="beads_${BEADS_VERSION#v}_linux_${beads_arch}.tar.gz"; \
    curl -fsSL "https://github.com/steveyegge/beads/releases/download/${BEADS_VERSION}/${beads_archive}" -o /tmp/beads.tar.gz; \
    curl -fsSL "https://github.com/steveyegge/beads/releases/download/${BEADS_VERSION}/checksums.txt" -o /tmp/beads-checksums.txt; \
    beads_sha256="$(grep "${beads_archive}" /tmp/beads-checksums.txt | awk '{print $1}')"; \
    echo "${beads_sha256}  /tmp/beads.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/beads.tar.gz -C /usr/local/bin bd; \
    ln -sf bd /usr/local/bin/beads; \
    rm -f /tmp/beads.tar.gz /tmp/beads-checksums.txt; \
    PYTHON_MINOR="${PYTHON_VERSION%.*}"; \
    ln -sf /opt/python/bin/python${PYTHON_MINOR} /usr/local/bin/python3; \
    ln -sf /opt/python/bin/python${PYTHON_MINOR} /usr/local/bin/python; \
    ln -sf /opt/python/bin/pip${PYTHON_MINOR} /usr/local/bin/pip3; \
    ln -sf /opt/python/bin/pip${PYTHON_MINOR} /usr/local/bin/pip; \
    rm -rf /var/lib/apt/lists/*

# Install OpenCode
#
# OpenCode updates frequently; this build arg lets us selectively bust the
# Docker layer cache for just the install step.
ARG OPENCODE_INSTALL_BUST=0
RUN set -eux; \
  echo "Installing OpenCode (cache bust: ${OPENCODE_INSTALL_BUST})"; \
  curl -fsSL https://opencode.ai/install | bash; \
  install -m 0755 /root/.opencode/bin/opencode /usr/local/bin/opencode

WORKDIR /workspace

COPY entrypoint.sh /usr/local/bin/opencode-entrypoint.sh
RUN chmod +x /usr/local/bin/opencode-entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/opencode-entrypoint.sh"]

# syntax=docker/dockerfile:1
ARG DEBIAN_TAG=trixie-slim
FROM debian:${DEBIAN_TAG}

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    openssh-client \
    ripgrep \
    less \
    tini \
    gosu \
  && rm -rf /var/lib/apt/lists/*

# Install OpenCode
RUN curl -fsSL https://opencode.ai/install | bash \
  && install -m 0755 /root/.opencode/bin/opencode /usr/local/bin/opencode

WORKDIR /workspace

COPY entrypoint.sh /usr/local/bin/opencode-entrypoint.sh
RUN chmod +x /usr/local/bin/opencode-entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/opencode-entrypoint.sh"]

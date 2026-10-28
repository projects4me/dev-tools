# Talos container runner image (talos/lib/container.py): the Claude CLI, node, python, git, gh and the docker CLI.
# No credentials are baked in: Talos passes them to each `docker run` (see the Talos README, "Container runs").
#   docker build -t p4m-talos-runner dev-tools/talos/container
FROM node:22-bookworm-slim

ARG GH_VERSION=2.63.2
ARG CLAUDE_CODE_VERSION=latest

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 git openssh-client ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && arch="$(dpkg --print-architecture)" \
 && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" \
    | tar -xz -C /tmp \
 && mv "/tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh \
 && rm -rf /tmp/gh_* \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force

COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY entrypoint.py /opt/talos/entrypoint.py

WORKDIR /work
ENTRYPOINT ["python3", "/opt/talos/entrypoint.py"]

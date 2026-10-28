#!/bin/sh
# The container's `docker`: the real CLI when the run has Docker access, else a clear message instead of
# "Cannot connect to the Docker daemon" or "permission denied".
if [ -z "$DOCKER_HOST" ]; then
  if [ ! -S /var/run/docker.sock ]; then
    echo "talos-container: this run has no Docker access. Tests that need Docker (p4m test, ai test) can't run here;" >&2
    echo "talos-container: set \"dockerSocket\": true or \"testDockerHost\" in the agent's runner (agent.json)." >&2
    exit 1
  fi
  if [ ! -r /var/run/docker.sock ] || [ ! -w /var/run/docker.sock ]; then
    echo "talos-container: the docker socket is mounted but uid $(id -u) can't use it; set \"dockerSocketGroup\" in the" >&2
    echo "talos-container: agent's runner (agent.json) to the socket's group id on the Docker host." >&2
    exit 1
  fi
fi
exec /usr/local/libexec/talos/docker "$@"

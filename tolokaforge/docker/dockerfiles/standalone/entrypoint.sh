#!/usr/bin/env bash
# Entrypoint for the all-in-one tolokaforge image.
#
# Modes (first argument):
#   appliance (default)  Start all services under supervisord and stay up.
#                        Drive trials from the host or `docker exec` against
#                        localhost:50051.
#   batch [args…]        Bring services up, wait for the runner, then run
#                        `tolokaforge run args…` in-container and exit with its
#                        status. The mounted run config MUST set
#                        orchestrator.auto_start_services: false (there is no
#                        env override for it); EXECUTOR_ADDRESS is exported here.
#   <anything else>      Executed verbatim (e.g. `docker run … tolokaforge --help`).
set -euo pipefail

RUNNER_ADDRESS="localhost:${RUNNER_PORT:-50051}"
SUPERVISOR_CONF=/etc/supervisor/standalone.conf

# Resolve the service hostnames the builtin http/browser tools address by name
# (mock-web:8080, rag-service:8001, json-db:8000) to loopback, so tasks work
# inside the single container exactly as on the networked stack. Needs root to
# write /etc/hosts; the service programs still run as the non-root `runner`.
add_host_aliases() {
  local marker="# tolokaforge-standalone"
  if [ "$(id -u)" = "0" ] && ! grep -q "${marker}" /etc/hosts 2>/dev/null; then
    printf '127.0.0.1 json-db mock-web rag-service runner %s\n' "${marker}" >> /etc/hosts
  fi
}

wait_for_runner() {
  local timeout="${1:-120}"
  /opt/venv/bin/python - "$RUNNER_ADDRESS" "$timeout" <<'PY'
import sys, time, grpc
address, timeout = sys.argv[1], float(sys.argv[2])
deadline = time.time() + timeout
while time.time() < deadline:
    try:
        grpc.channel_ready_future(grpc.insecure_channel(address)).result(timeout=3)
        sys.exit(0)
    except Exception:
        time.sleep(1)
sys.stderr.write(f"runner at {address} not ready within {timeout:.0f}s\n")
sys.exit(1)
PY
}

add_host_aliases

mode="${1:-appliance}"
case "$mode" in
  appliance|services)
    exec supervisord -c "$SUPERVISOR_CONF"
    ;;
  batch)
    shift
    supervisord -c "$SUPERVISOR_CONF" &
    supervisor_pid=$!
    trap 'kill "$supervisor_pid" 2>/dev/null || true' EXIT
    wait_for_runner 180
    # Attach the orchestrator to the already-running runner; no Docker bring-up.
    export EXECUTOR_ADDRESS="$RUNNER_ADDRESS"
    su -p runner -c "EXECUTOR_ADDRESS='$RUNNER_ADDRESS' tolokaforge run $*"
    ;;
  *)
    exec "$@"
    ;;
esac

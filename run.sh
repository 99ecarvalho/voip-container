#!/usr/bin/env bash
# Build, start and stop the SIP test target.
# Usage: ./run.sh [start|build|stop|restart|status|logs|call|devices]
#   (no argument) / start  build images only if missing, then start
#   build                  rebuild images, then start
#   call [ext] [account]   call the server with your headset (client/call.sh)
#   devices                list audio devices for AUDIO_OUT / AUDIO_IN
set -euo pipefail

cd "$(dirname "$0")"

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    echo "Docker Compose not found. Install Docker Compose v2." >&2
    exit 1
fi

# Recordings, uploads and captures (mounted into both containers).
export SIP_TEST_DIR="${SIP_TEST_DIR:-/tmp/sip-test}"
mkdir -p "$SIP_TEST_DIR"

host_ip() {
    hostname -I 2>/dev/null | awk '{print $1}'
}

print_access() {
    local ip
    ip="$(host_ip)"
    ip="${ip:-<host-ip>}"
    cat <<EOF

Web UI:     http://${ip}:8087
SIP server: ${ip}:5060 (UDP/TCP)
Accounts:   1001, 1001-inband, 1001-info, 1001-auto, 1001-opus  (password: test)
Test calls: sip:echo@${ip}  sip:milliwatt@${ip}  sip:dtmf-test@${ip}  (see README.md)
Files:      ${SIP_TEST_DIR}  (recordings, uploads, captures)

Headset call: ./run.sh call [extension] [account]   (default: echo 1001)
Asterisk CLI: docker exec -it barebone-asterisk asterisk -rvvv
Stop:         ./run.sh stop
EOF
}

case "${1:-start}" in
    start)
        "${COMPOSE[@]}" up -d
        print_access
        ;;
    build)
        "${COMPOSE[@]}" up --build -d
        print_access
        ;;
    stop)
        "${COMPOSE[@]}" down
        ;;
    restart)
        "${COMPOSE[@]}" down
        "${COMPOSE[@]}" up -d
        print_access
        ;;
    status)
        "${COMPOSE[@]}" ps
        print_access
        ;;
    logs)
        "${COMPOSE[@]}" logs -f
        ;;
    call)
        shift
        exec ./client/call.sh "$@"
        ;;
    devices)
        exec ./client/call.sh --devices
        ;;
    *)
        echo "Usage: $0 [start|build|stop|restart|status|logs|call|devices]" >&2
        exit 1
        ;;
esac

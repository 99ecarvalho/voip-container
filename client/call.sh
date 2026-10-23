#!/usr/bin/env bash
# Call the test server from this computer with your headset (baresip in Docker).
# Usage: client/call.sh [extension] [account]      e.g. client/call.sh milliwatt
#        client/call.sh --devices                  list audio devices
#
# Environment:
#   SERVER     SIP server IP           (default: this host's first IP)
#   AUDIO_OUT  PulseAudio sink name    (default: system default output)
#   AUDIO_IN   PulseAudio source name  (default: system default input)
#   AUDIO_NULL=1                       no audio device (smoke tests)
set -euo pipefail

cd "$(dirname "$0")"
IMAGE="sip-test-client"

if [[ "${1:-}" == "--devices" ]]; then
    echo "Outputs (AUDIO_OUT):"; pactl list short sinks   | awk '{print "  " $2}'
    echo "Inputs  (AUDIO_IN):";  pactl list short sources | awk '$2 !~ /\.monitor$/ {print "  " $2}'
    exit 0
fi

EXT="${1:-echo}"
ACCOUNT="${2:-1001}"
SERVER="${SERVER:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
SERVER="${SERVER:-127.0.0.1}"

PULSE_SOCK="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/pulse/native"
if [[ "${AUDIO_NULL:-0}" != "1" && ! -S "$PULSE_SOCK" ]]; then
    echo "PulseAudio/PipeWire socket not found at $PULSE_SOCK" >&2
    exit 1
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Building $IMAGE image..."
    docker build -t "$IMAGE" .
fi

CFG="$(mktemp -d)"
trap 'rm -rf "$CFG"' EXIT
if [[ "${AUDIO_NULL:-0}" == "1" ]]; then
    sed 's/alsa,default/alsa,null/' config > "$CFG/config"
else
    cp config "$CFG/config"
fi
echo "<sip:${ACCOUNT}@${SERVER}>;auth_pass=test;regint=600" > "$CFG/accounts"

cat <<EOF
Calling sip:${EXT}@${SERVER} as ${ACCOUNT}
  0-9 * # A-D  send DTMF      b  hang up      d  dial another number
  q            quit
EOF

ARGS=(--rm -i --network host --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$CFG:/cfg")
[[ -t 0 ]] && ARGS+=(-t)
if [[ -S "$PULSE_SOCK" ]]; then
    ARGS+=(-e PULSE_SERVER="unix:/run/pulse/native" -v "$PULSE_SOCK:/run/pulse/native")
fi
[[ -n "${AUDIO_OUT:-}" ]] && ARGS+=(-e PULSE_SINK="$AUDIO_OUT")
[[ -n "${AUDIO_IN:-}" ]] && ARGS+=(-e PULSE_SOURCE="$AUDIO_IN")

docker run "${ARGS[@]}" "$IMAGE" -f /cfg -e "/dial sip:${EXT}@${SERVER}"

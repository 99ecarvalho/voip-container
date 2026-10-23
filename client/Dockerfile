# baresip softphone that plays/records through the host's PulseAudio or
# PipeWire (pipewire-pulse) socket. Debian's baresip has no pulse module, so
# audio goes baresip -> ALSA "default" -> ALSA pulse plugin -> host socket.
FROM debian:trixie-slim

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        baresip-core \
        libasound2-plugins \
 && rm -rf /var/lib/apt/lists/*

RUN printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' > /etc/asound.conf

ENTRYPOINT ["baresip"]

# Minimal Asterisk echo/tone test server.
# NOTE: Debian bookworm (12) dropped the asterisk package and bullseye (11) is
# EOL. Ubuntu 24.04 (universe) ships Asterisk 20 (chan_pjsip +
# Echo()/Milliwatt()/Playtones), which is all we need.
FROM ubuntu:24.04

# BuildKit cache mount keeps downloaded packages on the host between builds so
# apt never re-downloads on a slow disk. The apt lists are kept in the cache too
# (no rm -rf) — they stay out of the image layer but persist for the next build.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        asterisk \
        asterisk-modules \
        sox \
        python3

# Drop in our minimal config (overrides the package defaults we care about).
COPY rtp.conf         /etc/asterisk/rtp.conf
COPY http.conf        /etc/asterisk/http.conf
COPY ari.conf         /etc/asterisk/ari.conf
COPY manager.conf     /etc/asterisk/manager.conf

# The files the webui can edit live in /etc/asterisk-custom, which
# docker-compose mounts from ./asterisk (a directory mount, so edits are seen
# even when an editor replaces the file). The copies here are the defaults.
COPY pjsip.conf extensions.conf logger.conf /etc/asterisk-custom/
RUN for f in pjsip.conf extensions.conf logger.conf; do \
        echo "#include /etc/asterisk-custom/$f" > /etc/asterisk/$f; \
    done

# chan_pjsip owns 5060; keep the deprecated chan_sip from trying to bind it.
# Also skip LDAP realtime (unconfigured) and blank the package's broken
# geolocation.conf; both only add errors to the log.
RUN sed -i '/^\[modules\]/a noload => chan_sip.so\nnoload => res_config_ldap.so' /etc/asterisk/modules.conf \
 && : > /etc/asterisk/geolocation.conf

# AGI scripts for protocol simulation (TG / P100).
COPY agi/ /usr/share/asterisk/agi-bin/
RUN chmod +x /usr/share/asterisk/agi-bin/*.py

# Run in the foreground with console logging.
CMD ["asterisk", "-f", "-vvv"]

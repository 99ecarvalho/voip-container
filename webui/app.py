"""
Barebone Asterisk WebUI — SIP/Audio Test Bench
-----------------------------------------------
Flask application bridging the browser to Asterisk via AMI (TCP) and ARI (HTTP).

Provides: call control (ring device, hangup, hold, transfer), DTMF with mode
selector (rfc4733/inband), WAV upload + playback into calls, MixMonitor
recording, sox analysis, tcpdump capture, config editing, and live log streaming.

Environment variables (set by docker-compose):
  ASTERISK_AMI_HOST      default: 127.0.0.1
  ASTERISK_AMI_PORT      default: 5038
  ASTERISK_AMI_USER      default: webui
  ASTERISK_AMI_SECRET    default: webui_secret
  ASTERISK_ARI_URL       default: http://127.0.0.1:8088
  ASTERISK_ARI_USER      default: webui
  ASTERISK_ARI_SECRET    default: webui_secret
"""

import glob
import hashlib as _hashlib
import itertools
import json
import os
import queue
import re
import signal
import socket
import subprocess
import threading
import time

import requests
from flask import (Flask, Response, jsonify, make_response, render_template,
                   request, send_file)

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB upload limit


def _static_version():
    base = os.path.join(os.path.dirname(__file__), "static")
    h = _hashlib.md5()
    for name in ("style.css", "app.js"):
        path = os.path.join(base, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                h.update(f.read())
    return h.hexdigest()[:8]


STATIC_VERSION = _static_version()


@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
AMI_HOST = os.environ.get("ASTERISK_AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.environ.get("ASTERISK_AMI_PORT", "5038"))
AMI_USER = os.environ.get("ASTERISK_AMI_USER", "webui")
AMI_SECRET = os.environ.get("ASTERISK_AMI_SECRET", "webui_secret")

ARI_URL = os.environ.get("ASTERISK_ARI_URL", "http://127.0.0.1:8088")
ARI_USER = os.environ.get("ASTERISK_ARI_USER", "webui")
ARI_SECRET = os.environ.get("ASTERISK_ARI_SECRET", "webui_secret")

# Shared with the Asterisk container (host: /tmp/sip-test by default).
ASTERISK_TMP = "/asterisk-tmp"
ASTERISK_CONF = "/etc/asterisk"
ASTERISK_LOG = "/var/log/asterisk/full"
CAPTURES_DIR = os.path.join(ASTERISK_TMP, "captures")

ALLOWED_CONFS = {"pjsip.conf", "extensions.conf", "logger.conf"}

os.makedirs(CAPTURES_DIR, exist_ok=True)
# Containers run as root; keep the folder writable so the host user can clean it up.
os.chmod(CAPTURES_DIR, 0o777)

# Strict filename pattern for user-supplied names
_SAFE_FILENAME = re.compile(r"^[\w][\w. -]{0,198}\.wav$", re.IGNORECASE)
_SAFE_PCAP = re.compile(r"^[\w][\w. -]{0,198}\.pcap$")


# ---------------------------------------------------------------------------
# AMI client  (persistent, reconnecting)
# ---------------------------------------------------------------------------

class AmiClient:
    RECONNECT_INTERVAL = 5

    def __init__(self, host, port, user, secret):
        self.host = host
        self.port = port
        self.user = user
        self.secret = secret
        self._sock = None
        self._fh = None
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._shutdown = threading.Event()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._action_ids = itertools.count(1)
        # One queue per /logs client so every browser tab sees every event.
        self._subscribers = set()
        self._subscribers_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ami-reader")
        self._thread.start()

    def _connect(self):
        try:
            s = socket.create_connection((self.host, self.port), timeout=5)
            s.settimeout(None)
            fh = s.makefile("r", encoding="utf-8", errors="replace")
            banner = fh.readline()
            if not banner.startswith("Asterisk"):
                s.close()
                return False
            self._sock = s
            self._fh = fh
            resp = self._send_action_internal(
                {"Action": "Login", "Username": self.user, "Secret": self.secret}, fh=fh
            )
            if resp and resp.get("Response") == "Success":
                self._connected.set()
                return True
            s.close()
            self._sock = None
            self._fh = None
            return False
        except Exception:
            return False

    def _run(self):
        while not self._shutdown.is_set():
            if not self._connect():
                time.sleep(self.RECONNECT_INTERVAL)
                continue
            try:
                self._read_loop()
            except Exception:
                pass
            self._connected.clear()
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
            self._sock = None
            self._fh = None
            with self._pending_lock:
                for q in self._pending.values():
                    try:
                        q.put_nowait({"Response": "Error", "Message": "AMI disconnected"})
                    except queue.Full:
                        pass
                self._pending.clear()
            if not self._shutdown.is_set():
                time.sleep(self.RECONNECT_INTERVAL)

    def _read_loop(self):
        current = {}
        for line in self._fh:
            line = line.rstrip("\r\n")
            if line == "":
                if current:
                    self._dispatch(current)
                    current = {}
            else:
                if ": " in line:
                    key, _, value = line.partition(": ")
                    current[key] = value
                elif line.startswith("--END COMMAND--"):
                    if current:
                        self._dispatch(current)
                        current = {}

    def _dispatch(self, packet):
        action_id = packet.get("ActionID")
        if action_id:
            with self._pending_lock:
                waiter = self._pending.get(action_id)
            if waiter:
                try:
                    waiter.put_nowait(packet)
                except queue.Full:
                    pass
                return
        with self._subscribers_lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(packet)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(packet)
                except (queue.Empty, queue.Full):
                    pass

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self._subscribers_lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self._subscribers_lock:
            self._subscribers.discard(q)

    def _send_action_internal(self, fields, fh):
        msg = "".join("{}: {}\r\n".format(k, v) for k, v in fields.items()) + "\r\n"
        self._sock.sendall(msg.encode("utf-8"))
        response = {}
        for line in fh:
            line = line.rstrip("\r\n")
            if line == "":
                if response:
                    return response
                continue
            if ": " in line:
                key, _, value = line.partition(": ")
                response[key] = value
        return response

    def send_action(self, fields, timeout=5):
        if not self._connected.wait(timeout=timeout):
            return None
        action_id = fields.get("ActionID", "aid-{}".format(next(self._action_ids)))
        fields = dict(fields)
        fields["ActionID"] = action_id
        waiter = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[action_id] = waiter
        msg = "".join("{}: {}\r\n".format(k, v) for k, v in fields.items()) + "\r\n"
        try:
            with self._lock:
                self._sock.sendall(msg.encode("utf-8"))
        except Exception:
            with self._pending_lock:
                self._pending.pop(action_id, None)
            return None
        try:
            return waiter.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            with self._pending_lock:
                self._pending.pop(action_id, None)

    def is_connected(self):
        return self._connected.is_set()

    def shutdown(self):
        self._shutdown.set()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass


ami = AmiClient(AMI_HOST, AMI_PORT, AMI_USER, AMI_SECRET)


# ---------------------------------------------------------------------------
# ARI helpers
# ---------------------------------------------------------------------------

def ari_get(path):
    url = "{}/ari{}".format(ARI_URL, path)
    try:
        r = requests.get(url, auth=(ARI_USER, ARI_SECRET), timeout=5)
        r.raise_for_status()
        return r.json(), None
    except requests.exceptions.ConnectionError:
        return None, "ARI not reachable"
    except requests.exceptions.HTTPError as e:
        return None, str(e)
    except Exception as e:
        return None, str(e)


def ari_post(path, data=None):
    url = "{}/ari{}".format(ARI_URL, path)
    try:
        r = requests.post(url, auth=(ARI_USER, ARI_SECRET), json=data or {}, timeout=5)
        r.raise_for_status()
        return (r.json() if r.content else {}), None
    except requests.exceptions.ConnectionError:
        return None, "ARI not reachable"
    except requests.exceptions.HTTPError as e:
        return None, "{} {}".format(r.status_code, r.text[:200])
    except Exception as e:
        return None, str(e)

def _channel_name(channel):
    """The UI sends ARI channel ids (uniqueids); some AMI actions only accept
    channel names, so resolve ids to names."""
    if re.match(r"^\d+\.\d+$", channel):
        data, _ = ari_get("/channels/{}".format(channel))
        if data and data.get("name"):
            return data["name"]
    return channel


# ---------------------------------------------------------------------------
# tcpdump state
# ---------------------------------------------------------------------------
_tcpdump_proc = None
_tcpdump_lock = threading.Lock()
_tcpdump_file = None


# ---------------------------------------------------------------------------
# Routes — pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    resp = make_response(render_template("index.html", static_version=STATIC_VERSION))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


# ---------------------------------------------------------------------------
# Routes — status
# ---------------------------------------------------------------------------

@app.route("/api/status")
def api_status():
    endpoints, ep_err = ari_get("/endpoints")
    channels, ch_err = ari_get("/channels")
    version = "unknown"
    resp = ami.send_action({"Action": "CoreSettings"})
    if resp and resp.get("Response") == "Success":
        version = resp.get("AsteriskVersion", "unknown")
    return jsonify({
        "endpoints": endpoints or [],
        "endpoints_error": ep_err,
        "channels": channels or [],
        "channels_error": ch_err,
        "version": version,
        "ami_connected": ami.is_connected(),
    })


# ---------------------------------------------------------------------------
# Routes — call control
# ---------------------------------------------------------------------------

@app.route("/api/ring", methods=["POST"])
def api_ring():
    """Ring a registered device at a test extension.

    Body: {"account": "1001", "extension": "echo"}
    Creates: PJSIP/1001 → test,echo,1
    """
    body = request.get_json(force=True, silent=True) or {}
    account = body.get("account", "1001")
    extension = body.get("extension", "echo")

    # Validate account (alphanumeric + dash)
    if not re.match(r"^[\w-]{1,30}$", account):
        return jsonify({"ok": False, "error": "invalid account"}), 400
    if not re.match(r"^[\w-]{1,30}$", extension):
        return jsonify({"ok": False, "error": "invalid extension"}), 400

    resp = ami.send_action({
        "Action": "Originate",
        "Channel": "PJSIP/{}".format(account),
        "Context": "test",
        "Exten": extension,
        "Priority": "1",
        "Async": "true",
        "Timeout": "30000",
    })
    if resp is None:
        return jsonify({"ok": False, "error": "AMI not connected or timeout"}), 503
    if resp.get("Response") == "Error":
        return jsonify({"ok": False, "error": resp.get("Message", "AMI error")}), 400
    return jsonify({"ok": True})


@app.route("/api/hangup", methods=["POST"])
def api_hangup():
    body = request.get_json(force=True, silent=True) or {}
    channel_id = body.get("channel", "")
    if not channel_id:
        return jsonify({"ok": False, "error": "channel required"}), 400
    url = "{}/ari/channels/{}".format(ARI_URL, requests.utils.quote(channel_id, safe=""))
    try:
        r = requests.delete(url, auth=(ARI_USER, ARI_SECRET), timeout=5)
        if r.status_code in (200, 204, 404):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "ARI {}".format(r.status_code)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/hold", methods=["POST"])
def api_hold():
    body = request.get_json(force=True, silent=True) or {}
    channel = body.get("channel", "")
    hold = body.get("hold", True)
    if not channel:
        return jsonify({"ok": False, "error": "channel required"}), 400
    channel = _channel_name(channel)
    # AMI has no Hold action; mute the audio both ways instead.
    resp = ami.send_action({"Action": "MuteAudio", "Channel": channel,
                            "Direction": "all", "State": "on" if hold else "off"})
    if resp is None:
        return jsonify({"ok": False, "error": "AMI timeout"}), 503
    if resp.get("Response") == "Error":
        return jsonify({"ok": False, "error": resp.get("Message", "AMI error")}), 400
    return jsonify({"ok": True})


@app.route("/api/transfer", methods=["POST"])
def api_transfer():
    body = request.get_json(force=True, silent=True) or {}
    channel = body.get("channel", "")
    extension = body.get("extension", "")
    if not channel or not extension:
        return jsonify({"ok": False, "error": "channel and extension required"}), 400
    channel = _channel_name(channel)
    resp = ami.send_action({
        "Action": "Redirect", "Channel": channel,
        "Context": "test", "Exten": extension, "Priority": "1",
    })
    if resp is None:
        return jsonify({"ok": False, "error": "AMI timeout"}), 503
    if resp.get("Response") == "Error":
        return jsonify({"ok": False, "error": resp.get("Message", "AMI error")}), 400
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes — DTMF with mode
# ---------------------------------------------------------------------------

@app.route("/api/dtmf", methods=["POST"])
def api_dtmf():
    """Send DTMF to a channel.

    Body: {"channel": "...", "digits": "1234", "duration": 250}

    Asterisk sends the digits using the endpoint's dtmf_mode from pjsip.conf
    (rfc4733 / inband / info / auto). To test another mode, register with the
    matching account (1001, 1001-inband, 1001-info, 1001-auto).
    """
    body = request.get_json(force=True, silent=True) or {}
    channel = body.get("channel", "")
    digits = body.get("digits", "")
    try:
        duration = min(2000, max(50, int(body.get("duration", 250))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid duration"}), 400

    if not channel:
        return jsonify({"ok": False, "error": "channel required"}), 400
    channel = _channel_name(channel)
    if not digits:
        return jsonify({"ok": False, "error": "digits required"}), 400

    valid_digits = [d.upper() for d in digits if d in "0123456789*#ABCDabcd"]
    if not valid_digits:
        return jsonify({"ok": False, "error": "no valid DTMF digits"}), 400

    # AMI PlayDTMF works on any channel (ARI's /dtmf needs a Stasis channel).
    for digit in valid_digits:
        resp = ami.send_action({
            "Action": "PlayDTMF",
            "Channel": channel,
            "Digit": digit,
            "Duration": str(duration),
        })
        if resp is None:
            return jsonify({"ok": False, "error": "AMI timeout"}), 503
        if resp.get("Response") == "Error":
            return jsonify({"ok": False, "error": resp.get("Message", "AMI error")}), 400
        time.sleep(duration / 1000.0 + 0.05)

    return jsonify({"ok": True, "digits": "".join(valid_digits)})


# ---------------------------------------------------------------------------
# Routes — recording (MixMonitor)
# ---------------------------------------------------------------------------

@app.route("/api/record", methods=["POST"])
def api_record():
    body = request.get_json(force=True, silent=True) or {}
    channel = body.get("channel", "")
    if not channel:
        return jsonify({"ok": False, "error": "channel required"}), 400
    channel = _channel_name(channel)
    fname = body.get("filename") or "monitor-{}.wav".format(time.strftime("%Y%m%d-%H%M%S"))
    if not _SAFE_FILENAME.match(fname):
        return jsonify({"ok": False, "error": "invalid filename"}), 400
    filepath = os.path.join(ASTERISK_TMP, fname)
    resp = ami.send_action({"Action": "MixMonitor", "Channel": channel, "File": filepath})
    if resp is None:
        return jsonify({"ok": False, "error": "AMI timeout"}), 503
    if resp.get("Response") == "Error":
        return jsonify({"ok": False, "error": resp.get("Message", "AMI error")}), 400
    return jsonify({"ok": True, "file": fname})


@app.route("/api/stoprecord", methods=["POST"])
def api_stoprecord():
    body = request.get_json(force=True, silent=True) or {}
    channel = body.get("channel", "")
    if not channel:
        return jsonify({"ok": False, "error": "channel required"}), 400
    channel = _channel_name(channel)
    resp = ami.send_action({"Action": "StopMixMonitor", "Channel": channel})
    if resp is None:
        return jsonify({"ok": False, "error": "AMI timeout"}), 503
    if resp.get("Response") == "Error":
        return jsonify({"ok": False, "error": resp.get("Message", "AMI error")}), 400
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes — WAV file upload & playback into channel
# ---------------------------------------------------------------------------

@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Upload a WAV file to /asterisk-tmp/ for playback or analysis."""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no file in request"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "empty filename"}), 400

    # Sanitize filename
    fname = os.path.basename(f.filename).strip()
    if not fname.lower().endswith(".wav"):
        return jsonify({"ok": False, "error": "only .wav files accepted"}), 400
    # Replace any non-safe chars
    safe_name = re.sub(r"[^\w. -]", "_", fname)
    if not _SAFE_FILENAME.match(safe_name):
        return jsonify({"ok": False, "error": "invalid filename"}), 400

    path = os.path.join(ASTERISK_TMP, safe_name)
    f.save(path)
    return jsonify({"ok": True, "filename": safe_name, "size": os.path.getsize(path)})


@app.route("/api/playback", methods=["POST"])
def api_playback():
    """Play a WAV file into an active channel.

    Body: {"channel": "...", "filename": "test.wav"}
    Sets PLAYFILE channel variable and redirects to the playfile extension.
    After playback, the channel continues in Echo().
    """
    body = request.get_json(force=True, silent=True) or {}
    channel = body.get("channel", "")
    filename = body.get("filename", "")
    if not channel:
        return jsonify({"ok": False, "error": "channel required"}), 400
    channel = _channel_name(channel)
    if not filename:
        return jsonify({"ok": False, "error": "filename required"}), 400

    full_path = _validate_wav_filename(filename)
    if not full_path:
        return jsonify({"ok": False, "error": "file not found"}), 404

    # Asterisk Playback() wants the path without extension
    play_path = os.path.splitext(full_path)[0]

    # Set channel variable and redirect
    resp = ami.send_action({
        "Action": "Setvar", "Channel": channel,
        "Variable": "PLAYFILE", "Value": play_path,
    })
    if resp is None:
        return jsonify({"ok": False, "error": "AMI timeout (setvar)"}), 503

    resp = ami.send_action({
        "Action": "Redirect", "Channel": channel,
        "Context": "test", "Exten": "playfile", "Priority": "1",
    })
    if resp is None:
        return jsonify({"ok": False, "error": "AMI timeout (redirect)"}), 503
    if resp.get("Response") == "Error":
        return jsonify({"ok": False, "error": resp.get("Message", "AMI error")}), 400
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes — recordings list, download, analysis
# ---------------------------------------------------------------------------

@app.route("/api/recordings")
def api_recordings():
    """List all .wav files in ASTERISK_TMP."""
    files = []
    for path in sorted(glob.glob(os.path.join(ASTERISK_TMP, "*.wav"))):
        try:
            st = os.stat(path)
            files.append({"name": os.path.basename(path), "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            pass
    return jsonify({"recordings": files})


def _validate_wav_filename(filename):
    """Check filename is a safe .wav name and return the full path or None."""
    if not filename or not _SAFE_FILENAME.match(filename):
        return None
    path = os.path.join(ASTERISK_TMP, filename)
    if not os.path.isfile(path):
        return None
    return path


def _spectrogram_path(wav_path):
    return os.path.splitext(wav_path)[0] + "-spectrogram.png"


@app.route("/api/recordings/<filename>/wav")
def api_recording_wav(filename):
    path = _validate_wav_filename(filename)
    if not path:
        return jsonify({"error": "not found"}), 404
    return send_file(path, mimetype="audio/wav", as_attachment=False)


@app.route("/api/recordings/<filename>/download")
def api_recording_download(filename):
    path = _validate_wav_filename(filename)
    if not path:
        return jsonify({"error": "not found"}), 404
    return send_file(path, mimetype="audio/wav", as_attachment=True, download_name=filename)


@app.route("/api/recordings/<filename>/spectrogram")
def api_spectrogram(filename):
    path = _validate_wav_filename(filename)
    if not path:
        return jsonify({"error": "not found"}), 404
    png_path = _spectrogram_path(path)
    try:
        result = subprocess.run(
            ["sox", path, "-n", "spectrogram", "-o", png_path],
            capture_output=True, timeout=60,
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr.decode(errors="replace")}), 500
        return send_file(png_path, mimetype="image/png")
    except FileNotFoundError:
        return jsonify({"error": "sox not installed"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "sox timed out"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/recordings/<filename>", methods=["DELETE"])
def api_recording_delete(filename):
    if not _SAFE_FILENAME.match(filename):
        return jsonify({"error": "invalid filename"}), 400
    path = os.path.join(ASTERISK_TMP, filename)
    try:
        os.remove(path)
        png = _spectrogram_path(path)
        if os.path.isfile(png):
            os.remove(png)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sox/<filename>")
def api_sox(filename):
    path = _validate_wav_filename(filename)
    if not path:
        return jsonify({"error": "file not found"}), 404
    try:
        stat_out = subprocess.run(["sox", path, "-n", "stat"], capture_output=True, text=True, timeout=30)
        stats_out = subprocess.run(["sox", path, "-n", "stats"], capture_output=True, text=True, timeout=30)
        return jsonify({
            "stat": stat_out.stderr or stat_out.stdout,
            "stats": stats_out.stderr or stats_out.stdout,
        })
    except FileNotFoundError:
        return jsonify({"error": "sox not installed"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "sox timed out"}), 500


# ---------------------------------------------------------------------------
# Routes — tcpdump packet capture
# ---------------------------------------------------------------------------

@app.route("/api/capture/start", methods=["POST"])
def api_capture_start():
    """Start tcpdump capturing SIP + RTP traffic.

    Body: {"filter": "optional pcap filter", "filename": "optional.pcap"}
    Default filter: port 5060 or portrange 10000-10200
    """
    global _tcpdump_proc, _tcpdump_file
    with _tcpdump_lock:
        if _tcpdump_proc and _tcpdump_proc.poll() is None:
            return jsonify({"ok": False, "error": "capture already running"}), 409

        body = request.get_json(force=True, silent=True) or {}
        pcap_filter = body.get("filter", "port 5060 or portrange 10000-10200")
        fname = body.get("filename") or "capture-{}.pcap".format(int(time.time()))
        if not fname.endswith(".pcap"):
            fname += ".pcap"
        # Sanitize
        fname = re.sub(r"[^\w. -]", "_", fname)
        pcap_path = os.path.join(CAPTURES_DIR, fname)

        if not _SAFE_PCAP.match(fname):
            return jsonify({"ok": False, "error": "invalid filename"}), 400

        # "--" ends option parsing, so the filter can't smuggle in tcpdump
        # options such as -z (run a command) or -w (write elsewhere).
        cmd = ["tcpdump", "-i", "any", "-w", pcap_path, "-s", "0", "--"]
        if pcap_filter.strip():
            cmd.append(pcap_filter)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except FileNotFoundError:
            return jsonify({"ok": False, "error": "tcpdump not installed"}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        # A bad filter makes tcpdump exit straight away; report it instead of
        # pretending the capture is running.
        try:
            proc.wait(timeout=0.5)
            err = proc.stderr.read().decode(errors="replace").strip()
            return jsonify({"ok": False, "error": err or "tcpdump exited"}), 400
        except subprocess.TimeoutExpired:
            pass
        _tcpdump_proc = proc
        _tcpdump_file = fname

    return jsonify({"ok": True, "filename": fname, "pid": _tcpdump_proc.pid})


@app.route("/api/capture/stop", methods=["POST"])
def api_capture_stop():
    global _tcpdump_proc, _tcpdump_file
    with _tcpdump_lock:
        if not _tcpdump_proc or _tcpdump_proc.poll() is not None:
            _tcpdump_proc = None
            return jsonify({"ok": True, "message": "no capture running"})
        _tcpdump_proc.terminate()
        try:
            _tcpdump_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _tcpdump_proc.kill()
        fname = _tcpdump_file
        _tcpdump_proc = None
        _tcpdump_file = None
    return jsonify({"ok": True, "filename": fname})


@app.route("/api/capture/status")
def api_capture_status():
    with _tcpdump_lock:
        running = _tcpdump_proc is not None and _tcpdump_proc.poll() is None
        fname = _tcpdump_file if running else None
        pid = _tcpdump_proc.pid if running else None
    return jsonify({"running": running, "filename": fname, "pid": pid})


@app.route("/api/captures")
def api_captures():
    files = []
    for path in sorted(glob.glob(os.path.join(CAPTURES_DIR, "*.pcap"))):
        try:
            st = os.stat(path)
            files.append({"name": os.path.basename(path), "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            pass
    return jsonify({"captures": files})


@app.route("/api/captures/<filename>/download")
def api_capture_download(filename):
    if not filename or ".." in filename or "/" in filename:
        return jsonify({"error": "invalid"}), 400
    path = os.path.join(CAPTURES_DIR, filename)
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    return send_file(path, mimetype="application/vnd.tcpdump.pcap",
                     as_attachment=True, download_name=filename)


@app.route("/api/captures/<filename>", methods=["DELETE"])
def api_capture_delete(filename):
    if not filename or ".." in filename or "/" in filename:
        return jsonify({"error": "invalid"}), 400
    path = os.path.join(CAPTURES_DIR, filename)
    try:
        os.remove(path)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Routes — config editor
# ---------------------------------------------------------------------------

@app.route("/api/config/<filename>", methods=["GET"])
def api_config_get(filename):
    if filename not in ALLOWED_CONFS:
        return jsonify({"error": "not allowed"}), 403
    path = os.path.join(ASTERISK_CONF, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return jsonify({"content": f.read()})
    except FileNotFoundError:
        return jsonify({"error": "file not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config/<filename>", methods=["POST"])
def api_config_post(filename):
    if filename not in ALLOWED_CONFS:
        return jsonify({"error": "not allowed"}), 403
    body = request.get_json(force=True, silent=True) or {}
    content = body.get("content", "")
    path = os.path.join(ASTERISK_CONF, filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return jsonify({"ok": False, "error": "write failed: {}".format(e)}), 500
    resp = ami.send_action({"Action": "Command", "Command": "core reload"})
    # Asterisk 13 and earlier answer "Follows"; newer versions answer "Success".
    reload_msg = "reloaded" if resp and resp.get("Response") in ("Success", "Follows") else "reload response uncertain"
    return jsonify({"ok": True, "reload": reload_msg})


# ---------------------------------------------------------------------------
# Routes — SSE log stream
# ---------------------------------------------------------------------------

def _open_log(seek_end):
    try:
        fh = open(ASTERISK_LOG, "r", encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    if seek_end:
        fh.seek(0, 2)
    return fh, os.fstat(fh.fileno()).st_ino


@app.route("/logs")
def logs_sse():
    def generate():
        events = ami.subscribe()
        log_fh, log_ino = _open_log(seek_end=True)
        last_check = time.time()
        try:
            while True:
                drained = False
                try:
                    while True:
                        event = events.get_nowait()
                        yield "data: {}\n\n".format(json.dumps({"source": "ami", "data": event}))
                        drained = True
                except queue.Empty:
                    pass
                if not drained and time.time() - last_check >= 1.0:
                    # Log not there yet (Asterisk still starting) or rotated: reopen.
                    try:
                        ino = os.stat(ASTERISK_LOG).st_ino
                    except OSError:
                        ino = None
                    if ino != log_ino:
                        if log_fh:
                            log_fh.close()
                        log_fh, log_ino = _open_log(seek_end=False)
                if log_fh:
                    try:
                        line = log_fh.readline()
                        while line:
                            yield "data: {}\n\n".format(
                                json.dumps({"source": "log", "data": line.rstrip()})
                            )
                            drained = True
                            line = log_fh.readline()
                    except Exception:
                        pass
                if not drained:
                    now = time.time()
                    if now - last_check >= 1.0:
                        yield ": keepalive\n\n"
                        last_check = now
                    else:
                        time.sleep(0.1)
        finally:
            ami.unsubscribe(events)
            if log_fh:
                log_fh.close()

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8087, threaded=True)

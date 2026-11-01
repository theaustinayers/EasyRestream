#!/usr/bin/env python3
"""Local control plane for independent, stream-copy-only FFmpeg outputs."""
import argparse
import base64
import collections
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

DESTINATIONS = ("youtube", "facebook", "x")
ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("relay")


def validate_username(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        raise ValueError("Username must be 1-64 letters, digits, dots, underscores or hyphens.")
    return value


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 16 <= len(password) <= 256:
        raise ValueError("Password must be 16-256 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000)
    return f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or len(password) > 256:
        return False
    scheme, iterations, salt, expected = encoded.split("$")
    if scheme != "pbkdf2_sha256" or iterations != "600000":
        raise ValueError("Unsupported password hash.")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(digest.hex(), expected)


def validate_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Enter a valid RTMP(S) URL.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid destination URL.") from exc
    if (parsed.scheme not in ("rtmp", "rtmps") or not parsed.hostname
            or not parsed.path.strip("/") or parsed.username or parsed.password
            or parsed.fragment or any(c.isspace() or ord(c) < 32 for c in value)
            or (port is not None and port == 0)):
        raise ValueError("Use an RTMP(S) URL with a stream-key path and no userinfo or fragment.")
    return value


def save_config(path: Path, data: dict) -> None:
    save_text(path, json.dumps(data, indent=2) + "\n")


def save_text(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(prefix=".relay-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            if os.name != "nt":
                os.fchmod(output.fileno(), 0o600)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def ffmpeg_command(binary: str, source: str, destination: str) -> list[str]:
    return [
        binary, "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
        "-stats_period", "1", "-progress", "pipe:1",
        "-rtsp_transport", "tcp", "-timeout", "10000000", "-i", source,
        "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
        "-f", "flv", "-flvflags", "no_duration_filesize",
        "-rw_timeout", "10000000", destination,
    ]


class StreamStats:
    def __init__(self):
        self.total_bytes = 0
        self.samples = collections.deque()
        self.last_size = 0
        self.last_frame = 0
        self.last_media = 0
        self.last_data = None

    def new_connection(self):
        self.samples.clear()
        self.last_size = self.last_frame = self.last_media = 0
        self.last_data = None

    def update(self, progress: dict, now: float) -> bool:
        try:
            size = int(progress["total_size"])
            frame = int(progress["frame"])
            media = int(progress["out_time_us"])
        except (KeyError, ValueError, TypeError):
            raise ValueError("FFmpeg did not provide valid byte/frame/timestamp counters.")
        if min(size, frame, media) < 0:
            raise ValueError("FFmpeg reported negative progress counters.")
        if size < self.last_size or frame < self.last_frame or media < self.last_media:
            self.new_connection()
        moved = size > self.last_size and frame > self.last_frame
        self.total_bytes += max(0, size - self.last_size)
        self.last_size, self.last_frame, self.last_media = size, frame, media
        if moved:
            self.last_data = now
        self.samples.append((now, size, frame, media))
        while len(self.samples) > 2 and self.samples[1][0] <= now - 5:
            self.samples.popleft()
        return moved

    def snapshot(self, now: float, active: bool) -> dict:
        result = {"bandwidth_mbps": None, "bitrate_mbps": None, "fps": None,
                  "media_fps": None, "total_bytes": self.total_bytes,
                  "sample_age_seconds": None if self.last_data is None else round(now - self.last_data, 1)}
        if not active or self.last_data is None or now - self.last_data > 5:
            result.update(bandwidth_mbps=0.0, fps=0.0)
            return result
        if len(self.samples) < 2:
            return result
        first, last = self.samples[0], self.samples[-1]
        wall = last[0] - first[0]
        media = (last[3] - first[3]) / 1_000_000
        size, frames = last[1] - first[1], last[2] - first[2]
        if wall > 0:
            result["bandwidth_mbps"] = round(size * 8 / wall / 1_000_000, 3)
            result["fps"] = round(frames / wall, 1)
        if media > 0:
            result["bitrate_mbps"] = round(size * 8 / media / 1_000_000, 3)
            result["media_fps"] = round(frames / media, 1)
        return result


class Output:
    def __init__(self, name: str, source: str, binary: str):
        self.name, self.source, self.binary = name, source, binary
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.state = "stopped"
        self.error = ""
        self.retries = 0
        self.last_progress = 0.0
        self.stats = StreamStats()

    def snapshot(self) -> dict:
        with self.lock:
            state = self.state
            if state == "forwarding" and time.monotonic() - self.last_progress > 5:
                state = "stalled"
            return {"name": self.name, "state": state, "error": self.error,
                    "retries": self.retries,
                    "stats": self.stats.snapshot(time.monotonic(), state == "forwarding")}

    def start(self, url: str) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        with self.lock:
            self.state, self.error, self.retries = "connecting", "", 0
            self.stats = StreamStats()
        self.thread = threading.Thread(target=self.run, args=(url,), name=self.name)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
            self.thread = None
        with self.lock:
            self.state = "stopped"
            self.error = ""

    def redact(self, text: str, url: str) -> str:
        destination = urlsplit(url)
        source = urlsplit(self.source)
        secrets = [url, self.source, destination.path.strip("/"),
                   unquote(destination.path.rsplit("/", 1)[-1]),
                   source.path.rsplit("/", 1)[-1]]
        secrets.extend(value for _, value in parse_qsl(destination.query))
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[redacted]")
        return re.sub(r"(?:rtmp|rtmps|rtsp)://[^\s'\"]+", "[redacted URL]", text)[:1500]

    def run(self, url: str) -> None:
        delay = 1
        while not self.stop_event.is_set():
            with self.lock:
                self.state = "connecting"
                self.stats.new_connection()
            try:
                process = subprocess.Popen(
                    ffmpeg_command(self.binary, self.source, url),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8",
                    errors="replace", bufsize=1,
                )
            except OSError:
                LOG.exception("%s: unable to launch FFmpeg", self.name)
                with self.lock:
                    self.state, self.error = "error", "Cannot launch FFmpeg; check service logs."
                return
            errors = collections.deque(maxlen=8)
            made_progress = threading.Event()

            def read_progress():
                progress = {}
                for line in process.stdout:
                    key, separator, value = line.strip().partition("=")
                    if not separator:
                        continue
                    progress[key] = value
                    if key == "progress":
                        with self.lock:
                            try:
                                moved = self.stats.update(progress, time.monotonic())
                            except ValueError as exc:
                                self.error = str(exc)
                                LOG.warning("%s: %s", self.name, exc)
                                moved = False
                            if moved:
                                made_progress.set()
                                self.state = "forwarding"
                                self.last_progress = time.monotonic()
                                self.error = ""
                        progress = {}

            def read_errors():
                for line in process.stderr:
                    errors.append(self.redact(line.strip(), url))

            readers = [threading.Thread(target=read_progress),
                       threading.Thread(target=read_errors)]
            for reader in readers:
                reader.start()
            started = time.monotonic()
            while process.poll() is None and not self.stop_event.wait(0.25):
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            code = process.wait()
            for reader in readers:
                reader.join()
            process.stdout.close()
            process.stderr.close()
            if self.stop_event.is_set():
                break
            if made_progress.is_set() and time.monotonic() - started > 30:
                delay = 1
            message = "\n".join(errors) or f"FFmpeg exited with code {code}."
            LOG.warning("%s: %s; retry in %ss", self.name, message, delay)
            with self.lock:
                self.state, self.error = "retrying", message
                self.retries += 1
            if self.stop_event.wait(delay):
                break
            delay = min(delay * 2, 30)


class Controller:
    def __init__(self, config_path: Path, monitor_enabled: bool = False):
        self.path = config_path
        if os.name != "nt" and config_path.stat().st_mode & 0o077:
            raise ValueError("Configuration must not be accessible to group/others (chmod 600).")
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.config.setdefault("dashboard_username", "admin")
        validate_username(self.config["dashboard_username"])
        if "dashboard_password_hash" not in self.config:
            self.config["dashboard_password_hash"] = hash_password(self.config["dashboard_password"])
            del self.config["dashboard_password"]
            save_config(self.path, self.config)
            LOG.info("Migrated dashboard credentials to a salted password hash")
        verify_password("", self.config["dashboard_password_hash"])
        source = urlsplit(self.config["source"])
        if source.scheme != "rtsp" or source.hostname != "127.0.0.1":
            raise ValueError("Source must be a local MediaMTX RTSP URL.")
        destinations = self.config["destinations"]
        if set(destinations) != set(DESTINATIONS):
            raise ValueError("Configure exactly youtube, facebook and x.")
        for url in destinations.values():
            if url:
                validate_url(url)
        self.lock = threading.RLock()
        self.auth_digest = None
        self.outputs = {name: Output(name, self.config["source"],
                                    self.config.get("ffmpeg", "ffmpeg"))
                        for name in DESTINATIONS}
        self.inbound = Output("inbound", self.config["source"],
                              self.config.get("ffmpeg", "ffmpeg"))
        if monitor_enabled:
            self.inbound.start(os.devnull)

    def status(self) -> dict:
        with self.lock:
            inbound = self.inbound.snapshot()
            if inbound["state"] in ("connecting", "retrying"):
                inbound["state"] = "waiting"
            return {"username": self.config["dashboard_username"],
                    "ingest_server": self.config.get("ingest_server", ""),
                    "inbound": inbound, "outputs": [dict(output.snapshot(),
                                     configured=bool(self.config["destinations"][name]))
                                for name, output in self.outputs.items()]}

    def authenticate(self, header: str) -> bool:
        digest = hashlib.sha256(header.encode()).digest()
        with self.lock:
            if self.auth_digest is not None and hmac.compare_digest(self.auth_digest, digest):
                return True
            try:
                scheme, value = header.split(" ", 1)
                if scheme != "Basic":
                    return False
                decoded = base64.b64decode(value, validate=True).decode("utf-8")
                username, password = decoded.split(":", 1)
            except (ValueError, UnicodeError):
                return False
            if not hmac.compare_digest(username.encode(), self.config["dashboard_username"].encode()):
                return False
            if not verify_password(password, self.config["dashboard_password_hash"]):
                return False
            # Cache only the verified header digest; polling does not repeatedly run PBKDF2.
            self.auth_digest = digest
            return True

    def update_account(self, username: str, password: str, current_password: str) -> None:
        with self.lock:
            if not verify_password(current_password, self.config["dashboard_password_hash"]):
                raise ValueError("Current password is incorrect.")
            username = validate_username(username)
            if not isinstance(password, str):
                raise ValueError("Enter a password, or leave it empty to keep your password.")
            updated = dict(self.config, dashboard_username=username)
            if password:
                updated["dashboard_password_hash"] = hash_password(password)
            save_config(self.path, updated)
            self.config = updated
            self.auth_digest = None

    def ingest_settings(self) -> dict:
        with self.lock:
            path, key = self.config["source"].rsplit("/", 1)
            prefix = urlsplit(path).path
            server = self.config.get("ingest_server", "") or f"rtmp://YOUR_SERVER_IP:1935{prefix}"
            return {"server": server, "stream_key": key, "url": f"{server}/{key}",
                    "configured": bool(self.config.get("ingest_server"))}

    def update_ingest(self, server: str, key: str, current_password: str) -> None:
        with self.lock:
            if not verify_password(current_password, self.config["dashboard_password_hash"]):
                raise ValueError("Current password is incorrect.")
            server = validate_url(server)
            parsed = urlsplit(server)
            prefix, old_key = self.config["source"].rsplit("/", 1)
            if (parsed.scheme != "rtmp" or parsed.port != 1935 or parsed.query
                    or parsed.path != urlsplit(prefix).path or parsed.hostname == "your_server_ip"):
                raise ValueError(f"Use rtmp://YOUR_HOST:1935{urlsplit(prefix).path} as the OBS server URL.")
            if not isinstance(key, str) or (key and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key)):
                raise ValueError("Stream key must be 16-128 letters, digits, underscores or hyphens.")
            key = key or old_key
            updated = dict(self.config, ingest_server=server, source=f"{prefix}/{key}")
            if key == old_key:
                save_config(self.path, updated)
                self.config = updated
                return
            if any(output.thread is not None and output.thread.is_alive()
                   for output in self.outputs.values()):
                raise ValueError("Stop all destinations before changing the ingest key.")
            if self.inbound.snapshot()["state"] in ("forwarding", "stalled"):
                raise ValueError("Stop OBS before changing the ingest key.")
            media_path = self.path.parent / "mediamtx.yml"
            original = media_path.read_text(encoding="utf-8")
            old_path = urlsplit(self.config["source"]).path.lstrip("/")
            new_path = urlsplit(updated["source"]).path.lstrip("/")
            if original.count(old_path) != 3:
                raise ValueError("MediaMTX configuration differs from the installer template; update it manually.")
            media = original.replace(old_path, new_path)
            monitoring = self.inbound.thread is not None
            self.inbound.stop()
            try:
                # MediaMTX hot-reloads this file; roll it back if the second save fails.
                save_text(media_path, media)
                try:
                    save_config(self.path, updated)
                except OSError:
                    LOG.error("Configuration save failed; restoring the previous MediaMTX key")
                    save_text(media_path, original)
                    raise
            except OSError:
                LOG.exception("Ingest key update failed; check both configuration files before reconnecting OBS")
                if monitoring:
                    self.inbound.start(os.devnull)
                raise
            self.config = updated
            self.inbound = Output("inbound", updated["source"], self.config.get("ffmpeg", "ffmpeg"))
            for output in self.outputs.values():
                output.source = updated["source"]
            if monitoring:
                self.inbound.start(os.devnull)
            LOG.info("Ingest key changed; update OBS before reconnecting")

    def action(self, name: str, action: str, url=None) -> None:
        with self.lock:
            if name not in self.outputs:
                raise ValueError("Unknown destination.")
            output = self.outputs[name]
            if action == "start":
                target = self.config["destinations"][name]
                if not target:
                    raise ValueError("Save a destination URL first.")
                output.start(target)
            elif action == "stop":
                output.stop()
            elif action == "configure":
                if not isinstance(url, str):
                    raise ValueError("Destination URL must be a string.")
                target = validate_url(url) if url else ""
                if output.thread is not None and output.thread.is_alive():
                    raise ValueError("Stop this destination before changing its URL.")
                updated = dict(self.config)
                updated["destinations"] = dict(self.config["destinations"], **{name: target})
                save_config(self.path, updated)
                self.config = updated
            else:
                raise ValueError("Unknown action.")

    def close(self) -> None:
        with self.lock:
            # Signal all children before joining, so shutdown is concurrent.
            for output in self.outputs.values():
                output.stop_event.set()
            self.inbound.stop_event.set()
            for output in self.outputs.values():
                output.stop()
            self.inbound.stop()


def make_handler(controller: Controller):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Request paths, headers and payloads may contain secrets.
            LOG.info("dashboard request completed")

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def respond(self, code, body, content_type="application/json"):
            if isinstance(body, dict):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header("Connection", "close")
            if code == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="RTMP relay", charset="UTF-8"')
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            if not controller.authenticate(self.headers.get("Authorization", "")):
                self.respond(401, {"error": "Authentication required."})
                return False
            host = self.headers.get("Host", "")
            port = self.server.server_port
            if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                self.respond(403, {"error": "Use the local dashboard address."})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == "/api/status":
                self.respond(200, controller.status())
                return
            if self.path == "/api/ingest":
                self.respond(200, controller.ingest_settings())
                return
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.path not in files:
                self.respond(404, {"error": "Not found."})
                return
            name, kind = files[self.path]
            self.respond(200, (ROOT / "web" / name).read_bytes(), kind)

        def do_POST(self):
            if not self.authorized():
                return
            origin = self.headers.get("Origin")
            if (self.headers.get("X-Relay-Control") != "1"
                    or (origin is not None and origin != "http://" + self.headers["Host"])):
                self.respond(403, {"error": "Invalid control request."})
                return
            if self.path not in ("/api/action", "/api/account", "/api/ingest"):
                self.respond(404, {"error": "Not found."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192 or self.headers.get("Content-Type") != "application/json":
                    raise ValueError("Expected a JSON body of at most 8192 bytes.")
                data = json.loads(self.rfile.read(length))
                fields = {"/api/action": {"name", "action", "url"},
                          "/api/account": {"username", "password", "current_password"},
                          "/api/ingest": {"server", "stream_key", "current_password"}}
                if not isinstance(data, dict) or set(data) - fields[self.path]:
                    raise ValueError("Invalid action payload.")
                if self.path == "/api/account":
                    controller.update_account(data.get("username"), data.get("password"),
                                              data.get("current_password"))
                elif self.path == "/api/ingest":
                    controller.update_ingest(data.get("server"), data.get("stream_key"),
                                             data.get("current_password"))
                else:
                    controller.action(data.get("name"), data.get("action"), data.get("url"))
            except (ValueError, TypeError, UnicodeError) as exc:
                LOG.warning("Rejected invalid dashboard action")
                self.respond(400, {"error": str(exc)})
                return
            except OSError:
                LOG.exception("Cannot save dashboard or ingest configuration")
                self.respond(500, {"error": "Could not save configuration; check service logs and verify ingest configuration before reconnecting OBS."})
                return
            self.respond(200, controller.status())

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    controller = Controller(args.config, monitor_enabled=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(controller))
    server.daemon_threads = True
    def shutdown(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info("Dashboard listening on 127.0.0.1:%s; all outputs start stopped", args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        controller.close()


if __name__ == "__main__":
    main()

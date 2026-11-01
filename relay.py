#!/usr/bin/env python3
"""Local control plane for independent, stream-copy-only FFmpeg outputs."""
import argparse
import base64
import collections
import hmac
import json
import logging
import os
from pathlib import Path
import re
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
    fd, name = tempfile.mkstemp(prefix=".relay-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            if os.name != "nt":
                os.fchmod(output.fileno(), 0o600)
            json.dump(data, output, indent=2)
            output.write("\n")
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
        if not isinstance(self.config.get("dashboard_password"), str) or len(
                self.config["dashboard_password"]) < 16:
            raise ValueError("Dashboard password must have at least 16 characters.")
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
            return {"inbound": inbound, "outputs": [dict(output.snapshot(),
                                     configured=bool(self.config["destinations"][name]))
                                for name, output in self.outputs.items()]}

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
                self.send_header("WWW-Authenticate", 'Basic realm="RTMP relay"')
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            expected = "Basic " + base64.b64encode(
                ("admin:" + controller.config["dashboard_password"]).encode()).decode()
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(),
                                       expected.encode()):
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
            if self.path != "/api/action":
                self.respond(404, {"error": "Not found."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192 or self.headers.get("Content-Type") != "application/json":
                    raise ValueError("Expected a JSON body of at most 8192 bytes.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or set(data) - {"name", "action", "url"}:
                    raise ValueError("Invalid action payload.")
                controller.action(data.get("name"), data.get("action"), data.get("url"))
            except (ValueError, TypeError, UnicodeError):
                LOG.warning("Rejected invalid dashboard action")
                self.respond(400, {"error": "Invalid request: use a valid URL, configure before starting, and stop before editing."})
                return
            except OSError:
                LOG.exception("Cannot save destination configuration")
                self.respond(500, {"error": "Could not save configuration; check service logs."})
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

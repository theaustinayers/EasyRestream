"""Bounded real 4K60 local fanout test; all children and temporary files are owned here."""
import argparse
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import relay


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def wait_for(predicate, description, timeout=35):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError("Timed out: " + description)


def terminate(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mediamtx", required=True)
    args = parser.parse_args()
    binary = str(Path(args.mediamtx).resolve())
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("Install ffmpeg/ffprobe before running this integration test.")
    ports = set()
    while len(ports) < 4:
        ports.add(free_port())
    rtmp, rtsp, api, rejected_port = ports
    children = []
    controller = None
    with tempfile.TemporaryDirectory(prefix="relay-integration-") as directory:
        folder = Path(directory)
        # Reuse the exact installer template, adding local sink permissions.
        installer = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
        template = installer.split('media = f"""', 1)[1].split('"""', 1)[0]
        config = template.replace("{key}", "testkey")
        config = config.replace("api: false", f"api: true\napiAddress: 127.0.0.1:{api}")
        config = config.replace("rtmpAddress: :1935", f"rtmpAddress: 127.0.0.1:{rtmp}")
        config = config.replace("127.0.0.1:8554", f"127.0.0.1:{rtsp}")
        config = config.replace("      - action: read", "      - action: api\n"
                                "      - action: publish\n        path: ~^sink/\n"
                                "      - action: read")
        config = config.replace("        path: ingest/testkey\npaths:",
                                "        path: \"\"\npaths:")
        config += '  "~^sink/":\n    source: publisher\n'
        (folder / "media.yml").write_text(config)
        relay.save_config(folder / "relay.json", {
            "dashboard_password": "integration-password-only",
            "source": f"rtsp://127.0.0.1:{rtsp}/ingest/testkey",
            "ffmpeg": ffmpeg,
            "destinations": {name: f"rtmp://127.0.0.1:{rtmp}/sink/{name}"
                             for name in relay.DESTINATIONS},
        })
        with (folder / "media.log").open("wb") as media_log:
            media = subprocess.Popen([binary, str(folder / "media.yml")],
                                     stdout=media_log, stderr=subprocess.STDOUT)
            children.append(media)
            try:
                def paths():
                    if media.poll() is not None:
                        raise AssertionError((folder / "media.log").read_text())
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{api}/v3/paths/list", timeout=2) as response:
                            return json.load(response)["items"]
                    except OSError:
                        return []

                def ready(name):
                    return any(item["name"] == name and item["ready"] for item in paths())

                wait_for(lambda: bool(paths()), "MediaMTX readiness")
                sample = folder / "4k60.flv"
                subprocess.run([
                    ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=3840x2160:r=60", "-f", "lavfi", "-i",
                    "anullsrc=r=48000:cl=stereo", "-t", "2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2",
                    "-pix_fmt", "yuv420p", "-g", "60", "-c:a", "aac",
                    "-f", "flv", str(sample),
                ], check=True, timeout=90)
                source_log = (folder / "source.log").open("wb")
                try:
                    def publish():
                        process = subprocess.Popen([
                            ffmpeg, "-nostdin", "-v", "error", "-re", "-stream_loop", "-1",
                            "-i", str(sample), "-c", "copy", "-f", "flv",
                            f"rtmp://127.0.0.1:{rtmp}/ingest/testkey",
                        ], stdout=subprocess.DEVNULL, stderr=source_log)
                        children.append(process)
                        return process

                    source = publish()
                    wait_for(lambda: ready("ingest/testkey"), "4K60 input")
                    controller = relay.Controller(folder / "relay.json")
                    for name in relay.DESTINATIONS:
                        controller.action(name, "start")
                    wait_for(lambda: all(ready("sink/" + name) for name in relay.DESTINATIONS),
                             "three outputs")
                    wait_for(lambda: all(item["state"] == "forwarding" for item in controller.status()["outputs"]),
                             "three outputs reporting progress")
                    probe = subprocess.run([
                        ffprobe, "-v", "error", "-rtsp_transport", "tcp",
                        "-read_intervals", "%+1", "-show_streams", "-of", "json",
                        f"rtsp://127.0.0.1:{rtsp}/sink/youtube",
                    ], capture_output=True, text=True, check=True, timeout=20)
                    streams = json.loads(probe.stdout)["streams"]
                    video = next(stream for stream in streams if stream["codec_type"] == "video")
                    audio = next(stream for stream in streams if stream["codec_type"] == "audio")
                    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 3840, 2160), video
                    assert video["r_frame_rate"] == "60/1", video
                    assert audio["codec_name"] == "aac", audio
                    print("PASS: three live outputs; relayed video H.264 3840x2160 60fps, audio AAC")

                    controller.action("facebook", "stop")
                    wait_for(lambda: not ready("sink/facebook"), "independent stop")
                    assert ready("sink/youtube") and ready("sink/x")
                    controller.action("facebook", "configure",
                                      f"rtmp://127.0.0.1:{rejected_port}/sink/rejected")
                    controller.action("facebook", "start")
                    wait_for(lambda: controller.outputs["facebook"].snapshot()["retries"] > 0,
                             "rejected destination retries")
                    assert ready("sink/youtube") and ready("sink/x")
                    controller.action("facebook", "stop")
                    print("PASS: independent stop and failed-destination isolation")

                    terminate(source)
                    wait_for(lambda: not ready("ingest/testkey"), "source disconnect")
                    wait_for(lambda: controller.outputs["youtube"].snapshot()["retries"] > 0,
                             "source disconnect retry")
                    publish()
                    wait_for(lambda: ready("sink/youtube") and ready("sink/x"),
                             "source recovery", timeout=45)
                    controller.action("facebook", "configure",
                                      f"rtmp://127.0.0.1:{rtmp}/sink/facebook")
                    controller.action("facebook", "start")
                    wait_for(lambda: ready("sink/facebook"), "destination restart")
                    print("PASS: OBS-style reconnect and individual restart")
                    controller.close()
                    wait_for(lambda: all(not ready("sink/" + name) for name in relay.DESTINATIONS),
                             "shutdown cleanup")
                    controller = relay.Controller(folder / "relay.json")
                    assert all(item["state"] == "stopped" for item in controller.status()["outputs"])
                    print("PASS: shutdown cleanup and restart defaults")
                finally:
                    if controller is not None:
                        controller.close()
                    for child in children[1:]:
                        terminate(child)
                    source_log.close()
            finally:
                for child in reversed(children):
                    terminate(child)


if __name__ == "__main__":
    main()

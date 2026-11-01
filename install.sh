#!/usr/bin/env bash
set -euo pipefail
umask 077
if [[ $EUID -ne 0 ]]; then echo "Run with sudo bash install.sh" >&2; exit 1; fi
source /etc/os-release
if [[ $ID != ubuntu || $VERSION_ID != 26.04 ]]; then
  echo "This installer targets Ubuntu 26.04 LTS only." >&2; exit 1
fi
BASE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -e /etc/rtmp-relay || -e /opt/rtmp-relay || -e /usr/local/bin/rtmp-relay-mediamtx ||
      -e /etc/systemd/system/rtmp-relay.service ||
      -e /etc/systemd/system/rtmp-relay-media.service ]]; then
  echo "Existing installation detected. Refusing to overwrite it; see README." >&2; exit 1
fi
for port in 1935 8080 8554; do
  if ss -H -ltn "sport = :$port" | grep -q .; then
    echo "TCP port $port is occupied. Refusing to disrupt the existing service." >&2; exit 1
  fi
done
case "$(uname -m)" in
  x86_64) ARCH=amd64; SHA=0a2ea7eba96023631efa1bea7804823a9145b043b6646a8a05798d4ac24ed635 ;;
  aarch64) ARCH=arm64; SHA=27f038237cb5f77a724f6afc58eca2798f2bc4edf9989895631a80eeb41a4fe2 ;;
  *) echo "Supported architectures: x86_64 and aarch64." >&2; exit 1 ;;
esac
for file in relay.py web/index.html web/app.js web/style.css; do
  [[ -f "$BASE/$file" ]] || { echo "Incomplete package: missing $file" >&2; exit 1; }
done
apt-get update
apt-get install -y --no-install-recommends python3 ffmpeg curl ca-certificates
WORK="$(mktemp -d)"
trap 'rm -f -- "$WORK/mediamtx.tar.gz" "$WORK/mediamtx"; rmdir -- "$WORK"' EXIT
curl --fail --location --proto '=https' --tlsv1.2 \
  "https://github.com/bluenviron/mediamtx/releases/download/v1.21.3/mediamtx_v1.21.3_linux_${ARCH}.tar.gz" \
  -o "$WORK/mediamtx.tar.gz"
printf '%s  %s\n' "$SHA" "$WORK/mediamtx.tar.gz" | sha256sum --check --status
tar -xzf "$WORK/mediamtx.tar.gz" -C "$WORK" mediamtx
if ! id rtmp-relay >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin rtmp-relay
elif [[ "$(getent passwd rtmp-relay | cut -d: -f7)" != /usr/sbin/nologin ]]; then
  echo "Existing rtmp-relay account is not a dedicated service account." >&2; exit 1
fi
install -d -m 755 /opt/rtmp-relay /opt/rtmp-relay/web
install -m 644 "$BASE/relay.py" /opt/rtmp-relay/relay.py
install -m 644 "$BASE/web/"{index.html,app.js,style.css} /opt/rtmp-relay/web/
install -m 755 "$WORK/mediamtx" /usr/local/bin/rtmp-relay-mediamtx
install -d -o rtmp-relay -g rtmp-relay -m 700 /etc/rtmp-relay
python3 - <<'PY'
import json
import os
from pathlib import Path
import secrets

path = Path("/etc/rtmp-relay")
key = secrets.token_hex(24)
password = secrets.token_urlsafe(24)
media = f"""logLevel: warn
api: false
metrics: false
pprof: false
playback: false
hls: false
webrtc: false
srt: false
moq: false
rtmp: true
rtmpAddress: :1935
rtsp: true
rtspAddress: 127.0.0.1:8554
rtspTransports: [tcp]
authMethod: internal
authInternalUsers:
  - user: any
    ips: []
    permissions:
      - action: publish
        path: ingest/{key}
  - user: any
    ips: [127.0.0.1, "::1"]
    permissions:
      - action: read
        path: ingest/{key}
paths:
  ingest/{key}:
    source: publisher
    overridePublisher: false
"""
(path / "mediamtx.yml").write_text(media)
(path / "config.json").write_text(json.dumps({
    "dashboard_password": password,
    "source": f"rtsp://127.0.0.1:8554/ingest/{key}",
    "ffmpeg": "/usr/bin/ffmpeg",
    "destinations": {"youtube": "", "facebook": "", "x": ""}
}, indent=2) + "\n")
(path / "access.txt").write_text(
    f"Dashboard: http://127.0.0.1:8080\nUsername: admin\nPassword: {password}\n"
    f"OBS server: rtmp://YOUR_SERVER_IP:1935/ingest\nOBS stream key: {key}\n"
    "Treat this file and the OBS stream key as secrets. RTMP ingest is not encrypted.\n")
for file in path.iterdir():
    os.chmod(file, 0o600)
PY
chown rtmp-relay:rtmp-relay /etc/rtmp-relay/config.json /etc/rtmp-relay/mediamtx.yml
cat > /etc/systemd/system/rtmp-relay-media.service <<'UNIT'
[Unit]
Description=RTMP relay MediaMTX ingest
After=network-online.target
Wants=network-online.target

[Service]
User=rtmp-relay
Group=rtmp-relay
ExecStart=/usr/local/bin/rtmp-relay-mediamtx /etc/rtmp-relay/mediamtx.yml
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectProc=invisible
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/rtmp-relay.service <<'UNIT'
[Unit]
Description=RTMP relay dashboard and output supervisor
After=network-online.target rtmp-relay-media.service
Wants=network-online.target rtmp-relay-media.service

[Service]
User=rtmp-relay
Group=rtmp-relay
ExecStart=/usr/bin/python3 /opt/rtmp-relay/relay.py --config /etc/rtmp-relay/config.json
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
KillMode=control-group
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectProc=invisible
ReadWritePaths=/etc/rtmp-relay
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 /etc/systemd/system/rtmp-relay{,-media}.service
systemctl daemon-reload
systemctl enable --now rtmp-relay-media.service rtmp-relay.service
sleep 2
systemctl is-active --quiet rtmp-relay-media.service rtmp-relay.service
code="$(curl --silent --show-error --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/)"
[[ "$code" == 401 ]] || { echo "Dashboard readiness check failed: HTTP $code" >&2; exit 1; }
echo "Installed. No destinations are broadcasting. No firewall rules were changed."
echo "Read credentials locally: sudo cat /etc/rtmp-relay/access.txt"
echo "Open an SSH tunnel: ssh -N -L 8080:127.0.0.1:8080 YOUR_SSH_USER@YOUR_SERVER_IP"
echo "Then visit http://127.0.0.1:8080 . See README for OBS and firewall setup."

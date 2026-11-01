# Ubuntu 26.04 lightweight RTMP relay

One OBS input, three independently controlled RTMP(S) outputs, **no GPU,
transcoding, resizing, placeholder, recording or preview**. MediaMTX v1.21.3
handles ingest. One FFmpeg stream-copy process per enabled destination handles
egress. The dashboard uses Ubuntu's system Python 3 and only the standard library.
One additional local FFmpeg stream-copy monitor measures inbound health even
when every destination is stopped; its remuxed output is discarded, not recorded.

## Compatibility comes first

Use **H.264 video and AAC audio** in OBS. Both tracks are required. Start with
SDR/8-bit and a two-second keyframe interval. The RTX 5060 Ti can do the encoding
in OBS; the Ubuntu server does not need a GPU. This package deliberately does
not support AV1/HEVC passthrough because the shared destination profile is
H.264/AAC.

**Every destination gets exactly the same encoded resolution, frame rate and
bitrate.** If Facebook or X rejects your 4K60 settings, this relay cannot fix it.
Verify each account's ingest limits; disable incompatible destinations or
change OBS to a common supported profile. YouTube supports 4K60 but puts 4K
broadcasts in normal-latency mode. See
[YouTube's encoder requirements](https://support.google.com/youtube/answer/2853702?hl=en).
Do not treat a successful local relay test as proof of platform compatibility.

For example, 40 Mbps to each of three outputs needs about 120 Mbps of server
egress, plus audio/protocol overhead and capacity headroom. OBS still uploads
only one feed. There is no zero-latency guarantee. CPU/RAM requirements depend
on bitrate, encryption and hardware; benchmark your actual workload.

## Install on a fresh Ubuntu 26.04 LTS server

Supports x86_64 and ARM64. Requires sudo, outbound HTTPS access to Ubuntu package
repositories/GitHub, and free TCP ports 1935, 8554 and 8080. Do not install on a
machine where these ports are already used. The installer refuses to overwrite
an existing installation and does not alter firewall rules.

You can also clone the source repository instead of downloading a ZIP:

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends git
git clone https://github.com/theaustinayers/EasyRestream.git
cd EasyRestream
sudo bash install.sh
```

1. Copy the ZIP to the server and extract it:

   ```bash
   sudo apt-get update
   sudo apt-get install --no-install-recommends unzip
   unzip rtmp-relay-ubuntu26.zip
   cd rtmp-relay
   sudo bash install.sh
   ```

2. Read the generated credentials **locally**, not in shared logs:

   ```bash
   sudo cat /etc/rtmp-relay/access.txt
   ```

3. From your workstation, open an SSH tunnel:

   ```bash
   ssh -N -L 8080:127.0.0.1:8080 YOUR_SSH_USER@YOUR_SERVER_IP
   ```

4. Visit `http://127.0.0.1:8080`. Log in as `admin` with the generated password.
   Use local port 8080 (the dashboard validates Host and Origin). Dashboard and
   RTSP are loopback-only. Do not expose port 8080 through a public proxy without
   separately designing TLS, authentication and matching origin checks.

5. In OBS, select a Custom streaming service. Use the server URL and stream key
   from the dashboard's **OBS ingest** panel. First set the OBS server URL to
   `rtmp://YOUR_SERVER_IP:1935/ingest` using your reachable server hostname/IP,
   enter your current dashboard password, and save. The key/full URL are
   masked by default; click **Show key and URL** to reveal them.
   Do not add the stream key to the server field:

   ```text
   Server:     rtmp://YOUR_SERVER_IP:1935/ingest
   Stream key: current key displayed on the dashboard
   ```

   The secret path is the ingest credential, not a public stream name. Only
   that path accepts publishing, and an existing publisher cannot be replaced.

6. Permit inbound TCP 1935 **only from your OBS machine's public IP**, in both
   the server firewall and cloud firewall. Preserve SSH access. If UFW is
   already active, an example is:

   ```bash
   sudo ufw allow from YOUR_OBS_PUBLIC_IP to any port 1935 proto tcp
   ```

   Do not enable/reset UFW blindly on a remote machine. For dynamic source IPs,
   manage the allowlist or use a VPN. Ingest RTMP is unencrypted: use a trusted
   private network/VPN for confidentiality. A leaked ingest key must be rotated.

7. Start OBS. In each destination card, save the **full platform RTMPS URL
   including its stream key**, then click Start. RTMP is also accepted if the
   platform requires it, but RTMPS is recommended. Destination keys are stored
   in a mode-600 file and never returned by the dashboard API.

## Controls and failure behavior

- **Start** connects one destination; it does not start OBS or create a platform
  broadcast/event. Complete any platform-specific go-live steps separately.
- **Stop** disconnects only that destination. There is no pause button: without
  a placeholder, a genuine continuous pause is not possible.
- **Save/Clear URL** is available only while the destination is stopped.
- **Forwarding** means FFmpeg reports increasing output timestamps. It does
  not confirm viewers can watch or that the platform accepts the profile.
- **Stalled** means no output timestamp progress for over five seconds.
- A source failure or destination failure causes retries at 1, 2, 4, 8, 16,
  then 30 seconds. A connection healthy for over 30 seconds resets the backoff.
  Network I/O timeout is ten seconds; detection is not instantaneous.
- An ingest restart or OBS reconnect requires FFmpeg to reconnect. There is no
  buffering of a complete outage, no placeholder and no guarantee the platform
  keeps the original broadcast alive. Source problems may affect all outputs;
  destination problems have independent processes and retry schedules.
- Destination URLs persist; **start/stop intent does not**. After a reboot or
  dashboard service restart all outputs are stopped to prevent accidental
  broadcasts. Manually start the desired destinations again.

## Operations

### Dashboard account and ingest settings

There is one dashboard account, initially `admin` with the install-generated
password. Open **Dashboard account** to change its username, password, or both.
Confirm your current password; leave the new-password fields blank to change
only the username. Usernames allow 1-64 letters, digits, dots, underscores and
hyphens. Passwords require 16-256 characters and matching confirmation.
The backend stores a salted PBKDF2-SHA256 hash, not your dashboard password.
Existing plaintext dashboard configurations migrate automatically on startup.
Old credentials stop working immediately after a successful change. The open
page switches to the new credentials; future visits/browser reloads may prompt
for the new login. Active streams are not restarted by account changes.

The **OBS ingest** panel displays the server address, masked current key and
masked full ingest URL. Set a reachable hostname/IP; the loopback dashboard
address reached through SSH is not the public ingest address. Changing this
displayed address does not configure DNS, ports, TLS or firewall rules.
Use the fixed `rtmp://HOST:1935/ingest` format.

To choose a new ingest key, **stop OBS and all outputs**, then enter a new
16-128-character key (letters, digits, underscores and hyphens) and confirm
your current dashboard password. Leave the new-key field blank to keep the key.
The dashboard updates MediaMTX's exact ingest path and permissions and its
own source configuration; MediaMTX hot-reloads the file. Reconfigure OBS
before reconnecting, then manually start the desired destinations.
Key changes are rejected while outputs are active or the monitor sees live/
stalled input. Always stop OBS first, including during connection retries.
Custom-edited MediaMTX path layouts must be updated manually instead.

The root-only `access.txt` is an **initial installation snapshot**, not a live
credential record. After account/key changes, its old values are obsolete;
use the dashboard and store updated credentials in your password manager.
Authenticated `/api/ingest` responses contain the ingest secret, whereas
routine telemetry never includes it or the password hash.

If you forget your dashboard password, stop the dashboard service and reset
the account locally as root, without putting passwords into shell history:

```bash
sudo systemctl stop rtmp-relay
sudo python3 - <<'PY'
import getpass
import json
from pathlib import Path
import sys
sys.path.insert(0, "/opt/rtmp-relay")
from relay import hash_password, save_config, validate_username
path = Path("/etc/rtmp-relay/config.json")
data = json.loads(path.read_text())
with open("/dev/tty", "r+") as terminal:
    terminal.write("Username: ")
    terminal.flush()
    data["dashboard_username"] = validate_username(terminal.readline().strip())
password = getpass.getpass("New password: ")
if password != getpass.getpass("Confirm password: "):
    raise ValueError("Passwords do not match")
data["dashboard_password_hash"] = hash_password(password)
data.pop("dashboard_password", None)
save_config(path, data)
PY
sudo chown rtmp-relay:rtmp-relay /etc/rtmp-relay/config.json
sudo systemctl start rtmp-relay
```

Because this reset recreates the configuration as root, restoring its service
ownership is required. Restarting the service leaves all destinations stopped.

### Live telemetry

The dashboard refreshes approximately once per second. Inbound and each output
show five-second rolling measurements:

- **Payload bandwidth (Mbps):** FLV byte-counter change per wall-clock second.
- **Encoded bitrate (Mbps):** FLV byte-counter change per second of media time.
  Includes audio and FLV mux overhead; not video-only bitrate.
- **Delivery FPS:** copied video-frame counter change per wall-clock second.
  This drops when delivery slows or stalls.
- **Media FPS:** copied video-frame counter change per second of media time.
  This describes the stream timing, not how quickly it is delivered.
- **Payload transferred (decimal GB):** cumulative local mux bytes during the
  current monitoring/start session, including bytes from reconnect attempts.
  A manual Start or process restart resets the corresponding counter.
- **Last data age**, current connection state, retries and redacted errors.

These are **local encoded-payload estimates, not exact NIC/wire bandwidth**:
RTMP headers, TCP/IP overhead, TLS, retransmissions and remote acknowledgements
are not measured. Inbound is measured by remuxing a local read of the OBS stream
to the discard device; it can lag ingest during connection/probing and is not
an OBS upload socket counter. No decoding or re-encoding is added.

Initial/reconnecting rate values show "Measuring..." until counters exist.
After a stall or stop, delivery bandwidth/FPS show zero and media rates are
unavailable rather than stale. A dashboard connection failure explicitly marks
displayed values as stale. Short startup bursts can temporarily exceed 60
delivery FPS; smoothing and media FPS help distinguish bursts from stream timing.
No pixel analysis is done, so a frozen picture with advancing timestamps is
not detected. Platform playback/ingest acceptance, OBS encoder dropped frames,
and end-to-end latency still require platform/OBS monitoring.

### Updating an existing installation

To apply source/dashboard changes without replacing saved keys or configuration:

```bash
cd EasyRestream
git pull --ff-only
sudo systemctl stop rtmp-relay
sudo install -m 644 relay.py /opt/rtmp-relay/relay.py
sudo install -m 644 web/index.html web/app.js web/style.css /opt/rtmp-relay/web/
sudo systemctl start rtmp-relay
```

Refresh the browser and manually restart the desired destinations. The installer
is for fresh Ubuntu 26.04 systems, not OS upgrades; do not rerun it over an
existing installation.

### Uninstall and clean reinstall

**This stops every outgoing stream and resets the dashboard account, ingest
key and saved destination settings.** The commands preserve a private backup
of the old configuration. They leave shared packages, firewall rules, your
Git clone and the dedicated service account intact.

#### 1. Uninstall on the Ubuntu server

For an existing installation with both service units installed:

```bash
sudo bash <<'SH'
set -e

if [ -d /etc/rtmp-relay ]; then
  install -d -m 700 /root/easyrestream-backups
  cp -a /etc/rtmp-relay \
    "/root/easyrestream-backups/config-$(date +%Y%m%d-%H%M%S)"
fi

systemctl disable --now rtmp-relay.service rtmp-relay-media.service

rm -f /etc/systemd/system/rtmp-relay.service
rm -f /etc/systemd/system/rtmp-relay-media.service
rm -f /usr/local/bin/rtmp-relay-mediamtx
rm -rf -- /opt/rtmp-relay
rm -rf -- /etc/rtmp-relay

systemctl daemon-reload
SH
```

The removal commands target only EasyRestream's installed components. Do not
substitute another application's directories. If service shutdown fails, the
script stops before deleting files; inspect the error rather than bypassing it.
Treat configuration backups as sensitive because they contain stream keys.

#### 2. Clean install from the existing Git clone

From the directory containing your `EasyRestream` clone:

```bash
(
  set -e
  cd EasyRestream
  git pull --ff-only
  sudo bash install.sh
)
```

The installer requires **Ubuntu 26.04 LTS**. If Git reports conflicting local
changes, stop rather than resetting or overwriting them.

#### 3. Sign in and reconfigure OBS

On the server, read the newly generated credentials:

```bash
sudo cat /etc/rtmp-relay/access.txt
```

On your workstation, open the SSH tunnel:

```bash
ssh -N -L 8080:127.0.0.1:8080 YOUR_SSH_USER@YOUR_SERVER_IP
```

Open `http://127.0.0.1:8080`, hard-refresh with `Ctrl+Shift+R`, and sign in with
the new credentials. Then:

1. Set the **OBS ingest server URL** to your server's reachable hostname/IP.
2. Update OBS with the **new ingest key** shown on the dashboard.
3. Re-enter the destination URLs and stream keys.
4. Start OBS, then click **Start** for each desired destination.

Do not restore the old configuration backup if your intent is a clean reset.

### Service diagnostics

```bash
sudo systemctl status rtmp-relay-media rtmp-relay
sudo journalctl -u rtmp-relay-media -u rtmp-relay --since "10 minutes ago"
sudo systemctl restart rtmp-relay
```

FFmpeg output errors shown by the dashboard are URL/key-redacted. FFmpeg still
needs the destination URL in its process arguments; privileged users, and
processes running as the dedicated service user, may inspect those arguments.
Do not grant untrusted users access to the service account. MediaMTX may log
ingest paths on errors, so treat its journal as sensitive.

Configuration is in `/etc/rtmp-relay/config.json`. Do not edit it while the
dashboard service is running; dashboard saves are atomic and could replace
manual edits. Prefer the dashboard for account and ingest key changes. For a
manual key recovery, stop both services, replace the key in both configurations,
preserve ownership/mode 600, update OBS, and restart both services.

The service account cannot edit installed program files. The dashboard needs
write access to its private configuration directory for atomic saves.
Keep trusted backups of configuration. There is no built-in updater: review
new MediaMTX releases, verify checksums, stop services and replace only the
appropriate binary/code files. Do not rerun the installer as an upgrade.

If installation fails, inspect the reported error and service logs before
retrying. Partial installs are intentionally not silently rolled back or
overwritten. Installed components are `/opt/rtmp-relay`, the dedicated
`/usr/local/bin/rtmp-relay-mediamtx`, `/etc/rtmp-relay`, and the two
`rtmp-relay*.service` units. Do not remove unrelated MediaMTX installations.

## Verification

Run the included standard-library tests from this directory:

```bash
python3 -m unittest discover -s tests -v
```

Before a real broadcast, test the exact OBS 4K60 settings for an extended period
to each real platform. Watch OBS dropped frames, server CPU/memory/network
usage, FFmpeg errors, and platform stream health. Test individual stops, a
destination rejection, an OBS disconnect/reconnect, and a server restart.
The included integration test uses a local MediaMTX executable and FFmpeg:

```bash
python3 tests/integration.py --mediamtx /usr/local/bin/rtmp-relay-mediamtx
```

This is a local fanout/control/reconnect check, not a platform load benchmark.
Ubuntu installation/systemd and real-platform behavior require verification
on the target machine; this package is not a claim of zero lag or certified
Facebook/X 4K60 support.

MediaMTX and FFmpeg are downloaded/installed from their upstream sources, not
bundled. See [MediaMTX](https://github.com/bluenviron/mediamtx) and
[FFmpeg](https://ffmpeg.org/legal.html) for their licenses.

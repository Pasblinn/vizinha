# yoosee-nvr

**Stop paying for camera cloud storage. Record locally, back up to a drive you already own.**

Cheap Wi-Fi cameras — the ~US$15 Yoosee/HWN-AP03 class sold on Shopee, AliExpress
and Amazon — are sold at a loss and monetised through a cloud subscription. Without
it you get live view and nothing else: no continuous recording, no history you can
scroll back through, no footage after the SD card is stolen along with the camera.

`yoosee-nvr` replaces that subscription with software. It records continuously to
your own disk, mirrors each finished clip to your own Google Drive (or any other
[rclone](https://rclone.org) remote), rotates old footage before the disk fills up,
and shows the whole thing on a small web dashboard. The recurring cost is whatever
you already pay for storage — for most people, zero.

```
  camera (RTSP)  ->  proxy  ->  ffmpeg  ->  local disk  ->  rclone  ->  your cloud
                       |                        |
                  sound level             rotation by age
                       |                     and by size
                       +-------->  web dashboard  <-------+
```

---

## Why a proxy sits in the middle

This is the part that makes cheap cameras hard, and the reason `ffmpeg -i rtsp://...`
alone does not work on them.

Ask an HWN-AP03 to stream over TCP and it answers:

```
Transport: RTP/AVP;unicast;client_port=...
```

`RTP/AVP` means UDP. So ffmpeg waits for UDP packets — while the camera happily
sends the interleaved TCP stream that ffmpeg is no longer listening for. Connection
established, zero bytes written, eventual timeout.

The camera is not lying about its capabilities, only about the header. So
[`recorder.py`](recorder.py) puts a small proxy between the two: it forwards every
byte in both directions untouched, except that one header, which it rewrites to
`RTP/AVP/TCP;`. ffmpeg then reads the stream that was already being sent to it.

Audio rides along on the same connection (interleaved channel 2), so the sound level
meter decodes it from packets already in flight rather than opening a second stream
the camera cannot afford.

---

## What you get

| | |
|---|---|
| **Continuous recording** | Segmented files (10 min by default), stream-copied — no re-encoding, so it runs on a Raspberry Pi |
| **Scheduled windows** | Record 21:00–07:00, business hours, or around the clock |
| **Cloud backup** | Every finished segment pushed to Google Drive, S3, Backblaze, Dropbox, SFTP — anything rclone speaks |
| **Storage rotation** | Deletes by age *and* by free space; never deletes footage that has not been backed up yet |
| **Sound level meter** | Live dB estimate with history, extracted from the stream already being recorded |
| **Web dashboard** | Status, live meter, storage, backup queue, recent recordings |
| **Hardening script** | Firewall, TLS certificate, file permissions, SSH — all optional, all with `--dry-run` |

---

## Requirements

- Python 3.9+
- `ffmpeg` — recording and audio decoding
- `rclone` — cloud backup (optional; skip it to record locally only)
- An RTSP-capable camera on your LAN

Tested on Ubuntu 24.04 with a Yoosee HWN-AP03. Any RTSP camera should work; the
Transport-header workaround is harmless for cameras that do not need it.

---

## Quick start

```bash
git clone https://github.com/Pasblinn/yoosee-nvr.git
cd yoosee-nvr

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
chmod 600 .env
$EDITOR .env          # camera IP, camera password, dashboard password
```

Check that the camera answers and the credentials are right:

```bash
python3 rtsp_probe.py --describe
```

Record 30 seconds and stop, to confirm footage actually lands on disk:

```bash
python3 start.py --test
```

Then run it:

```bash
python3 start.py
```

The dashboard is at <http://127.0.0.1:9847>.

### Cloud backup

```bash
rclone config          # create a remote named "gdrive" (or set RCLONE_REMOTE)
python3 start.py --setup
```

`--setup` verifies the remote exists and writes a probe file to prove it has write
access, so a broken backup surfaces now instead of the night you need the footage.

Recordings land in `<remote>:<CLOUD_FOLDER_NAME>/<date>/`. rclone verifies the
checksum after each transfer, and only then is the file marked as uploaded.

To let the cloud be the real archive and keep the local disk small, set
`CLOUD_DELETE_LOCAL_AFTER_UPLOAD=true`.

---

## Configuration

Everything lives in `.env` — see [`.env.example`](.env.example), which documents each
setting. Real environment variables override the file, so systemd or Docker can
supply secrets without editing anything.

The settings people change first:

| Variable | Default | What it does |
|---|---|---|
| `CAMERA_IP` | *required* | Camera address. Give it a DHCP reservation |
| `CAMERA_PASS` | *required* | Password set in the Yoosee app |
| `RECORDING_START_HOUR` / `RECORDING_END_HOUR` | `21` / `7` | Recording window; start > end crosses midnight; equal values record 24/7 |
| `SEGMENT_DURATION` | `600` | Seconds per file |
| `MAX_STORAGE_GB` / `RETENTION_DAYS` | `50` / `7` | When rotation kicks in |
| `CLOUD_DELETE_LOCAL_AFTER_UPLOAD` | `false` | Free local space once backed up |
| `WEB_HOST` | `127.0.0.1` | `0.0.0.0` exposes the dashboard to the LAN — read the security section first |

---

## Running as a service

```bash
sudo cp systemd/yoosee-nvr.service /etc/systemd/system/
sudo $EDITOR /etc/systemd/system/yoosee-nvr.service   # replace CHANGE_ME
sudo systemctl daemon-reload
sudo systemctl enable --now yoosee-nvr
journalctl -u yoosee-nvr -f
```

---

## Security

This records the inside of your home and uploads it to your cloud account. Treat it
accordingly.

- **The dashboard binds to `127.0.0.1` by default.** Reach it over SSH
  (`ssh -L 9847:127.0.0.1:9847 user@host`) or a VPN. Set `WEB_HOST=0.0.0.0` only
  behind the firewall rules below, and never expose it to the internet.
- **Hash the dashboard password**: `python3 scripts/hash_password.py`, then put the
  result in `DASHBOARD_PASS_HASH` and delete `DASHBOARD_PASS`.
- **Harden the host** (optional, reversible, and it shows you the plan first):

  ```bash
  sudo bash scripts/harden.sh --dry-run
  sudo bash scripts/harden.sh
  ```

  Firewall limited to your subnet, self-signed TLS for the dashboard, `600` on every
  secret, SSH root login disabled. The firewall step sets the INPUT policy to `DROP`
  and asks you to retype the subnet first — get it wrong over SSH and you lock
  yourself out.
- **Never commit `.env`, `certs/`, `rclone.conf` or footage.** They are all in
  `.gitignore`; the rclone config holds an OAuth refresh token for your Drive.

Details and the reporting process: [SECURITY.md](SECURITY.md).

---

## Troubleshooting

**Nothing is recorded, no obvious error.** Run `python3 rtsp_probe.py --describe`. It
prints each RTSP step and the camera's SDP:

- `OPTIONS` fails → wrong IP, or not an RTSP camera.
- `DESCRIBE: 401` → wrong `CAMERA_USER` / `CAMERA_PASS`.
- A warning that the camera answered UDP for a TCP request → normal for this camera
  family; that is exactly what the proxy fixes.

**Recording stops overnight.** Usually the camera got a new DHCP lease. Give it a
static reservation on the router.

**The dB reading looks wrong.** It is an uncalibrated estimate — these microphones
report whatever gain their firmware applied. Adjust `DECIBEL_MIC_OFFSET` until a
quiet room reads 30–40 dB, and treat the numbers as relative.

**`start.py` exits with a configuration error.** That is deliberate: a missing
`CAMERA_IP` or password stops the process instead of starting a recorder that
silently records nothing.

---

## Project layout

| File | Role |
|---|---|
| [`start.py`](start.py) | Entry point; starts rotation, backup, scheduler, dashboard |
| [`recorder.py`](recorder.py) | RTSP proxy + ffmpeg segment recording |
| [`scheduler.py`](scheduler.py) | Decides when the recording window is open |
| [`cloud_uploader.py`](cloud_uploader.py) | rclone backup with an upload ledger |
| [`storage_manager.py`](storage_manager.py) | Rotation by age and by free space |
| [`audio_level.py`](audio_level.py) | A-law/PCM decoding and dB math |
| [`decibel_meter.py`](decibel_meter.py) | Standalone meter, when not recording |
| [`dashboard.py`](dashboard.py) | Flask app: auth, APIs, pages |
| [`rtsp_probe.py`](rtsp_probe.py) | Diagnostic RTSP client |
| [`config.py`](config.py) | Configuration, validated at import |

---

## Contributing

Bug reports and pull requests are welcome — especially reports from other camera
models, which is how the compatibility list grows. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)

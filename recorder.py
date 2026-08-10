"""
Continuous recorder for Yoosee-family cameras (HWN-AP03 and relatives).

The problem this module solves
------------------------------
These cameras answer an RTSP SETUP with `Transport: RTP/AVP;unicast;...` even
when the client asked for TCP interleaving, and then proceed to send the
interleaved stream anyway. ffmpeg believes the header, waits for UDP packets
that never arrive, and times out.

So we sit a small proxy between ffmpeg and the camera: it forwards both
directions verbatim except for that one header, which it rewrites to
`RTP/AVP/TCP;`. ffmpeg then reads the stream it was already being sent.

Riding along on the same connection, audio packets (interleaved channel 2) are
decoded to measure sound level without opening a second stream to the camera.
"""

import logging
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import audio_level
import config

log = logging.getLogger(__name__)

# Shared with the dashboard.
levels = audio_level.LevelTracker()
recorder_status = {"recording": False, "started": "", "packets": 0, "bytes": 0}

# RTSP interleaved framing: '$' marks a data frame.
INTERLEAVED_MAGIC = 0x24
AUDIO_CHANNEL = 2


def get_db_reading():
    """Latest sound level reading (dashboard API)."""
    return levels.current()


def get_db_history(seconds=300):
    """Sound level history for the last N seconds (dashboard API)."""
    return levels.history(seconds)


def process_audio_db(rtp_payload):
    """Decode one A-law RTP packet and record its level."""
    if len(rtp_payload) < 14:
        return

    # Skip the RTP header: 12 fixed bytes + 4 per CSRC.
    header_size = 12 + (rtp_payload[0] & 0x0F) * 4
    # Bit 0x10 marks a header extension, whose length is in 32-bit words.
    if rtp_payload[0] & 0x10 and len(rtp_payload) > header_size + 4:
        ext_words = struct.unpack(">H", rtp_payload[header_size + 2 : header_size + 4])[0]
        header_size += 4 + ext_words * 4

    audio = rtp_payload[header_size:]
    if len(audio) < 10:
        return

    db = audio_level.samples_to_db(audio_level.alaw_to_linear(audio))
    levels.record(db)

    if levels.is_alert(db):
        log.warning(
            "Sound level alert: %s dB (threshold %s)", db, config.DECIBEL_ALERT_THRESHOLD
        )


class RTSPProxy:
    """
    Local RTSP proxy that fixes the camera's Transport header.

    Listens on PROXY_HOST:PROXY_PORT, opens one upstream connection to the
    camera per client, and relays bytes both ways.
    """

    def __init__(self, listen_host=None, listen_port=None):
        self.listen_host = listen_host or config.PROXY_HOST
        self.listen_port = listen_port or config.PROXY_PORT
        self.running = False

    def _connect_camera(self):
        """Open the upstream TCP connection to the camera."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect((config.CAMERA_IP, config.CAMERA_PORT))
        log.info("Proxy: connected to camera %s:%s", config.CAMERA_IP, config.CAMERA_PORT)
        return s

    def _relay(self, client_sock, camera_sock):
        """Relay traffic between ffmpeg and the camera until either side drops."""
        client_sock.settimeout(1)
        camera_sock.settimeout(1)

        last_db_time = time.time()
        packets = 0
        total_bytes = 0

        while self.running:
            # Camera -> ffmpeg: media data and RTSP responses.
            try:
                data = camera_sock.recv(65536)
                if not data:
                    log.warning("Proxy: camera closed the connection")
                    break

                packets += 1
                total_bytes += len(data)
                recorder_status["packets"] = packets
                recorder_status["bytes"] = total_bytes

                if time.time() - last_db_time >= config.DECIBEL_INTERVAL:
                    self._extract_audio_for_db(data)
                    last_db_time = time.time()

                # The one-line fix this whole proxy exists for.
                if b"Transport:" in data and b"RTP/AVP;" in data:
                    data = data.replace(b"RTP/AVP;", b"RTP/AVP/TCP;")

                try:
                    client_sock.sendall(data)
                except (BrokenPipeError, ConnectionResetError):
                    log.warning("Proxy: ffmpeg disconnected")
                    break

            except socket.timeout:
                pass
            except OSError as exc:
                log.error("Proxy camera->client error: %s", exc)
                break

            # ffmpeg -> camera: RTSP requests.
            try:
                request = client_sock.recv(4096)
                if request:
                    camera_sock.sendall(request)
            except socket.timeout:
                pass
            except OSError:
                break

            if packets and packets % 2000 == 0:
                reading = levels.current()
                log.info(
                    "Proxy: %d packets, %.1f MB | dB %.1f (peak %.1f, avg %.1f)",
                    packets,
                    total_bytes / 1024 / 1024,
                    reading["value"],
                    reading["peak"],
                    reading["avg"],
                )

    def _extract_audio_for_db(self, data):
        """Walk the interleaved frames in a buffer and measure the audio ones."""
        pos = 0
        while pos < len(data) - 4:
            if data[pos] != INTERLEAVED_MAGIC:
                pos += 1
                continue

            channel = data[pos + 1]
            length = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
            if pos + 4 + length > len(data):
                break  # frame spans the next read; skip it rather than misparse

            if channel == AUDIO_CHANNEL:
                process_audio_db(data[pos + 4 : pos + 4 + length])
            pos += 4 + length

    def start(self):
        """Accept clients and relay, until stop() is called."""
        self.running = True
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.settimeout(2)
        server.bind((self.listen_host, self.listen_port))
        server.listen(1)
        log.info("RTSP proxy listening on %s:%s", self.listen_host, self.listen_port)

        while self.running:
            camera_sock = None
            client_sock = None
            try:
                client_sock, addr = server.accept()
                log.info("Proxy: ffmpeg connected from %s:%s", *addr)
                camera_sock = self._connect_camera()
                self._relay(client_sock, camera_sock)
            except socket.timeout:
                continue
            except OSError as exc:
                log.error("Proxy error: %s", exc)
                time.sleep(2)
            finally:
                for sock in (client_sock, camera_sock):
                    if sock is not None:
                        try:
                            sock.close()
                        except OSError:
                            pass

        server.close()

    def stop(self):
        self.running = False


class CameraRecorder:
    """Runs the proxy plus an ffmpeg process that writes segmented files."""

    def __init__(self):
        self.proxy = RTSPProxy()
        self.ffmpeg_proc = None
        self.running = False

    def _start_ffmpeg(self):
        """Spawn ffmpeg reading from the proxy and writing dated segments."""
        rec_dir = Path(config.RECORDING_DIR) / datetime.now().strftime("%Y-%m-%d")
        rec_dir.mkdir(parents=True, exist_ok=True)

        output_pattern = str(rec_dir / f"cam_%H-%M-%S.{config.CONTAINER_FORMAT}")

        cmd = [
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-i", config.proxy_rtsp_url(),
            "-c:v", "copy",           # no re-encode: the Pi cannot afford it
            "-c:a", "aac",            # A-law is not valid in an mp4 container
            "-ar", "16000",
            "-f", "segment",
            "-segment_time", str(config.SEGMENT_DURATION),
            "-segment_format", config.CONTAINER_FORMAT,
            "-strftime", "1",
            "-reset_timestamps", "1",
            "-y",
            output_pattern,
        ]

        log.info(
            "ffmpeg: %ss segments into %s (source %s)",
            config.SEGMENT_DURATION,
            rec_dir,
            config.proxy_rtsp_url(redacted=True),
        )
        self.ffmpeg_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        return self.ffmpeg_proc

    def record(self):
        """Record until stop() is called, restarting ffmpeg when it dies."""
        self.running = True
        recorder_status["recording"] = True
        recorder_status["started"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        proxy_thread = threading.Thread(
            target=self.proxy.start, daemon=True, name="rtsp-proxy"
        )
        proxy_thread.start()
        time.sleep(1)  # let the listener bind before ffmpeg dials in

        while self.running:
            try:
                proc = self._start_ffmpeg()
                log.info("ffmpeg started (pid %s)", proc.pid)

                while self.running:
                    if proc.poll() is not None:
                        stderr = (
                            proc.stderr.read().decode(errors="replace")
                            if proc.stderr
                            else ""
                        )
                        if proc.returncode != 0:
                            log.error(
                                "ffmpeg exited with %s: %s",
                                proc.returncode,
                                stderr.strip()[-300:],
                            )
                        break
                    time.sleep(1)

            except OSError as exc:
                log.error("Recording error: %s", exc)

            if self.running:
                log.info("Restarting ffmpeg in 5s...")
                time.sleep(5)

        recorder_status["recording"] = False

    def stop(self):
        """Stop ffmpeg and the proxy."""
        self.running = False
        self.proxy.stop()
        if self.ffmpeg_proc:
            try:
                # SIGINT lets ffmpeg finalize the current segment; a killed
                # ffmpeg leaves an mp4 without its moov atom, i.e. unplayable.
                self.ffmpeg_proc.send_signal(signal.SIGINT)
                self.ffmpeg_proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                self.ffmpeg_proc.kill()


_recorder = None


def get_recorder():
    """Process-wide recorder instance."""
    global _recorder
    if _recorder is None:
        _recorder = CameraRecorder()
    return _recorder


def start_recording_thread():
    """Start recording in a background thread. Returns the thread."""
    t = threading.Thread(target=get_recorder().record, daemon=True, name="recorder")
    t.start()
    return t


def stop_recording():
    """Stop the running recorder, if any."""
    if _recorder:
        _recorder.stop()


def main():
    """Run the recorder on its own, without the scheduler or dashboard."""
    import log_setup

    log_setup.configure()
    recorder = CameraRecorder()

    def cleanup(signum=None, frame=None):
        log.info("Shutting down...")
        recorder.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    log.info("=" * 50)
    log.info("Recorder starting")
    log.info("Camera:     %s:%s", config.CAMERA_IP, config.CAMERA_PORT)
    log.info("Recordings: %s", config.RECORDING_DIR)
    log.info("=" * 50)

    recorder.record()


if __name__ == "__main__":
    main()

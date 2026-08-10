"""
Standalone RTSP client, used to diagnose a camera before trusting the recorder.

When footage is not being written, the question is always "is it the camera,
the credentials, or ffmpeg?". This module speaks RTSP directly - OPTIONS,
DESCRIBE with digest auth, SETUP, PLAY - and prints exactly what the camera
answered, which separates the three cases in one command:

    python3 rtsp_probe.py            # 10-second capture to a temp file
    python3 rtsp_probe.py --describe # handshake only, print the SDP

Nothing in the recording pipeline imports this module; it is a debugging tool.
"""

import argparse
import hashlib
import logging
import re
import signal
import socket
import struct
import sys
import time
from pathlib import Path

import config

log = logging.getLogger(__name__)

INTERLEAVED_MAGIC = 0x24


class RTSPClient:
    """Minimal RTSP 1.0 client with digest authentication."""

    def __init__(self, ip=None, port=None, user=None, password=None, path=None):
        self.ip = ip or config.CAMERA_IP
        self.port = port or config.CAMERA_PORT
        self.user = user or config.CAMERA_USER
        self.password = password or config.CAMERA_PASS
        self.path = path or config.CAMERA_PATH
        self.base_url = f"rtsp://{self.ip}:{self.port}{self.path}"
        self.sock = None
        self.cseq = 0
        self.session = None
        self.realm = None
        self.nonce = None
        self.running = False

    # -- transport ---------------------------------------------------------

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect((self.ip, self.port))
        log.info("Connected to %s:%s", self.ip, self.port)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def _recv_exactly(self, n):
        """Read exactly n bytes, or None if the stream ends or stalls."""
        data = b""
        while len(data) < n:
            try:
                chunk = self.sock.recv(n - len(data))
                if not chunk:
                    return None
                data += chunk
            except socket.timeout:
                return None
        return data

    def _recv_response(self):
        """Read one complete RTSP response, honouring Content-Length."""
        data = b""
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\r\n\r\n" not in data:
                continue

            header_end = data.index(b"\r\n\r\n") + 4
            header = data[:header_end].decode(errors="replace")
            match = re.search(r"Content-Length:\s*(\d+)", header, re.IGNORECASE)
            if not match:
                break
            if len(data) >= header_end + int(match.group(1)):
                break
        return data.decode(errors="replace")

    # -- protocol ----------------------------------------------------------

    def _digest_auth(self, method, uri):
        """Build an RFC 2069 digest Authorization header."""
        # MD5 is what RTSP digest auth specifies; the camera offers nothing else.
        ha1 = hashlib.md5(
            f"{self.user}:{self.realm}:{self.password}".encode()
        ).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        response = hashlib.md5(f"{ha1}:{self.nonce}:{ha2}".encode()).hexdigest()
        return (
            f'Digest username="{self.user}", realm="{self.realm}", '
            f'nonce="{self.nonce}", uri="{uri}", response="{response}"'
        )

    def _request(self, method, url=None, extra_headers=None, auth=True):
        url = url or self.base_url
        self.cseq += 1

        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self.cseq}", "User-Agent: yoosee-nvr/1.0"]
        if self.session:
            lines.append(f"Session: {self.session}")
        if auth and self.realm and self.nonce:
            lines.append(f"Authorization: {self._digest_auth(method, url)}")
        for key, value in (extra_headers or {}).items():
            lines.append(f"{key}: {value}")

        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        return self._recv_response()

    def options(self):
        """Send OPTIONS. Returns True when the camera speaks RTSP at all."""
        response = self._request("OPTIONS", auth=False)
        status = response.split("\r\n")[0]
        log.info("OPTIONS: %s", status)
        return "200" in status

    def describe(self):
        """Send DESCRIBE, authenticating if challenged. Returns the SDP."""
        response = self._request(
            "DESCRIBE", extra_headers={"Accept": "application/sdp"}, auth=False
        )

        if "401" in response.split("\r\n")[0]:
            realm = re.search(r'realm="([^"]+)"', response)
            nonce = re.search(r'nonce="([^"]+)"', response)
            if not (realm and nonce):
                raise RuntimeError(
                    "Camera demanded authentication but sent no digest challenge"
                )
            self.realm, self.nonce = realm.group(1), nonce.group(1)
            log.info("Authenticating (realm=%s)", self.realm)
            response = self._request(
                "DESCRIBE", extra_headers={"Accept": "application/sdp"}
            )

        status = response.split("\r\n")[0]
        log.info("DESCRIBE: %s", status)
        if "401" in status:
            raise RuntimeError(
                "Authentication failed - check CAMERA_USER and CAMERA_PASS"
            )
        if "200" not in status:
            raise RuntimeError(f"DESCRIBE failed: {status}")

        sdp_start = response.find("\r\n\r\n")
        return response[sdp_start + 4 :] if sdp_start >= 0 else ""

    def setup(self, track, interleaved_start):
        """SETUP one track over TCP interleaving."""
        transport = (
            f"RTP/AVP/TCP;unicast;"
            f"interleaved={interleaved_start}-{interleaved_start + 1}"
        )
        response = self._request(
            "SETUP",
            url=f"{self.base_url}/{track}",
            extra_headers={"Transport": transport},
        )

        status = response.split("\r\n")[0]
        log.info("SETUP %s: %s", track, status)

        session = re.search(r"Session:\s*([^\s;]+)", response)
        if session:
            self.session = session.group(1).strip()

        transport_header = re.search(r"Transport:\s*(.+?)(?:\r\n|$)", response)
        if transport_header:
            answered = transport_header.group(1)
            log.info("Transport answered: %s", answered)
            if "RTP/AVP;" in answered:
                # This is the camera bug the recorder's proxy exists to patch.
                log.warning(
                    "Camera answered UDP transport for a TCP request - "
                    "this is the malformed header recorder.py rewrites"
                )
        return "200" in status

    def play(self):
        """PLAY the negotiated session."""
        response = self._request("PLAY", extra_headers={"Range": "npt=0.000-"})
        status = response.split("\r\n")[0]
        log.info("PLAY: %s", status)
        return "200" in status

    def teardown(self):
        try:
            self._request("TEARDOWN")
        except OSError:
            pass
        self.close()

    def read_interleaved(self):
        """Yield (channel, payload) for each interleaved frame."""
        while self.running:
            try:
                header = self._recv_exactly(1)
                if not header:
                    break

                if header[0] != INTERLEAVED_MAGIC:
                    # An RTSP response (keepalive answer) rather than media.
                    continue

                info = self._recv_exactly(3)
                if not info:
                    break
                channel = info[0]
                length = struct.unpack(">H", info[1:3])[0]

                payload = self._recv_exactly(length)
                if payload:
                    yield channel, payload

            except socket.timeout:
                try:
                    self._request("GET_PARAMETER")  # keepalive
                except OSError:
                    break
            except OSError as exc:
                log.error("Stream read error: %s", exc)
                break

    def handshake(self):
        """connect -> OPTIONS -> DESCRIBE -> SETUP both tracks -> PLAY."""
        self.connect()
        if not self.options():
            raise RuntimeError("OPTIONS failed - is this an RTSP camera?")

        sdp = self.describe()
        log.info("SDP:\n%s", sdp)

        self.setup("track1", 0)  # video
        self.setup("track2", 2)  # audio

        if not self.play():
            raise RuntimeError("PLAY failed")
        return sdp


def capture_raw(output_file=None, duration=10):
    """
    Capture the raw interleaved stream to a file, for a fixed duration.

    Each frame is stored as channel(1) + length(2) + payload, so the dump can
    be replayed or inspected offline.
    """
    if output_file is None:
        output_file = Path(config.BASE_DIR) / f"rtsp_probe_{int(time.time())}.raw"
    output_file = Path(output_file)

    client = RTSPClient()

    def stop(signum=None, frame=None):
        client.running = False
        log.info("Stopping capture...")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    client.handshake()
    client.running = True
    log.info("Capturing to %s for %ss...", output_file, duration)

    packets = 0
    started = time.time()
    try:
        with open(output_file, "wb") as f:
            for channel, payload in client.read_interleaved():
                if duration > 0 and time.time() - started > duration:
                    break
                f.write(struct.pack(">BH", channel, len(payload)))
                f.write(payload)
                packets += 1
                if packets % 100 == 0:
                    log.info("  %d frames, %.0f KB", packets, f.tell() / 1024)
    finally:
        client.teardown()

    log.info(
        "Captured %d frames in %.1fs -> %s", packets, time.time() - started, output_file
    )
    return output_file


def main():
    parser = argparse.ArgumentParser(description="Probe an RTSP camera")
    parser.add_argument(
        "--describe", action="store_true", help="handshake only, print the SDP"
    )
    parser.add_argument(
        "--duration", type=int, default=10, help="capture seconds (default: 10)"
    )
    parser.add_argument("--output", help="where to write the raw capture")
    args = parser.parse_args()

    import log_setup

    log_setup.configure()

    log.info("Probing %s", config.rtsp_url(redacted=True))

    try:
        if args.describe:
            client = RTSPClient()
            client.handshake()
            client.teardown()
            log.info("Handshake OK - the camera is reachable and credentials work")
        else:
            capture_raw(args.output, args.duration)
    except (OSError, RuntimeError) as exc:
        log.error("Probe failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

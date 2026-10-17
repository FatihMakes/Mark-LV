"""
dashboard/server.py — JARVIS Local HTTP Dashboard

Plain HTTP on port 8000 (no SSL warnings, no firewall issues).
Security at the application layer: AES-256-CBC with session-key-derived key.
CryptoJS is auto-downloaded once and served locally — no CDN needed after that.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import re
import secrets
import socket
import string
import time
import os
import json
import contextlib
from pathlib import Path
from urllib.parse import urlsplit
import logging
from core import headless
from core.spotify import default_oauth, SpotifyConfigError

logger = logging.getLogger(__name__)

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

# python-multipart is required for file uploads — optional dependency
_UPLOAD_OK = False
try:
    import python_multipart
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR    = Path(__file__).resolve().parent.parent
STATIC_DIR  = Path(__file__).parent / "static"
PORT        = 8000
MAX_UPLOAD_MB = 500


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()

def _get_gemini_key() -> str | None:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return _json.load(f).get("gemini_api_key")
    except Exception:
        return None

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b'JARVIS-DASHBOARD-v1'


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) → 32-byte AES-256 key (microseconds, no PBKDF2 needed)."""
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw      = base64.b64decode(enc_b64)
    iv, ct   = raw[:16], raw[16:]
    dec      = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')


# ── CryptoJS (auto-download once, served locally) ─────────────────────────────
_CRYPTOJS_CDN  = ("https://cdnjs.cloudflare.com/ajax/libs/"
                  "crypto-js/4.2.0/crypto-js.min.js")
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"


def _ensure_network_access(port: int) -> None:
    """Cross-platform, best-effort: open port in the OS firewall for LAN access.

    Runs in a background thread — never blocks uvicorn startup.

    Windows : writes a .bat file, runs it elevated via Windows ShellExecuteW
              (native UAC dialog, guaranteed to appear). One-time setup.
    macOS   : osascript admin dialog if the Application Firewall is on.
    Linux   : pkexec GUI → sudo -n → prints manual command as fallback.
    """
    import sys, subprocess, os, tempfile, threading

    # ── Windows ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes, time

        port_rule = f"JARVIS Dashboard Port {port}"
        prog_rule  = "JARVIS Dashboard Python"
        py_exe     = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        def _network_is_public() -> bool:
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-NetConnectionProfile | "
                     "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                     "Measure-Object).Count"],
                    capture_output=True, text=True, timeout=6,
                )
                return r.stdout.strip() not in ("", "0")
            except Exception:
                return False

        need_port    = not _netsh_rule_exists(port_rule)
        need_prog    = not _netsh_rule_exists(prog_rule)
        need_private = _network_is_public()

        if not need_port and not need_prog and not need_private:
            return  # already fully configured

        # Build a .bat file — netsh + powershell, runs fast when elevated
        bat_lines = ["@echo off"]
        if need_private:
            bat_lines.append(
                'powershell -NoProfile -NonInteractive -Command "'
                'Get-NetConnectionProfile | '
                "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                'Set-NetConnectionProfile -NetworkCategory Private"'
            )
        if need_port:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{port_rule}" protocol=TCP dir=in '
                f'localport={port} action=allow'
            )
        if need_prog:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="jarvis_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs"))   # Windows cmd.exe expects ANSI
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        # ── Try running directly (succeeds when already admin) ────────────────
        try:
            r = subprocess.run(
                [bat_path], capture_output=True, timeout=8, shell=True
            )
            if r.returncode == 0:
                print(f"[Dashboard] Firewall configured for port {port}.")
                try:
                    os.unlink(bat_path)
                except Exception:
                    pass
                return
        except Exception:
            pass

        # ── ShellExecuteW: native UAC elevation (most reliable on Windows) ────
        # ShellExecuteW with verb "runas" always shows the UAC dialog regardless
        # of UAC level settings. Non-blocking — uvicorn is already running.
        print("[Dashboard] One-time network setup required.")
        print("[Dashboard] >>> A Windows security dialog will appear — click 'Yes' <<<")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,       # hwnd  (no parent window)
                "runas",    # verb  (request elevation)
                bat_path,   # file  (our .bat)
                None,       # params
                None,       # working dir
                0,          # SW_HIDE (run without a visible cmd window)
            )
            if int(ret) > 32:
                # ShellExecuteW returns immediately; bat finishes in ~1 second.
                # Sleep briefly so the rules are in place before the first retry.
                time.sleep(2)
                print(f"[Dashboard] Network setup complete — port {port} is open.")
                print("[Dashboard] Refresh your phone browser to connect.")
            else:
                print("[Dashboard] Setup was not allowed.")
                print("[Dashboard] Phone connections may fail until JARVIS is run as Administrator.")
        except Exception as e:
            print(f"[Dashboard] Firewall setup error: {e}")
        finally:
            # Cleanup after the bat has had time to run
            def _cleanup(path: str) -> None:
                time.sleep(5)
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, args=(bat_path,), daemon=True).start()
        return

    # ── macOS ─────────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        fw_ctl = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        try:
            r = subprocess.run(
                [fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do

            py = sys.executable
            listed = subprocess.run(
                [fw_ctl, "--listapps"], capture_output=True, text=True, timeout=5,
            )
            if py in listed.stdout:
                return  # already allowed

            print("[Dashboard] One-time network setup — enter your password in the macOS dialog.")
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{fw_ctl} --add {py} && {fw_ctl} --unblockapp {py}"'
                 f' with administrator privileges'],
                timeout=60,
            )
        except Exception:
            pass  # macOS firewall is off by default — silent failure is fine
        return

    # ── Linux ─────────────────────────────────────────────────────────────────
    def _privileged(cmd: list[str]) -> bool:
        for prefix in (["pkexec"], ["sudo", "-n"]):
            try:
                r = subprocess.run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try:  # ufw
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            if _privileged(["ufw", "allow", f"{port}/tcp"]):
                print(f"[Dashboard] ufw: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try:  # firewalld
        r = subprocess.run(
            ["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5,
        )
        if "running" in r.stdout.lower():
            ok = (_privileged(["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"])
                  and _privileged(["firewall-cmd", "--reload"]))
            if ok:
                print(f"[Dashboard] firewalld: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload")
            return
    except FileNotFoundError:
        pass

    try:  # iptables (not persistent but works until reboot)
        r = subprocess.run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
        if r.returncode == 0:
            if _privileged(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]):
                print(f"[Dashboard] iptables: port {port} opened.")
            else:
                print(f"[Dashboard] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT")
    except FileNotFoundError:
        pass  # no iptables means firewall is probably off — nothing to do


def _ensure_crypto_js() -> None:
    if _CRYPTOJS_FILE.exists():
        return
    try:
        import urllib.request
        print("[Dashboard] Downloading CryptoJS (one-time setup)…")
        urllib.request.urlretrieve(_CRYPTOJS_CDN, str(_CRYPTOJS_FILE))
        print("[Dashboard] CryptoJS cached — will serve locally from now on.")
    except Exception as e:
        print(f"[Dashboard] CryptoJS download failed: {e}")
        print(f"[Dashboard] Encryption will fall back to CDN load on client.")


# CryptoJS is vendored: no startup network/download side effects.


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, no internet required."""
    # Method 1: route trick (fast, works when internet is available)
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Method 2: hostname resolution (works offline on most systems)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Method 3: enumerate all interfaces (fully offline, no external deps)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def _ensure_certs() -> bool:
    """
    Make sure config/certs holds a TLS key pair, generating a self-signed one the
    first time the dashboard runs.

    The pair is deliberately NOT shipped in the repository. A private key that
    every user downloads is the same as having no private key at all: anyone can
    present a certificate that matches it. Generating locally gives each install
    its own key, costs about a second, and happens exactly once.

    Returns True when a usable pair exists afterwards; False leaves the caller on
    plain HTTP, which still works — the QR code simply encodes http:// instead.
    """
    certs = BASE_DIR / "config" / "certs"
    key_p = certs / "jarvis.key"
    crt_p = certs / "jarvis.crt"
    if key_p.exists() and crt_p.exists():
        return True

    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("[Dashboard] cryptography not installed — serving over plain HTTP.")
        print("[Dashboard] For HTTPS run:  pip install cryptography")
        return False

    try:
        certs.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        who = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Dashboard"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS"),
        ])

        # The SAN has to cover every address the phone might use: the LAN IP the
        # QR code encodes, plus localhost when testing on the machine itself.
        alt = [x509.DNSName("localhost"),
               x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
        try:
            lan = _local_ip()
            if not lan.startswith("127."):
                alt.append(x509.IPAddress(ipaddress.IPv4Address(lan)))
        except Exception:
            pass          # no LAN address resolvable — localhost entries still work

        # Timezone-aware UTC: datetime.utcnow() is deprecated from Python 3.12 on,
        # and the builder normalises aware values to UTC itself.
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(who)
            .issuer_name(who)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )

        key_p.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        crt_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        try:
            import os as _os
            _os.chmod(key_p, 0o600)   # best effort — largely a no-op on Windows
        except Exception:
            pass

        print(f"[Dashboard] Generated a self-signed certificate for this machine: {certs}")
        return True
    except Exception as e:
        print(f"[Dashboard] Certificate generation failed ({e}) — serving over plain HTTP.")
        return False


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ── DashboardServer ───────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self, public_url=None, host=None, spotify_oauth=None):
        self._host = host or os.environ.get('JARVIS_BIND', '127.0.0.1' if headless.enabled() else '0.0.0.0')
        self._public_url = (public_url or os.environ.get('JARVIS_PUBLIC_URL', '')).rstrip('/')
        if headless.enabled() and self._host not in ('127.0.0.1', '::1', 'localhost'):
            raise ValueError('Headless dashboard must bind loopback behind trusted HTTPS')
        if self._public_url:
            parsed = urlsplit(self._public_url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or parsed.path):
                raise ValueError('JARVIS_PUBLIC_URL must be an HTTPS origin, without path or credentials')
        self._interrupt_callback = None
        self._audio_allowed_callback = None
        self._status = {'type': 'status', 'state': 'sleeping'}
        self._audio_clients = set()
        self._audio_owner = None
        self._audio_epoch = 0
        self._capture_serial = 0
        self._audio_epoch_has_pcm = False
        self._http_ready = asyncio.Event()
        self._control_ready = asyncio.Event()
        self._pair_attempts = []
        self._ip = '127.0.0.1' if headless.enabled() else _local_ip()
        self._tokens: set[str]            = set()
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        self._aes_cache:  dict[str, bytes]= {}   # session_key → AES bytes
        self._clients: set[WebSocket]     = set()
        self._history: list[dict]         = []
        self._command_queue               = asyncio.Queue()
        self._wake_callback               = None
        self._connect_callback            = None
        self._pending_keys: dict[str, float] = {}
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=200)
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        if spotify_oauth is not None:
            self._spotify_oauth = spotify_oauth
        else:
            oauth = default_oauth()
            # Fail fast: a corrupt or unsafe OAuth store is a permanent
            # configuration failure. SpotifyConfigError subclasses ValueError,
            # so headless startup maps it to the existing exit-78 path.
            oauth.status()
            self._spotify_oauth = oauth
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        # Rotation is explicit and revokes *everything*, including paired devices.
        self._pending_keys.clear()
        self._tokens.clear()
        self._token_keys.clear()
        self._aes_cache.clear()
        self._device_sessions.clear()
        self.invalidate_audio()
        self._history.clear()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        self._revocation_tasks = []
        if loop:
            for ws in list(self._clients | self._audio_clients):
                self._revocation_tasks.append(loop.create_task(ws.close(code=4001)))
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key

    @staticmethod
    def _ssl_enabled() -> bool:
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    def get_url(self) -> str:
        if self._public_url:
            return self._public_url
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        if self._public_url:
            return self._public_url
        """URL for manual browser entry. When HTTPS active, points to alias port (also HTTPS)."""
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"{self._ip}:{PORT}"

    def _aes_key(self, session_key: str) -> bytes:
        if session_key not in self._aes_cache:
            self._aes_cache[session_key] = _derive_key(session_key)
        return self._aes_cache[session_key]

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        sk = self._token_keys.get(token)
        if not sk:
            return None
        try:
            return _decrypt_cbc(self._aes_key(sk), enc_b64)
        except Exception:
            return None

    # ── callbacks ────────────────────────────────────────────────────────

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    def set_interrupt_callback(self, fn):
        self._interrupt_callback = fn

    def set_audio_allowed_callback(self, fn):
        self._audio_allowed_callback = fn

    def audio_allowed(self):
        try:
            return bool(self._audio_allowed_callback and self._audio_allowed_callback())
        except Exception:
            return False

    def current_status(self):
        return {**self._status, 'audio_allowed': self.audio_allowed(),
                'audio_busy': self._audio_owner is not None}

    def invalidate_audio(self):
        old_epoch = self._audio_epoch
        self._audio_epoch += 1
        # Discard stale PCM, but retain a bounded upstream closure obligation.
        # A cumulative epoch fence subsumes older pending closes; it cannot end
        # input belonging to a newer owner, even if delivered late.
        needs_close = self._audio_epoch_has_pcm
        while not self._phone_audio_queue.empty():
            message = self._phone_audio_queue.get_nowait()
            needs_close |= bool(message.get('audio_stream_end'))
        self._audio_epoch_has_pcm = False
        if needs_close:
            self._phone_audio_queue.put_nowait({'audio_stream_end': True,
                                                'control': True, 'epoch': old_epoch})

    def audio_message_valid(self, message):
        return (self.audio_allowed() and message.get('epoch') == self._audio_epoch
                and 0 <= time.monotonic() - message.get('at', 0) <= 1.0)


    async def _read_control_message(self, reader):
        raw = b''
        while len(raw) < 4097:
            chunk = await reader.read(4097 - len(raw))
            if not chunk:
                break
            raw += chunk
            if b'\n' in raw:
                break
        return raw

    async def _dispatch_control(self, payload):
        if not isinstance(payload, dict):
            return {'ok': False, 'error': 'malformed control request'}
        command = payload.get('command')
        if command == 'spotify-auth-start':
            client_id = payload.get('client_id')
            if not isinstance(client_id, str) or not client_id:
                return {'ok': False, 'error': 'invalid client_id'}
            try:
                start = self._spotify_oauth.start_authorization(client_id)
            except Exception:
                return {'ok': False, 'error': 'invalid client_id'}
            return {'ok': True, 'authorization_url': start.url, 'state_expires_at': start.state_expires_at.isoformat()}
        if command == 'spotify-auth-status':
            try:
                status = self._spotify_oauth.status()
            except Exception:
                return {'ok': False, 'error': 'status unavailable'}
            return {
                'ok': True,
                'authorized': status.authorized,
                'reauthorization_required': status.reauthorization_required,
                'authorized_at': status.authorized_at.isoformat() if status.authorized_at else None,
                'authorization_expires_at': status.authorization_expires_at.isoformat() if status.authorization_expires_at else None,
                'access_expires_at': status.access_expires_at.isoformat() if status.access_expires_at else None,
            }
        return {'ok': False, 'error': 'unknown control command'}

    async def serve_control(self, path=None):
        """Local operator channel, inside a private directory, socket mode 0600."""
        path = Path(path or headless.control_path())
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
            raise PermissionError('Pairing control directory must be owned by service user and mode 0700')
        # Refuse a second live instance. Only remove our own stale socket.
        if path.exists():
            try:
                reader, writer = await asyncio.open_unix_connection(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                writer.close()
                await writer.wait_closed()
                raise RuntimeError('A JARVIS control socket is already active')

        async def handle(reader, writer):
            reply = None
            try:
                raw = await asyncio.wait_for(self._read_control_message(reader), 2)
                head, sep, tail = raw.partition(b'\n')
                if not sep or tail:
                    reply = {'ok': False, 'error': 'control frame must contain exactly one command'}
                elif head == b'pair':
                    if not self._public_url:
                        reply = {'error': 'Set JARVIS_PUBLIC_URL to the trusted external HTTPS origin before pairing'}
                    else:
                        key = self.new_key()
                        await asyncio.gather(*self._revocation_tasks, return_exceptions=True)
                        reply = {'url': f'{self.get_url()}/auto-login?key={key}'}
                else:
                    try:
                        payload = json.loads(head.decode('utf-8'))
                    except (UnicodeDecodeError, ValueError):
                        reply = {'ok': False, 'error': 'malformed control request'}
                    else:
                        reply = await self._dispatch_control(payload)
            except TimeoutError:
                reply = {'ok': False, 'error': 'control request timed out'}
            finally:
                if reply is not None:
                    try:
                        writer.write((json.dumps(reply) + '\n').encode())
                        await writer.drain()
                    except Exception:
                        pass
                writer.close()
                await writer.wait_closed()


        # Cancellation may race the bind's completion. Shield and reap it so a
        # sibling HTTP startup failure cannot leak a bound, unowned socket.
        binding = asyncio.create_task(asyncio.start_unix_server(handle, str(path), limit=256))
        server = None
        try:
            server = self._control_server = await asyncio.shield(binding)
            async with server:
                os.chmod(path, 0o600)
                self._control_ready.set()
                await server.serve_forever()
        finally:
            self._control_ready.clear()
            if server is None:
                server = await binding
            server.close()
            await server.wait_closed()
            path.unlink(missing_ok=True)

    async def wait_ready(self):
        """Ready means HTTP is accepting and the private control socket is bound."""
        await asyncio.gather(self._http_ready.wait(), self._control_ready.wait())

    async def _watch_status(self):
        previous = None
        while True:
            status = self.current_status()
            if status != previous:
                if not status['audio_allowed']:
                    self.invalidate_audio()
                    for ws in list(self._audio_clients):
                        with contextlib.suppress(Exception):
                            await ws.close(code=4009)
                await self.broadcast(status)
                previous = status
            await asyncio.sleep(0.1)

    # ── broadcast ────────────────────────────────────────────────────────

    async def broadcast(self, msg: dict) -> None:
        if msg.get('type') == 'status':
            self._status = {'type':'status', 'state':msg.get('state', self._status['state'])}
            msg = self.current_status()
        else:
            self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        @app.middleware('http')
        async def private_headers(req, call_next):
            response = await call_next(req)
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            return response

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return bool(tok) and tok in self._tokens

        _SPOTIFY_CONNECTED_HTML = (
            "<!DOCTYPE html>"
            "<html><head><meta charset=\"UTF-8\">"
            "<title>Spotify Connected</title></head>"
            "<body><h1>Spotify is connected</h1>"
            "<p>You may close this window.</p></body></html>"
        )
        _SPOTIFY_ERROR_HTML = (
            "<!DOCTYPE html>"
            "<html><head><meta charset=\"UTF-8\">"
            "<title>Spotify Connection Failed</title></head>"
            "<body><h1>Spotify connection failed</h1>"
            "<p>Try again from the JARVIS control interface.</p></body></html>"
        )

        @app.get("/api/spotify/callback")
        async def spotify_callback(state: str = "", code: str = "", error: str = ""):
            try:
                status = self._spotify_oauth.complete_authorization(
                    state=state or None, code=code or None, error=error or None
                )
            except Exception:
                # Never a 500: the operator page gets the fixed no-store 400
                # HTML and the internal reason is never echoed or logged.
                logger.warning('spotify callback authorization failed: %s',
                               type(self._spotify_oauth).__name__)
                return HTMLResponse(_SPOTIFY_ERROR_HTML, status_code=400)
            if status.authorized:
                return HTMLResponse(_SPOTIFY_CONNECTED_HTML, status_code=200)
            return HTMLResponse(_SPOTIFY_ERROR_HTML, status_code=400)

        def pairing_rate_allowed():
            now = time.monotonic()
            self._pair_attempts = [t for t in self._pair_attempts if t > now - 60]
            if len(self._pair_attempts) >= 10:
                return False
            self._pair_attempts.append(now)
            return True

        @app.get('/api/status')
        async def status_ep(req: Request):
            if not _auth(req):
                return JSONResponse({'error':'Unauthorized'}, status_code=401)
            return JSONResponse(self.current_status())

        @app.post('/api/interrupt')
        async def interrupt_ep(req: Request):
            if not _auth(req):
                return JSONResponse({'error':'Unauthorized'}, status_code=401)
            self.invalidate_audio()
            if self._interrupt_callback:
                self._interrupt_callback()
            for ws in list(self._audio_clients):
                with contextlib.suppress(Exception):
                    await ws.close(code=4009)
            await self.broadcast(self.current_status())
            return JSONResponse({'ok':True})

        # serve CryptoJS from local cache, fallback to CDN redirect
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(str(_CRYPTOJS_FILE),
                                    media_type="application/javascript")
            from fastapi.responses import RedirectResponse
            return RedirectResponse(_CRYPTOJS_CDN)

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            # Auth is handled client-side via sessionStorage bearer token.
            # Server-side header auth can't work here because browser navigations
            # don't send custom headers (location.href doesn't carry Authorization).
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(PORT)))
            return HTMLResponse(html)

        @app.post("/login")
        async def login(req: Request):
            if not pairing_rate_allowed():
                return JSONResponse({'error':'Try again later'}, status_code=429)
            body    = await req.json()
            entered = str(body.get("pin", "")).strip().upper()
            now     = time.time()
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                del self._pending_keys[entered]          # one-time use
                tok = secrets.token_urlsafe(32)
                self._tokens.add(tok)
                self._token_keys[tok] = entered
                self._aes_key(entered)                   # pre-derive & cache
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                # Bearer token in response body — no cookies needed (works on any browser/HTTP)
                return JSONResponse({"ok": True, "token": tok})
            return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                status_code=401)

        @app.get("/auto-login")
        async def auto_login(key: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
            if not pairing_rate_allowed():
                return JSONResponse({'error':'Try again later'}, status_code=429)
            now = time.time()
            if not key or key not in self._pending_keys or self._pending_keys[key] <= now:
                return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
  h2{color:#f87171;margin-bottom:12px}p{color:#5e6a7e;font-size:14px}
</style></head>
<body><div><h2>Link Expired</h2>
<p>Ask the server operator for a fresh pairing link. On the server, from the JARVIS checkout<br>
using its Python environment, run <code>python -m core.headless pair</code>.</p>
<p>Open the trusted private HTTPS link in Safari over the private network/Tailscale.<br>
Tap the microphone button and allow microphone access in Safari's website permissions.<br>
Do not bypass certificate warnings or browser security protections.</p>
</div></body></html>""")

            del self._pending_keys[key]
            tok     = secrets.token_urlsafe(32)
            dev_tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = key
            self._aes_key(key)
            self._device_sessions[dev_tok] = {"session_key": key}

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))

            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('jarvis_token','{tok}');
  sessionStorage.setItem('jarvis_key','{key}');
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS…</p>
</body></html>""")

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = (body.get("device_token") or "").strip()
            if not dev_tok or dev_tok not in self._device_sessions:
                return JSONResponse({"ok": False}, status_code=401)
            session_key = self._device_sessions[dev_tok]["session_key"]
            tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = session_key
            self._aes_key(session_key)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            return JSONResponse({"ok": True, "token": tok, "key": session_key})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Invalidate all persistent device tokens (admin action)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            count = len(self._device_sessions)
            self._device_sessions.clear()
            return JSONResponse({"ok": True, "revoked": count})

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body  = await req.json()
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc   = body.get("enc", "")
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text:
                await self._command_queue.put(text)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            if (headless.capture_source() != 'phone' or self._audio_owner is not None
                    or not self.audio_allowed()):
                await websocket.close(code=4009)
                return
            self._audio_owner = websocket
            self._audio_clients.add(websocket)
            epoch = self._audio_epoch
            self._capture_serial += 1
            capture = self._capture_serial
            ended = False
            try:
                await websocket.accept()
                await websocket.send_json({'type':'audio_ready'})
                while True:
                    message = await websocket.receive()
                    if message['type'] == 'websocket.disconnect':
                        break
                    if tok not in self._tokens or not self.audio_allowed() or epoch != self._audio_epoch:
                        self.invalidate_audio()
                        await websocket.close(code=4009)
                        break
                    data = message.get('bytes')
                    if data is None:
                        try:
                            end = json.loads(message.get('text', '{}')).get('type') == 'audio_stream_end'
                        except (ValueError, AttributeError):
                            end = False
                        if not end:
                            await websocket.close(code=4002)
                            break
                        await self._phone_audio_queue.put({'audio_stream_end':True,
                                                          'epoch':epoch, 'capture':capture,
                                                          'at':time.monotonic()})
                        ended = True
                        await websocket.send_json({'type':'audio_ended'})
                        await websocket.close(code=1000)
                        break
                    if not data or len(data) > 8192 or len(data) % 2:
                        await websocket.close(code=4002)
                        break
                    try:
                        self._phone_audio_queue.put_nowait(
                            {'data':data, 'mime_type':'audio/pcm;rate=16000',
                             'epoch':epoch, 'capture':capture, 'at':time.monotonic()}
                        )
                        self._audio_epoch_has_pcm = True
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                self._audio_clients.discard(websocket)
                if self._audio_owner is websocket:
                    self._audio_owner = None
                    if not ended and epoch == self._audio_epoch:
                        self.invalidate_audio()

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, token: str = ""):
            # Auth via query param — browser <a download> can't send custom headers
            tok = token.strip()
            if not tok or tok not in self._tokens:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r'[/\\]', '', filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            try:
                await websocket.accept()
                self._clients.add(websocket)
                await websocket.send_json(self.current_status())
                for entry in self._history[-50:]:
                    try:
                        await websocket.send_json(entry)
                    except Exception:
                        break
                while True:
                    data = await websocket.receive_json()
                    if tok not in self._tokens:
                        await websocket.close(code=4001)
                        break
                    if data.get("type") == "command":
                        enc = data.get("enc", "")
                        t   = self._decrypt(tok, enc) if enc else (data.get("text") or "").strip()
                        if t:
                            await self._command_queue.put(t)
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
        )
        print(f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        if headless.enabled():
            # Trusted private reverse proxy terminates TLS. No firewall changes,
            # self-signed certificates, alias listeners or secret query logging.
            if self._host not in ('127.0.0.1', '::1', 'localhost'):
                raise ValueError('Headless dashboard must bind loopback behind trusted HTTPS')
            cfg = uvicorn.Config(self.app, host=self._host, port=PORT,
                                 log_level='warning', access_log=False,
                                 proxy_headers=False, timeout_graceful_shutdown=3)
            # Bind ourselves: Uvicorn's normal bind path calls sys.exit(1),
            # which escapes task supervision. An OSError here remains retryable.
            family = socket.AF_INET6 if self._host == '::1' else socket.AF_INET
            with socket.create_server((self._host, PORT), family=family) as listener:
                listener.setblocking(False)
                class OwnedHTTPServer(uvicorn.Server):
                    cleaned = False

                    def capture_signals(self):
                        # The application owns SIGTERM, not nested HTTP.
                        return contextlib.nullcontext()

                    async def shutdown(self, sockets=None):
                        try:
                            await super().shutdown(sockets=sockets)
                        finally:
                            self.cleaned = True

                server = self._http_server = OwnedHTTPServer(cfg)

                async def serve_http():
                    try:
                        await server.serve(sockets=[listener])
                    except SystemExit as exc:
                        raise RuntimeError('HTTP server exited during startup') from exc

                serving = asyncio.create_task(serve_http())
                watcher = asyncio.create_task(self._watch_status())
                try:
                    while not server.started:
                        if serving.done():
                            await serving
                            raise RuntimeError('HTTP startup failed')
                        await asyncio.sleep(.01)
                    if serving.done():
                        await serving
                        raise RuntimeError('HTTP startup failed')
                    self._http_ready.set()
                    done, _ = await asyncio.wait((serving, watcher),
                                                 return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        await task
                finally:
                    self._http_ready.clear()
                    server.should_exit = True
                    try:
                        await asyncio.wait_for(asyncio.shield(serving), 5)
                    except TimeoutError:
                        serving.cancel()
                        await asyncio.gather(serving, return_exceptions=True)
                    finally:
                        try:
                            if server.started and not server.cleaned:
                                await server.shutdown(sockets=[listener])
                        finally:
                            watcher.cancel()
                            await asyncio.gather(watcher, return_exceptions=True)
            return
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Firewall setup runs in a thread — uvicorn starts immediately,
        # no waiting for UAC dialogs or subprocess timeouts.
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT)

        # Generate the TLS pair on first run so no private key ships in the repo.
        _ensure_certs()

        use_ssl  = self._ssl_enabled()
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        if use_ssl:
            asyncio.create_task(self._serve_alias())

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT, log_level="warning",
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        proto = "https" if use_ssl else "http"
        print(f"[Dashboard] {proto}://{self._ip}:{PORT}")
        print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        await uvicorn.Server(cfg).serve()

from core.user_paths import get_user_data_dir
"""
dashboard/server.py — Brahma Local HTTP Dashboard

HTTPS on port 8000 with a locally generated certificate, plus application-layer AES-256-CBC/session authentication.
CryptoJS is auto-downloaded once and served locally — no CDN needed after that.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import socket
import string
import sys
import time
from pathlib import Path

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
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR    = Path(__file__).resolve().parent.parent

def _get_static_dir() -> Path:
    if getattr(sys, "frozen", False):
        candidate = Path(sys._MEIPASS) / "dashboard" / "static"
        if candidate.exists():
            return candidate
        candidate = Path(sys._MEIPASS) / "static"
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent / "static"

STATIC_DIR  = _get_static_dir()
PORT        = 8000
MAX_UPLOAD_MB = 500


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "Brahma Uploads",
        Path.home() / "Documents" / "Brahma Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()

def _quiet_run(*args, **kwargs):
    import platform
    import subprocess

    if platform.system() == 'Windows':
        kwargs.setdefault('creationflags', getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return subprocess.run(*args, **kwargs)


def _get_gemini_key() -> str | None:
    try:
        from config import get_api_key
        return get_api_key("Gemini") or None
    except Exception:
        return None

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b'BRAHMA-DASHBOARD-v1'


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) → 32-byte AES-256 key (microseconds, no PBKDF2 needed)."""
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext ‖ HMAC-SHA256) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw = base64.b64decode(enc_b64, validate=True)
    if len(raw) < 16 + 16 + 32:
        raise ValueError("Encrypted payload is incomplete")
    body, tag = raw[:-32], raw[-32:]
    expected = hmac.new(aes_key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("Encrypted payload authentication failed")
    iv, ct = body[:16], body[16:]
    dec = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded = dec.update(ct) + dec.finalize()
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

        port_rule = f"Brahma Dashboard Port {port}"
        prog_rule  = "Brahma Dashboard Python"
        py_exe     = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = _quiet_run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        need_port = not _netsh_rule_exists(port_rule)
        need_prog = not _netsh_rule_exists(prog_rule)

        if not need_port and not need_prog:
            return  # already fully configured

        # Build a .bat file — netsh + powershell, runs fast when elevated
        bat_lines = ["@echo off"]
        if need_port:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{port_rule}" protocol=TCP dir=in '
                f'localport={port} action=allow profile=Private'
            )
        if need_prog:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes profile=Private'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="brahma_fw_")
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
            r = _quiet_run(
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
                print("[Dashboard] Phone connections may fail until Brahma is run as Administrator.")
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
            r = _quiet_run(
                [fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do

            py = sys.executable
            listed = _quiet_run(
                [fw_ctl, "--listapps"], capture_output=True, text=True, timeout=5,
            )
            if py in listed.stdout:
                return  # already allowed

            print("[Dashboard] One-time network setup — enter your password in the macOS dialog.")
            _quiet_run(
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
                r = _quiet_run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try:  # ufw
        r = _quiet_run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            if _privileged(["ufw", "allow", f"{port}/tcp"]):
                print(f"[Dashboard] ufw: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try:  # firewalld
        r = _quiet_run(
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
        r = _quiet_run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
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


_ensure_crypto_js()


# ── helpers ───────────────────────────────────────────────────────────────────

def _is_private_ipv4(ip: str) -> bool:
    try:
        parts = [int(part) for part in ip.split('.')]
        if len(parts) != 4:
            return False
        a, b, c, d = parts
        if a == 10:
            return True
        if a == 172 and 16 <= b <= 31:
            return True
        if a == 192 and b == 168:
            return True
        return False
    except Exception:
        return False


def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, preferring a private network IP."""
    preferred_private: list[str] = []
    private_candidates: list[str] = []
    other_candidates: list[str] = []

    def _add_candidate(ip: str, preferred: bool = False) -> None:
        if not ip or ip in ('127.0.0.1', '0.0.0.0'):
            return
        if ip.startswith('127.') or ip.startswith('169.254.'):
            return
        if _is_private_ipv4(ip):
            if preferred:
                preferred_private.append(ip)
            else:
                private_candidates.append(ip)
        else:
            other_candidates.append(ip)

    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                ip = getattr(addr, 'address', '') or ''
                if ip and '.' in ip:
                    _add_candidate(ip)
    except Exception:
        pass

    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            _add_candidate(info[4][0])
    except Exception:
        pass

    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            _add_candidate(ip)
    except Exception:
        pass

    try:
        import subprocess
        r = _quiet_run(['ipconfig'], capture_output=True, text=True, timeout=8)
        for ip in re.findall(r'IPv4[^:]*:\s*([0-9]+(?:\.[0-9]+){3})', r.stdout):
            _add_candidate(ip)
    except Exception:
        pass

    for probe in ('8.8.8.8', '1.1.1.1', '192.168.1.1'):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            _add_candidate(s.getsockname()[0], preferred=True)
            s.close()
        except Exception:
            pass

    for ip in preferred_private:
        if _is_private_ipv4(ip):
            return ip

    for ip in private_candidates:
        if _is_private_ipv4(ip):
            return ip

    for ip in other_candidates:
        if ip:
            return ip

    return '127.0.0.1'


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _ensure_ssl_certs() -> bool:
    """Create or repair local self-signed certs without accepting partial/corrupt files."""
    certs = get_user_data_dir() / "config" / "certs"
    key_path = certs / "brahma.key"
    cert_path = certs / "brahma.crt"
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime
        import ipaddress
        import os

        if key_path.is_file() and cert_path.is_file():
            try:
                key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
                cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
                now = datetime.datetime.now(datetime.timezone.utc)
                not_before = getattr(cert, "not_valid_before_utc", cert.not_valid_before)
                not_after = getattr(cert, "not_valid_after_utc", cert.not_valid_after)
                key_public = key.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                cert_public = cert.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                if not (not_before <= now <= not_after) or key_public != cert_public:
                    raise ValueError("dashboard certificate/key pair is invalid or mismatched")
                return True
            except Exception:
                # Regenerate invalid or incomplete local credentials below.
                pass

        certs.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "Brahma AI Local Remote"),
        ])
        alt_names = [
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]
        try:
            alt_names.append(x509.IPAddress(ipaddress.ip_address(_local_ip())))
        except Exception:
            pass
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
            .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
            .sign(key, hashes.SHA256())
        )

        key_bytes = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        cert_bytes = cert.public_bytes(serialization.Encoding.PEM)
        token = secrets.token_hex(8)
        key_tmp = certs / f".brahma-{token}.key.tmp"
        cert_tmp = certs / f".brahma-{token}.crt.tmp"
        try:
            key_tmp.write_bytes(key_bytes)
            cert_tmp.write_bytes(cert_bytes)
            os.replace(key_tmp, key_path)
            os.replace(cert_tmp, cert_path)
        finally:
            key_tmp.unlink(missing_ok=True)
            cert_tmp.unlink(missing_ok=True)
        return True
    except Exception as exc:
        print(f"[Dashboard] Could not create HTTPS certificate: {exc}")
        return False

# ── DashboardServer ───────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self):
        self._ip                          = _local_ip()
        self._tokens: set[str]            = set()
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        self._token_expiry: dict[str, float] = {}
        self._login_failures: dict[str, tuple[int, float]] = {}
        self._aes_cache:  dict[str, bytes]= {}   # session_key → AES bytes
        self._clients: set[WebSocket]     = set()
        self._history: list[dict]         = []
        self._command_queue               = asyncio.Queue(maxsize=64)
        self._server                      = None
        self._wake_callback               = None
        self._connect_callback            = None
        self._pending_keys: dict[str, float] = {}
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=200)
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def _prune_auth_state(self) -> None:
        now = time.time()
        expired_tokens = [
            token for token, expiry in self._token_expiry.items()
            if expiry <= now
        ]
        for token in expired_tokens:
            self._token_expiry.pop(token, None)
            self._tokens.discard(token)
            session_key = self._token_keys.pop(token, None)
            if session_key:
                self._aes_cache.pop(session_key, None)

        # Bound runtime-local bearer state so repeated reconnects cannot grow
        # memory indefinitely. Shortest-lived tokens are discarded first.
        if len(self._tokens) > 512:
            ordered = sorted(self._token_expiry.items(), key=lambda item: item[1])
            for token, _expiry in ordered[: len(self._tokens) - 512]:
                self._token_expiry.pop(token, None)
                self._tokens.discard(token)
                session_key = self._token_keys.pop(token, None)
                if session_key:
                    self._aes_cache.pop(session_key, None)

        if len(self._device_sessions) > 256:
            for device_token in list(self._device_sessions)[: len(self._device_sessions) - 256]:
                self._device_sessions.pop(device_token, None)

        stale_failures = [
            ip for ip, (_attempts, blocked_until) in self._login_failures.items()
            if blocked_until <= now - 300
        ]
        for ip in stale_failures:
            self._login_failures.pop(ip, None)
        if len(self._login_failures) > 2048:
            for ip, _value in list(self._login_failures.items())[: len(self._login_failures) - 2048]:
                self._login_failures.pop(ip, None)

    def new_key(self, expiry_secs: int = 600) -> str:
        self._prune_auth_state()
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key
    def get_url(self) -> str:
        return f"https://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        """URL for manual browser entry on the local network."""
        return f"https://{self._ip}:{PORT}"

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

    # ── broadcast ────────────────────────────────────────────────────────

    def _enqueue_command(self, text: str) -> bool:
        """Queue one authenticated remote command without blocking the HTTP/WS handler."""
        value = str(text or "").strip()
        if not value:
            return True
        try:
            self._command_queue.put_nowait(value)
            return True
        except asyncio.QueueFull:
            return False

    async def broadcast(self, msg: dict) -> None:
        self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await asyncio.wait_for(ws.send_json(msg), timeout=5.0)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        def _valid_token(tok: str) -> bool:
            self._prune_auth_state()
            token = str(tok or "").strip()
            if not token or token not in self._tokens:
                return False
            expiry = self._token_expiry.get(token, 0.0)
            if expiry and expiry <= time.time():
                session_key = self._token_keys.pop(token, None)
                self._tokens.discard(token)
                self._token_expiry.pop(token, None)
                if session_key:
                    self._aes_cache.pop(session_key, None)
                return False
            return True

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return _valid_token(tok)

        async def _read_json_limited(req: Request, max_bytes: int) -> dict:
            chunks: list[bytes] = []
            total = 0
            async for chunk in req.stream():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("Request body is too large.")
                chunks.append(chunk)
            try:
                body = json.loads(b"".join(chunks).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Invalid JSON request.") from exc
            if not isinstance(body, dict):
                raise ValueError("JSON request must be an object.")
            return body

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
            # Prune attacker-controlled failure entries before every new login
            # attempt so rotating source IPs cannot grow this map indefinitely.
            self._prune_auth_state()
            try:
                body = await _read_json_limited(req, 32 * 1024)
            except ValueError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=413 if "large" in str(exc).lower() else 400)
            entered = str(body.get("pin", "")).strip().upper()
            now = time.time()
            client_ip = getattr(getattr(req, "client", None), "host", "unknown") or "unknown"
            attempts, blocked_until = self._login_failures.get(client_ip, (0, 0.0))
            if blocked_until > now:
                return JSONResponse({"ok": False, "error": "Too many attempts; try again shortly."}, status_code=429)
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                del self._pending_keys[entered]
                self._login_failures.pop(client_ip, None)
                tok = secrets.token_urlsafe(32)
                self._tokens.add(tok)
                self._token_keys[tok] = entered
                self._token_expiry[tok] = now + 12 * 60 * 60
                self._aes_key(entered)
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                return JSONResponse({"ok": True, "token": tok})
            attempts += 1
            self._login_failures[client_ip] = (attempts, now + 60.0) if attempts >= 8 else (attempts, 0.0)
            return JSONResponse({"ok": False, "error": "Invalid or expired key"}, status_code=401)

        @app.get("/auto-login")
        async def auto_login(key: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
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
<p>Press <strong style="color:#dde3ed">Mobile Connect</strong> in Brahma to get a new QR code.</p>
</div></body></html>""")

            del self._pending_keys[key]
            tok     = secrets.token_urlsafe(32)
            dev_tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = key
            self._token_expiry[tok] = now + 12 * 60 * 60
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
  sessionStorage.setItem('brahma_token','{tok}');
  sessionStorage.setItem('brahma_key','{key}');
  localStorage.setItem('brahma_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to Brahma…</p>
</body></html>""")

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            try:
                body = await _read_json_limited(req, 32 * 1024)
            except ValueError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=413 if "large" in str(exc).lower() else 400)
            dev_tok = (body.get("device_token") or "").strip()
            if not dev_tok or dev_tok not in self._device_sessions:
                return JSONResponse({"ok": False}, status_code=401)
            session_key = self._device_sessions[dev_tok]["session_key"]
            tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = session_key
            self._token_expiry[tok] = time.time() + 12 * 60 * 60
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
            try:
                body = await _read_json_limited(req, 1024 * 1024)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=413 if "large" in str(exc).lower() else 400)
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc = body.get("enc", "")
            if enc:
                if len(str(enc).encode("utf-8")) > 512 * 1024:
                    return JSONResponse({"error": "Encrypted command payload is too large."}, status_code=413)
                text = self._decrypt(token, str(enc))
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if len(str(text).encode("utf-8")) > 256 * 1024:
                return JSONResponse({"error": "Command payload is too large."}, status_code=413)
            if text:
                if not self._enqueue_command(text):
                    return JSONResponse({"error": "Command queue is busy; retry shortly."}, status_code=429)
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
        async def phone_audio_ws(websocket: WebSocket):
            protocols = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
            tok = next((p[len("brahma-auth."):].strip() for p in protocols if p.startswith("brahma-auth.")), "")
            if not _valid_token(tok):
                await websocket.close(code=4001)
                return
            await websocket.accept(subprotocol=f"brahma-auth.{tok}")
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            try:
                while True:
                    data = await websocket.receive_bytes()
                    if len(data) > 256 * 1024:
                        await websocket.close(code=1009, reason="Audio frame too large")
                        break
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        def _open_unique_upload(root: Path, safe_name: str):
            """Atomically reserve a unique destination to prevent concurrent upload overwrites."""
            root.mkdir(parents=True, exist_ok=True)
            path = root / safe_name
            stem, suffix = path.stem, path.suffix
            for counter in range(10000):
                candidate = path if counter == 0 else root / f"{stem}_{counter}{suffix}"
                try:
                    return candidate, candidate.open("xb")
                except FileExistsError:
                    continue
            raise RuntimeError("Unable to allocate a unique upload filename.")

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                dest = None
                try:
                    dest, fout = _open_unique_upload(self._uploads_dir, safe)
                    with fout:
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


        @app.get("/api/download/{filename:path}")
        async def download_file(req: Request, filename: str):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

            safe = _safe_filename(filename)
            root = self._uploads_dir.resolve()
            try:
                target = (root / safe).resolve()
                target.relative_to(root)
            except (OSError, ValueError):
                return JSONResponse({"error": "Not found"}, status_code=404)
            if not target.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(target), filename=safe)

        @app.get("/web_background/{filename:path}")
        async def web_background_static(filename: str):
            bg_dir = (BASE_DIR / "assets" / "web_background").resolve()
            safe = filename if filename else "index.html"
            try:
                target = (bg_dir / safe).resolve()
                target.relative_to(bg_dir)
            except (OSError, ValueError):
                return JSONResponse({"error": "Not found"}, status_code=404)
            if target.is_file():
                return FileResponse(str(target))
            return JSONResponse({"error": "Not found"}, status_code=404)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket):
            protocols = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
            tok = next((p[len("brahma-auth."):].strip() for p in protocols if p.startswith("brahma-auth.")), "")
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            if not _valid_token(tok):
                await websocket.close(code=4001)
                return
            await websocket.accept(subprotocol=f"brahma-auth.{tok}")
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    raw_message = await websocket.receive_text()
                    if len(raw_message.encode("utf-8")) > 2 * 1024 * 1024:
                        await websocket.close(code=1009, reason="Message too large")
                        break
                    try:
                        data = json.loads(raw_message)
                    except json.JSONDecodeError:
                        await websocket.send_json({"type": "error", "error": "Invalid JSON message."})
                        continue
                    if data.get("type") == "command":
                        enc = data.get("enc", "")
                        if enc and len(str(enc).encode("utf-8")) > 512 * 1024:
                            await websocket.send_json({"type": "error", "error": "Encrypted command payload is too large."})
                            continue
                        t = self._decrypt(tok, str(enc)) if enc else (data.get("text") or "").strip()
                        if len(str(t).encode("utf-8")) > 256 * 1024:
                            await websocket.send_json({"type": "error", "error": "Command payload is too large."})
                            continue
                        if t:
                            if not self._enqueue_command(t):
                                await websocket.send_json({"type": "error", "error": "Command queue is busy; retry shortly."})
                                continue
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        return app

    # ── serve ─────────────────────────────────────────────────────────────
    async def _serve_alias(self) -> None:
        """Compatibility HTTPS alias on the next port."""
        ssl_key = get_user_data_dir() / "config" / "certs" / "brahma.key"
        ssl_cert = get_user_data_dir() / "config" / "certs" / "brahma.crt"
        if not _ensure_ssl_certs():
            raise RuntimeError("Dashboard HTTPS certificate could not be created.")
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
            log_config=None, access_log=False,
        )
        print(f"[Dashboard] Manual entry: https://{self._ip}:{PORT + 1}")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed - dashboard disabled.")
            print("[Dashboard] Run: pip install fastapi 'uvicorn[standard]' cryptography")
            return
        if not _ensure_ssl_certs():
            raise RuntimeError("Dashboard HTTPS certificate could not be created.")

        ssl_key = get_user_data_dir() / "config" / "certs" / "brahma.key"
        ssl_cert = get_user_data_dir() / "config" / "certs" / "brahma.crt"

        # Firewall setup runs in a thread; uvicorn starts immediately after
        # the certificate is confirmed.
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT)

        cfg = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=PORT,
            log_level="warning",
            ssl_keyfile=str(ssl_key),
            ssl_certfile=str(ssl_cert),
            log_config=None,
            access_log=False,
        )

        print(f"[Dashboard] https://{self._ip}:{PORT}")
        print("[Dashboard] Press 'Mobile Connect' in Brahma UI to get the QR code.")
        server = uvicorn.Server(cfg)
        self._server = server
        try:
            await server.serve()
        finally:
            self._server = None

    def stop(self) -> None:
        """Request a graceful shutdown of the local dashboard server."""
        server = self._server
        if server is not None:
            server.should_exit = True

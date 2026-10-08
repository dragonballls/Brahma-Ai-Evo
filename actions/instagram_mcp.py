from core.user_paths import get_user_data_dir
from core.runtime_paths import CONFIG_DIR, API_CONFIG_PATH, LOG_DIR
"""
actions/instagram_mcp.py
Instagram Model Context Protocol (MCP) & Background Social Engine for Brahma AI.

Features:
- Direct API posting (Photos & Reels) without browser clicking.
- Direct Messaging (Send & Read DMs).
- Profile & Metrics Intelligence.
- Auto-opens published posts and sent messages in the user's default browser.
- Background DM polling daemon with voice announcements:
  "You have a new Instagram message from {username}. What should I reply, or should I take over the chat?"
- Dual-mode: In-process service for Brahma Evo + standalone MCP JSON-RPC server over stdio.
"""

import sys
import os
import re
import time
import json
import queue
import threading
import webbrowser
from pathlib import Path
from concurrent.futures import Future
from typing import Optional, Dict, Any, List, Tuple

try:
    from instagrapi import Client
    INSTAGRAPI_AVAILABLE = True
except ImportError:
    Client = None
    INSTAGRAPI_AVAILABLE = False


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
CONFIG_PATH = API_CONFIG_PATH
SESSION_PATH = CONFIG_DIR / "ig_session.json"
BROWSER_PROFILE_DIR = CONFIG_DIR / "ig_browser_profile"
LOG_PATH = LOG_DIR / "ig_debug.log"


def ig_log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {msg}"
    try:
        print(entry)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        pass


MAX_DM_RECIPIENT_LENGTH = 320
MAX_DM_MESSAGE_BYTES = 8 * 1024


def _path_has_link_component(path: Path) -> bool:
    try:
        current = Path(path.anchor) if path.anchor else Path(".")
        parts = path.parts[1:] if path.anchor else path.parts
        for part in parts:
            current = current / part
            if current.is_symlink():
                return True
            is_junction = getattr(current, "is_junction", None)
            if is_junction is not None and is_junction():
                return True
            if os.name == "nt":
                attrs = getattr(current.stat(follow_symlinks=False), "st_file_attributes", 0)
                reparse = getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                if reparse and attrs & reparse:
                    return True
        return False
    except OSError:
        return True


def _validate_private_storage_path(path: Path, *, directory: bool = False) -> None:
    path = Path(path)
    if _path_has_link_component(path.parent):
        raise RuntimeError(f"Instagram storage parent contains a symlink/junction/reparse component: {path.parent}")
    if _path_has_link_component(path):
        raise RuntimeError(f"Instagram storage path must not be a symlink/junction/reparse point: {path}")
    if path.exists():
        if directory and not path.is_dir():
            raise RuntimeError(f"Instagram browser profile path is not a directory: {path}")
        if not directory:
            if not path.is_file():
                raise RuntimeError(f"Instagram session path is not a regular file: {path}")
            try:
                if int(path.stat(follow_symlinks=False).st_nlink) > 1:
                    raise RuntimeError(f"Instagram session path has multiple hard links: {path}")
            except OSError as exc:
                raise RuntimeError(f"Instagram session path could not be inspected safely: {path}") from exc


def kill_browser_processes():
    """Ensures no orphan Playwright chromium processes hold locks on ig_browser_profile."""
    try:
        import psutil
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmd = " ".join(p.info.get('cmdline') or [])
                if 'ig_browser_profile' in cmd and 'chrome' in p.info.get('name', '').lower():
                    p.kill()
            except Exception:
                pass
    except Exception:
        pass


def _validate_dm_inputs(recipient: object, message_text: object) -> tuple[str, str]:
    clean_recipient = str(recipient or "").strip().lstrip("@")
    clean_message = str(message_text or "")
    if not clean_recipient or len(clean_recipient) > MAX_DM_RECIPIENT_LENGTH:
        raise ValueError("Instagram recipient is missing or exceeds the safety limit.")
    if len(clean_message.encode("utf-8")) > MAX_DM_MESSAGE_BYTES:
        raise ValueError("Instagram message exceeds the 8 KiB safety limit.")
    if any(ord(ch) < 0x20 and ch not in {"\r", "\n", "\t"} for ch in clean_message):
        raise ValueError("Instagram message contains unsupported control characters.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in clean_recipient):
        raise ValueError("Instagram recipient contains unsupported control characters.")
    if clean_recipient.isdigit() and len(clean_recipient) > 30:
        raise ValueError("Instagram thread ID is too long.")
    return clean_recipient, clean_message


class BrowserInstagramWorker(threading.Thread):
    """
    Dedicated worker thread managing persistent Playwright Chromium browser.
    Runs headless in the background to handle inbox polling and DM sending.
    """

    def __init__(self, service: "InstagramService"):
        super().__init__(name="IGBrowserWorker", daemon=True)
        self.service = service
        self.q: queue.Queue = queue.Queue(maxsize=32)
        self.running = True
        self._stop_event = threading.Event()
        self._last_processed_msgs: Dict[str, str] = {}

    def stop(self) -> None:
        self.running = False
        self._stop_event.set()
        rejection = RuntimeError("Instagram browser worker is stopping.")
        while True:
            try:
                task = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                if task is not None:
                    _, _, fut = task
                    if not fut.done():
                        fut.set_exception(rejection)
            finally:
                self.q.task_done()
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass

    def execute(self, fn, *args, timeout: float = 35.0):
        if self._stop_event.is_set() or not self.running:
            raise RuntimeError("Instagram browser worker is stopped.")
        fut = Future()
        try:
            self.q.put((fn, args, fut), timeout=1.0)
        except queue.Full as exc:
            raise RuntimeError("Instagram browser worker is busy.") from exc
        if self._stop_event.is_set():
            # Stop may race the enqueue. Resolve the future immediately rather
            # than leaving callers waiting for a worker that is shutting down.
            if not fut.done():
                fut.set_exception(RuntimeError("Instagram browser worker is stopping."))
        return fut.result(timeout=timeout)

    def is_browser_logged_in(self, ctx) -> bool:
        try:
            cookies = ctx.cookies("https://www.instagram.com")
            cookie_map = {c["name"]: c["value"] for c in cookies}
            return "sessionid" in cookie_map and "ds_user_id" in cookie_map
        except Exception:
            return False

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            ig_log("Playwright not installed; browser worker aborted.")
            return

        _validate_private_storage_path(BROWSER_PROFILE_DIR, directory=True)
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _validate_private_storage_path(BROWSER_PROFILE_DIR, directory=True)
        ig_log("BrowserInstagramWorker starting Chromium engine...")

        with sync_playwright() as p:
            try:
                ctx = p.chromium.launch_persistent_context(
                    str(BROWSER_PROFILE_DIR),
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                    viewport={"width": 1280, "height": 800},
                )
            except Exception as e:
                ig_log(f"Failed to launch browser context: {e}")
                return

            if not self.is_browser_logged_in(ctx):
                ig_log("BrowserInstagramWorker: Persistent profile is not logged in. Waiting for user to connect via Browser.")
                try:
                    ctx.close()
                except Exception:
                    pass
                return

            POLL_INTERVAL = 35

            while not self._stop_event.is_set():
                # 1. Process pending action requests from the queue.
                try:
                    task = self.q.get(timeout=0.5)
                    if task is None:
                        break
                    fn, args, fut = task
                    try:
                        res = fn(ctx, *args)
                        fut.set_result(res)
                    except Exception as ex:
                        fut.set_exception(ex)
                    self.q.task_done()
                except queue.Empty:
                    # Waiting on the stop event is interruptible, so shutdown
                    # never has to wait out the full polling interval.
                    if self._stop_event.wait(timeout=POLL_INTERVAL):
                        break

                if self._stop_event.is_set():
                    break

                # 2. Poll the inbox after each interruptible interval.
                try:
                    if self.is_browser_logged_in(ctx):
                        self._poll_inbox(ctx)
                except Exception as e:
                    ig_log(f"Browser inbox polling notice: {e}")

            try:
                ctx.close()
            except Exception:
                pass
            ig_log("BrowserInstagramWorker stopped.")

    def get_inbox_action(self, ctx, amount: int = 5) -> List[Dict[str, Any]]:
        if not self.is_browser_logged_in(ctx):
            raise RuntimeError("Instagram browser profile is not logged in. Please click 'Connect via Browser' in Settings Hub.")

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if "/direct/inbox" not in page.url:
            page.goto("https://www.instagram.com/direct/inbox/", timeout=30000, wait_until="domcontentloaded")
            try:
                for sel in ['button:has-text("Not Now")', 'button:has-text("Cancel")']:
                    btn = page.query_selector(sel)
                    if btn:
                        btn.click()
                        page.wait_for_timeout(300)
            except Exception:
                pass
        else:
            page.reload(wait_until="domcontentloaded", timeout=20000)

        page.wait_for_timeout(2000)

        threads = []
        thread_links = page.query_selector_all('a[href*="/direct/t/"]')
        for link in thread_links[:amount]:
            try:
                href = link.get_attribute("href") or ""
                tid_match = re.search(r"/direct/t/([^/]+)", href)
                tid = tid_match.group(1) if tid_match else ""

                lines = [l.strip() for l in link.inner_text().split("\n") if l.strip()]
                sender = lines[0] if len(lines) > 0 else "Unknown"
                snippet = lines[1] if len(lines) > 1 else ""

                is_me = False
                if snippet.lower().startswith("you:"):
                    is_me = True
                    snippet = snippet[4:].strip()

                threads.append({
                    "thread_id": tid,
                    "sender": sender,
                    "users": [sender],
                    "last_message": snippet,
                    "sent_by_me": is_me,
                    "timestamp": None,
                    "url": f"https://www.instagram.com{href}" if href.startswith("/") else href,
                })
            except Exception as e:
                ig_log(f"Error parsing thread row: {e}")

        return threads

    def send_dm_action(self, ctx, recipient: str, message_text: str, open_in_browser: bool = True) -> Dict[str, Any]:
        if not self.is_browser_logged_in(ctx):
            raise RuntimeError("Instagram browser profile is not logged in. Please click 'Connect via Browser' in Settings Hub.")
        recipient, message_text = _validate_dm_inputs(recipient, message_text)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        thread_id = recipient if recipient.isdigit() else ""

        if thread_id:
            target_url = f"https://www.instagram.com/direct/t/{thread_id}/"
            page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
        else:
            page.goto("https://www.instagram.com/direct/inbox/", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            try:
                for sel in ['button:has-text("Not Now")', 'button:has-text("Cancel")']:
                    btn = page.query_selector(sel)
                    if btn:
                        btn.click()
                        page.wait_for_timeout(300)
            except Exception:
                pass

            found_thread = False
            links = page.query_selector_all('a[href*="/direct/t/"]')
            for link in links:
                if recipient.lower() in link.inner_text().lower():
                    link.click()
                    found_thread = True
                    page.wait_for_timeout(2000)
                    break

            if not found_thread:
                page.goto(f"https://www.instagram.com/{recipient}/", timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                msg_btn = page.query_selector('div[role="button"]:has-text("Message"), button:has-text("Message")')
                if msg_btn:
                    msg_btn.click()
                    page.wait_for_timeout(2500)
                else:
                    raise RuntimeError(f"Could not find conversation or Message button for @{recipient}")

        box = page.wait_for_selector(
            'div[contenteditable="true"][role="textbox"], div[aria-label*="Message"], textarea',
            timeout=15000,
        )
        if not box:
            raise RuntimeError("Could not find message input box on chat thread.")

        send_attempted = False
        try:
            box.click()
            page.wait_for_timeout(400)
            page.keyboard.type(message_text, delay=20)
            page.wait_for_timeout(400)
            send_attempted = True
            page.keyboard.press("Enter")
            page.wait_for_timeout(1500)

            cur_url = page.url
            tid_match = re.search(r"/direct/t/([^/]+)", cur_url)
            final_tid = tid_match.group(1) if tid_match else thread_id
            browser_url = f"https://www.instagram.com/direct/t/{final_tid}/" if final_tid else cur_url

            if open_in_browser:
                try:
                    webbrowser.open(browser_url)
                except Exception as e:
                    ig_log(f"Browser launch notice: {e}")

            return {
                "success": False,
                "status": "submitted",
                "submitted": True,
                "send_attempted": True,
                "delivery_verified": False,
                "error": "Instagram accepted the send interaction, but delivery was not independently verified.",
                "error_code": "DELIVERY_UNVERIFIED",
                "recipient": recipient,
                "thread_id": final_tid,
                "message": message_text,
                "browser_url": browser_url,
            }
        except Exception as exc:
            return {
                "success": False,
                "status": "unknown" if send_attempted else "failed",
                "submitted": False,
                "send_attempted": send_attempted,
                "delivery_verified": False,
                "error": f"Instagram send outcome is {'ambiguous' if send_attempted else 'not established'}: {exc}",
                "error_code": "DELIVERY_UNKNOWN" if send_attempted else "SEND_FAILED",
                "recipient": recipient,
                "thread_id": thread_id,
                "message": message_text,
            }


    def _poll_inbox(self, ctx):
        threads = self.get_inbox_action(ctx, amount=8)
        for thread in threads:
            tid = thread.get("thread_id")
            if not tid:
                continue
            last_msg = thread.get("last_message", "").strip()
            is_me = thread.get("sent_by_me", False)
            sender = thread.get("sender", "Unknown")

            if not is_me and last_msg:
                last_known = self._last_processed_msgs.get(tid)
                if last_known != last_msg:
                    self._last_processed_msgs[tid] = last_msg
                    ig_log(f"Browser Engine: Incoming DM from @{sender}: {last_msg}")

                    if self.service._reply_callback:
                        is_auto = tid in self.service._auto_threads
                        ai_resp = self.service._reply_callback(tid, sender, last_msg, is_auto)
                        if ai_resp:
                            ig_log(f"Auto-replying via browser to @{sender}: {ai_resp[:40]}...")
                            self.send_dm_action(ctx, tid, ai_resp, open_in_browser=True)
                    else:
                        try:
                            from actions.attention_monitor import speak_native
                            snippet = f": '{last_msg[:75]}...'" if len(last_msg) > 75 else f": '{last_msg}'"
                            speak_native(f"You received an Instagram message from {sender}{snippet}. What should I reply?")
                        except Exception as e:
                            ig_log(f"Voice alert error: {e}")


class InstagramService:
    """
    Core Instagram Service handling authentication, API calls, and background DM polling.
    """

    _instance: Optional["InstagramService"] = None

    @classmethod
    def instance(cls) -> "InstagramService":
        if cls._instance is None:
            cls._instance = InstagramService()
        return cls._instance

    def __init__(self):
        self._client: Optional[Client] = None
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reply_callback = None
        self._auto_threads: set[str] = set()
        self._last_processed_msgs: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._authenticated = False
        self._browser_worker: Optional[BrowserInstagramWorker] = None

    def is_browser_ready(self) -> bool:
        """Checks if persistent browser profile exists and has completed login."""
        try:
            if not (BROWSER_PROFILE_DIR.exists() and (BROWSER_PROFILE_DIR / "Default").exists()):
                return False
            from config import get_config
            data = get_config()
            return bool(
                data.get("instagram_browser_authenticated")
                and data.get("instagram_sessionid")
            )
        except Exception:
            return False

    def get_browser_worker(self) -> Optional[BrowserInstagramWorker]:
        with self._lock:
            if self._browser_worker is None or not self._browser_worker.is_alive():
                self._browser_worker = BrowserInstagramWorker(self)
                self._browser_worker.start()
            return self._browser_worker

    def load_credentials(self) -> Tuple[str, str]:
        try:
            from config import get_config
            data = get_config()
            return (
                str(data.get("instagram_username", "") or "").strip(),
                str(data.get("instagram_password", "") or "").strip(),
            )
        except Exception as e:
            ig_log(f"Error reading credentials: {e}")
        return "", ""

    def get_client(self, require_auth: bool = True) -> Client:
        with self._lock:
            if self._client is None:
                if not INSTAGRAPI_AVAILABLE:
                    raise RuntimeError("instagrapi is not installed. Please run 'pip install instagrapi'")
                self._client = Client()
                self._client.delay_range = [1, 3]

            if require_auth and not self._authenticated:
                self._login_internal()

            return self._client

    def _login_internal(self) -> bool:
        username, password = self.load_credentials()
        if not username or not password:
            raise ValueError("Instagram credentials missing. Configure username and password in Settings Hub.")

        ig_log(f"Authenticating session for {username}...")
        try:
            _validate_private_storage_path(SESSION_PATH)
            if SESSION_PATH.exists():
                try:
                    self._client.load_settings(SESSION_PATH)
                    default_cl = Client()
                    if getattr(default_cl, "user_agent", None):
                        self._client.user_agent = default_cl.user_agent
                    if getattr(default_cl, "app_id", None):
                        self._client.app_id = default_cl.app_id

                    if getattr(self._client, "user_id", None):
                        try:
                            self._client.get_timeline_feed()
                            self._authenticated = True
                            ig_log("Active session verified from cache.")
                            return True
                        except Exception:
                            pass
                except Exception as e:
                    ig_log(f"Session load notice: {e}")

            if password.startswith("sessionid:") or (len(password) > 40 and "%3A" in password):
                import re
                sid = password.replace("sessionid:", "").strip()
                user_match = re.search(r"^\d+", sid)
                user_id = user_match.group() if user_match else ""
                self._client.settings["cookies"] = {"sessionid": sid, "ds_user_id": user_id}
                self._client.init()
                self._client.authorization_data = {
                    "ds_user_id": user_id,
                    "sessionid": sid,
                    "should_use_header_over_cookies": True,
                }
                self._client.private.cookies.set("ds_user_id", user_id, domain=".instagram.com")
                self._client.private.cookies.set("sessionid", sid, domain=".instagram.com")
                self._client.private.headers.update(self._client.base_headers)
                self._client.username = username or "buildonaut.dev"
                try:
                    self._client.login_by_sessionid(sid)
                except Exception as e:
                    ig_log(f"login_by_sessionid warning (using injected cookie): {e}")
            else:
                self._client.username = username
                outcome = self._client.bloks_caa_login(username, password)
                raw_str = json.dumps(outcome, default=str)

                if outcome.get("logged_in"):
                    self._client.login_flow()
                elif "login_wrong_password" in raw_str or "Incorrect password" in raw_str or "password you entered is incorrect" in raw_str:
                    raise Exception("The password you entered is incorrect. Please double-check your Instagram password.")
                elif "checkpoint_required" in raw_str or "challenge_required" in raw_str:
                    raise Exception("Instagram requires a security checkpoint/challenge. Please verify on your phone or use Session ID.")
                else:
                    try:
                        self._client.login(username, password)
                        self._client.get_timeline_feed()
                    except Exception as e:
                        err_str = str(e)
                        if "out of date" in err_str.lower() or "CAA login" in err_str:
                            raise Exception("Instagram login rejected credentials. Please check your username and password, or use Session ID.")
                        raise

            _validate_private_storage_path(SESSION_PATH.parent, directory=True)
            SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
            _validate_private_storage_path(SESSION_PATH.parent, directory=True)
            temp_session = SESSION_PATH.with_name(f".{SESSION_PATH.name}.{uuid.uuid4().hex}.tmp")
            try:
                self._client.dump_settings(temp_session)
                _validate_private_storage_path(temp_session)
                os.chmod(temp_session, 0o600)
                os.replace(temp_session, SESSION_PATH)
                _validate_private_storage_path(SESSION_PATH)
                try:
                    os.chmod(SESSION_PATH, 0o600)
                except OSError:
                    pass
            finally:
                try:
                    temp_session.unlink(missing_ok=True)
                except OSError:
                    pass
            self._authenticated = True
            ig_log("Instagram authentication successful.")
            return True
        except Exception as e:
            self._authenticated = False
            ig_log(f"Authentication failure: {e}")
            raise

    def set_prompt_callback(self, callback):
        """Sets the incoming DM callback for voice prompts."""
        self._reply_callback = callback

    def add_auto_thread(self, thread_id: str):
        self._auto_threads.add(str(thread_id))
        ig_log(f"Thread {thread_id} added to auto-mode.")

    # --- Actions ---

    def send_dm(self, recipient: str, message_text: str, open_in_browser: bool = True) -> Dict[str, Any]:
        """
        Sends an Instagram Direct Message to a username or thread ID.
        Delivery is reported as submitted unless independently verified.
        """
        recipient, message_text = _validate_dm_inputs(recipient, message_text)
        if self.is_browser_ready():
            try:
                bw = self.get_browser_worker()
                if bw:
                    ig_log(f"Dispatching send_dm via BrowserEngine to @{recipient}...")
                    browser_result = bw.execute(bw.send_dm_action, recipient, message_text, open_in_browser)
                    if isinstance(browser_result, dict) and browser_result.get("send_attempted"):
                        return browser_result
                    return browser_result
            except Exception as e:
                ig_log(f"Browser send_dm error before delivery result: {e}. Attempting instagrapi fallback...")

        cl = self.get_client(require_auth=True)
        thread_id = None

        ig_log(f"Sending DM via Instagrapi to '{recipient}': {message_text[:40]}...")

        if recipient.isdigit() and len(recipient) > 10:
            thread_id = recipient
            try:
                cl.direct_send(message_text, thread_ids=[int(thread_id)])
            except Exception as e:
                ig_log(f"direct_send by thread_id outcome is ambiguous; refusing duplicate retry: {e}")
                return {
                    "success": False,
                    "status": "unknown",
                    "submitted": False,
                    "send_attempted": True,
                    "delivery_verified": False,
                    "error": f"Instagram send outcome is ambiguous: {e}",
                    "error_code": "DELIVERY_UNKNOWN",
                    "recipient": recipient,
                    "thread_id": thread_id,
                    "message": message_text,
                }
        else:
            try:
                user_id = cl.user_id_from_username(recipient)
            except Exception as e:
                ig_log(f"Error resolving username {recipient}: {e}")
                threads = cl.direct_threads(amount=15)
                for t in threads:
                    for u in t.users:
                        if u.username.lower() == recipient.lower():
                            thread_id = str(t.id)
                            break
                    if thread_id:
                        break
                if not thread_id:
                    raise RuntimeError(f"Could not find user @{recipient}: {e}")
                try:
                    cl.direct_send(message_text, thread_ids=[int(thread_id)])
                except Exception as send_exc:
                    ig_log(f"Fallback thread send outcome is ambiguous; refusing duplicate retry: {send_exc}")
                    return {
                        "success": False,
                        "status": "unknown",
                        "submitted": False,
                        "send_attempted": True,
                        "delivery_verified": False,
                        "error": f"Instagram send outcome is ambiguous: {send_exc}",
                        "error_code": "DELIVERY_UNKNOWN",
                        "recipient": recipient,
                        "thread_id": thread_id,
                        "message": message_text,
                    }
            else:
                try:
                    res = cl.direct_send(message_text, user_ids=[user_id])
                    if hasattr(res, "thread_id"):
                        thread_id = str(res.thread_id)
                except Exception as send_exc:
                    ig_log(f"direct_send outcome is ambiguous; refusing duplicate retry: {send_exc}")
                    return {
                        "success": False,
                        "status": "unknown",
                        "submitted": False,
                        "send_attempted": True,
                        "delivery_verified": False,
                        "error": f"Instagram send outcome is ambiguous: {send_exc}",
                        "error_code": "DELIVERY_UNKNOWN",
                        "recipient": recipient,
                        "thread_id": thread_id,
                        "message": message_text,
                    }

        browser_url = f"https://www.instagram.com/direct/t/{thread_id}/" if thread_id else "https://www.instagram.com/direct/inbox/"
        if open_in_browser:
            try:
                webbrowser.open(browser_url)
            except Exception as e:
                ig_log(f"Could not open browser: {e}")

        return {
            "success": False,
            "status": "submitted",
            "submitted": True,
            "delivery_verified": False,
            "error": "Instagram accepted the send request, but delivery was not independently verified.",
            "error_code": "DELIVERY_UNVERIFIED",
            "recipient": recipient,
            "thread_id": thread_id,
            "message": message_text,
            "browser_url": browser_url,
        }


    def get_inbox(self, amount: int = 5) -> List[Dict[str, Any]]:
        """Retrieves recent inbox conversations and unread messages."""
        if self.is_browser_ready():
            try:
                bw = self.get_browser_worker()
                if bw:
                    ig_log("Fetching inbox via BrowserEngine...")
                    res = bw.execute(bw.get_inbox_action, amount)
                    if res is not None:
                        return res
            except Exception as e:
                ig_log(f"Browser get_inbox error: {e}. Attempting instagrapi fallback...")

        cl = self.get_client(require_auth=True)
        threads = cl.direct_threads(amount=amount)
        results = []

        my_user_id = str(cl.user_id) if hasattr(cl, "user_id") else ""

        for t in threads:
            last_msg = t.messages[0] if t.messages else None
            sender = t.users[0].username if t.users else "Unknown"
            text = last_msg.text if last_msg and last_msg.text else "[Media / Non-text message]"
            is_me = str(last_msg.user_id) == my_user_id if last_msg else False

            results.append({
                "thread_id": str(t.id),
                "sender": sender,
                "users": [u.username for u in t.users],
                "last_message": text,
                "sent_by_me": is_me,
                "timestamp": last_msg.timestamp.isoformat() if last_msg and hasattr(last_msg, "timestamp") and last_msg.timestamp else None,
                "url": f"https://www.instagram.com/direct/t/{t.id}/",
            })
        return results

    def post_photo(self, image_path: str, caption: str = "", open_in_browser: bool = True) -> Dict[str, Any]:
        """
        Directly publishes a photo to the Instagram feed.
        Auto-launches the newly published post in the browser.
        """
        p = Path(image_path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Image file does not exist: {image_path}")

        cl = self.get_client(require_auth=True)
        ig_log(f"Uploading photo {p.name} with caption: {caption[:40]}...")

        media = cl.photo_upload(p, caption=caption)
        code = getattr(media, "code", None)
        post_url = f"https://www.instagram.com/p/{code}/" if code else f"https://www.instagram.com/{cl.username}/"

        if open_in_browser:
            try:
                webbrowser.open(post_url)
            except Exception as e:
                ig_log(f"Could not open browser: {e}")

        return {
            "status": "success",
            "media_id": str(getattr(media, "id", "")),
            "code": code,
            "post_url": post_url,
            "caption": caption,
        }

    def post_reel(self, video_path: str, caption: str = "", thumbnail_path: Optional[str] = None, open_in_browser: bool = True) -> Dict[str, Any]:
        """
        Directly publishes a video / Reel to Instagram.
        Auto-launches the newly published Reel in the browser.
        """
        p = Path(video_path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Video file does not exist: {video_path}")

        cl = self.get_client(require_auth=True)
        ig_log(f"Uploading Reel {p.name} with caption: {caption[:40]}...")

        thumb = Path(thumbnail_path).resolve() if thumbnail_path and Path(thumbnail_path).exists() else None
        media = cl.clip_upload(p, caption=caption, thumbnail=thumb)
        code = getattr(media, "code", None)
        reel_url = f"https://www.instagram.com/reel/{code}/" if code else f"https://www.instagram.com/{cl.username}/"

        if open_in_browser:
            try:
                webbrowser.open(reel_url)
            except Exception as e:
                ig_log(f"Could not open browser: {e}")

        return {
            "status": "success",
            "media_id": str(getattr(media, "id", "")),
            "code": code,
            "reel_url": reel_url,
            "caption": caption,
        }

    def get_user_profile(self, username: str) -> Dict[str, Any]:
        """Looks up profile metrics and details for any handle."""
        cl = self.get_client(require_auth=True)
        username = str(username).strip().lstrip("@")
        user = cl.user_info_by_username(username)
        return {
            "username": user.username,
            "full_name": user.full_name,
            "bio": user.biography,
            "followers": user.follower_count,
            "following": user.following_count,
            "posts_count": user.media_count,
            "is_verified": user.is_verified,
            "profile_pic_url": str(user.profile_pic_url_hd or user.profile_pic_url),
            "url": f"https://www.instagram.com/{user.username}/",
        }

    # --- Polling Daemon ---

    def start_daemon(self):
        """Starts the background DM listener (BrowserEngine or Instagrapi)."""
        if self._running:
            ig_log("Instagram daemon already running.")
            return

        self._stop_event.clear()

        # Prefer the browser worker when an authenticated profile exists.
        if self.is_browser_ready():
            worker = self.get_browser_worker()
            if worker is None or not worker.is_alive():
                self._running = False
                ig_log("Instagram BrowserEngine failed to start.")
                return
            self._running = True
            ig_log("Instagram BrowserEngine daemon started.")
            return

        if not INSTAGRAPI_AVAILABLE:
            self._running = False
            ig_log("instagrapi not installed and browser profile not found. Background listener skipped.")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._daemon_loop,
            name="InstagramDaemon",
            daemon=True,
        )
        self._thread.start()
        ig_log("Instagram Instagrapi background daemon started.")

    def stop_daemon(self):
        """Stops the background DM listener and joins owned worker threads."""
        self._running = False
        self._stop_event.set()
        with self._lock:
            worker = self._browser_worker
            self._browser_worker = None
        if worker is not None:
            worker.stop()
            if worker.is_alive() and worker is not threading.current_thread():
                worker.join(timeout=2.0)

        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if thread and not thread.is_alive():
            self._thread = None
        ig_log("Instagram background daemon stopped.")

    def _daemon_loop(self):
        try:
            cl = self.get_client(require_auth=True)
        except Exception as e:
            ig_log(f"Daemon initialization halted: {e}")
            self._running = False
            return

        poll_interval = 35
        try:
            while self._running:
                try:
                    threads = cl.direct_threads(amount=10)
                    my_user_id = str(cl.user_id) if hasattr(cl, "user_id") else ""

                    for thread in threads:
                        latest_msg = thread.messages[0] if thread.messages else None
                        if latest_msg and str(latest_msg.user_id) != my_user_id:
                            if self._last_processed_msgs.get(str(thread.id)) != str(latest_msg.id):
                                self._last_processed_msgs[str(thread.id)] = str(latest_msg.id)
                                text = latest_msg.text or ""
                                sender_username = thread.users[0].username if thread.users else "Unknown"

                                ig_log(f"Incoming DM from @{sender_username}: {text}")

                                if self._reply_callback:
                                    is_auto = str(thread.id) in self._auto_threads
                                    ai_response = self._reply_callback(
                                        str(thread.id), sender_username, text, is_auto
                                    )
                                    if ai_response:
                                        ig_log(
                                            f"Auto-replying to @{sender_username}: "
                                            f"{ai_response[:40]}..."
                                        )
                                        cl.direct_send(
                                            ai_response,
                                            thread_ids=[int(thread.id)],
                                        )
                                else:
                                    try:
                                        from actions.attention_monitor import speak_native
                                        clean_text = (text or "").strip()
                                        snippet = (
                                            f": '{clean_text[:75]}...'"
                                            if len(clean_text) > 75
                                            else (f": '{clean_text}'" if clean_text else "")
                                        )
                                        speak_native(
                                            f"You received a message from "
                                            f"{sender_username}{snippet}."
                                        )
                                    except Exception as e:
                                        ig_log(f"Voice alert notice: {e}")

                                if self._stop_event.wait(timeout=2):
                                    break

                    if self._stop_event.is_set():
                        break

                    try:
                        pending = cl.direct_pending_inbox()
                        for thread in pending:
                            sender_name = (
                                thread.users[0].username if thread.users else "Unknown"
                            )
                            ig_log(
                                f"Auto-approving incoming request from @{sender_name}"
                            )
                            cl.direct_pending_approve(thread.id)
                            if self._stop_event.wait(timeout=1):
                                break
                    except Exception:
                        pass

                except Exception as e:
                    err_str = str(e).lower()
                    ig_log(f"Polling cycle error: {e}")
                    if "429" in err_str or "too many requests" in err_str:
                        ig_log(
                            "Instagram rate limit (429) hit. "
                            "Pausing for 60s cooldown..."
                        )
                        if self._stop_event.wait(timeout=60):
                            break
                    elif "login_required" in err_str:
                        ig_log(
                            "Session expired (login_required). "
                            "Resetting auth flag for refresh..."
                        )
                        self._authenticated = False
                        if self._stop_event.wait(timeout=30):
                            break

                if self._stop_event.wait(timeout=poll_interval):
                    break
        finally:
            self._running = False
            if self._thread is threading.current_thread():
                self._thread = None


# --- Global Helpers matching legacy interface ---

def start_daemon():
    InstagramService.instance().start_daemon()

def stop_daemon():
    InstagramService.instance().stop_daemon()

def set_ig_prompt_callback(cb):
    InstagramService.instance().set_prompt_callback(cb)

def add_auto_thread(tid):
    InstagramService.instance().add_auto_thread(tid)

def send_direct_reply(thread_id, text):
    return InstagramService.instance().send_dm(thread_id, text, open_in_browser=True)

def get_recent_messages(amount=5) -> str:
    try:
        inbox = InstagramService.instance().get_inbox(amount=amount)
        if not inbox:
            return "You have no recent messages."
        lines = []
        for item in inbox:
            if item["sent_by_me"]:
                lines.append(f"- You replied to @{item['sender']}: \"{item['last_message']}\"")
            else:
                lines.append(f"- @{item['sender']}: \"{item['last_message']}\"")
        return "\n".join(lines)
    except Exception as e:
        return f"Error checking Instagram messages: {e}"


# --- MCP Server Specification & JSON-RPC Dispatcher ---

MCP_TOOLS = [
    {
        "name": "instagram_get_inbox",
        "description": "Checks your Instagram inbox for recent direct messages and conversation threads.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of recent threads to retrieve (default: 5)"}
            }
        }
    },
    {
        "name": "instagram_send_dm",
        "description": "Sends an Instagram direct message to a username or conversation thread, and opens the thread in your browser.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "The Instagram username (e.g. 'john_doe') or numeric thread ID"},
                "message": {"type": "string", "description": "The message text to send"},
                "open_in_browser": {"type": "boolean", "description": "Whether to auto-open the chat thread in your browser (default: true)"}
            },
            "required": ["recipient", "message"]
        }
    },
    {
        "name": "instagram_post_photo",
        "description": "Publishes a photo directly to your Instagram feed and automatically opens the live post in your browser.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Absolute path to the JPG or PNG image file"},
                "caption": {"type": "string", "description": "Caption text with optional hashtags"},
                "open_in_browser": {"type": "boolean", "description": "Whether to auto-open the published post in your browser (default: true)"}
            },
            "required": ["image_path"]
        }
    },
    {
        "name": "instagram_post_reel",
        "description": "Publishes a video/Reel directly to your Instagram account and automatically opens the live Reel in your browser.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_path": {"type": "string", "description": "Absolute path to the MP4 video file"},
                "caption": {"type": "string", "description": "Caption text with optional hashtags"},
                "thumbnail_path": {"type": "string", "description": "Optional path to cover image"},
                "open_in_browser": {"type": "boolean", "description": "Whether to auto-open the published Reel in your browser (default: true)"}
            },
            "required": ["video_path"]
        }
    },
    {
        "name": "instagram_get_user_info",
        "description": "Looks up profile intelligence for any Instagram account (follower count, following, bio, verified status).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "The Instagram handle to look up (e.g. 'natgeo')"}
            },
            "required": ["username"]
        }
    }
]


def run_mcp_stdio():
    """Simple JSON-RPC stdio handler for running as a standard MCP server."""
    svc = InstagramService.instance()
    sys.stderr.write("Instagram MCP Server listening on stdio...\n")
    sys.stderr.flush()

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line)
            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params", {})

            if method == "tools/list":
                resp = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}}
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments", {})

                if name == "instagram_get_inbox":
                    res = svc.get_inbox(amount=args.get("limit", 5))
                elif name == "instagram_send_dm":
                    res = svc.send_dm(args["recipient"], args["message"], open_in_browser=args.get("open_in_browser", True))
                elif name == "instagram_post_photo":
                    res = svc.post_photo(args["image_path"], caption=args.get("caption", ""), open_in_browser=args.get("open_in_browser", True))
                elif name == "instagram_post_reel":
                    res = svc.post_reel(args["video_path"], caption=args.get("caption", ""), thumbnail_path=args.get("thumbnail_path"), open_in_browser=args.get("open_in_browser", True))
                elif name == "instagram_get_user_info":
                    res = svc.get_user_profile(args["username"])
                else:
                    raise ValueError(f"Unknown tool: {name}")

                resp = {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(res, indent=2)}]}}
            else:
                resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}

            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        except Exception as e:
            err_resp = {"jsonrpc": "2.0", "id": req.get("id") if 'req' in locals() else None, "error": {"code": -32603, "message": str(e)}}
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()


def launch_browser_login():
    """Launches visible Chromium browser to let the user log into Instagram."""
    kill_browser_processes()
    print("Launching Chromium browser for Instagram login...")
    from playwright.sync_api import sync_playwright
    _validate_private_storage_path(BROWSER_PROFILE_DIR, directory=True)
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _validate_private_storage_path(BROWSER_PROFILE_DIR, directory=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1080, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.instagram.com/accounts/login/", timeout=60000)
        print("Please log into your Instagram account in the opened window...")

        logged_in = False
        detected_username = ""
        for i in range(300):
            try:
                cookies = ctx.cookies("https://www.instagram.com")
                cmap = {c["name"]: c["value"] for c in cookies}
                if "sessionid" in cmap and "ds_user_id" in cmap:
                    logged_in = True
                    sid = cmap["sessionid"]
                    uid = cmap["ds_user_id"]
                    time.sleep(2)
                    try:
                        u = page.evaluate("() => window._sharedData?.config?.viewer?.username || ''")
                        if u:
                            detected_username = u
                        else:
                            parts = [x for x in page.url.replace("https://www.instagram.com/", "").split("/") if x]
                            if parts and parts[0] not in ["accounts", "direct", "explore", "reels"]:
                                detected_username = parts[0]
                    except Exception:
                        pass

                    from config import save_config
                    updates = {
                        "instagram_sessionid": sid,
                        "instagram_user_id": uid,
                        "instagram_browser_authenticated": True,
                    }
                    if detected_username:
                        updates["instagram_username"] = detected_username
                    save_config(updates)
                    print(f"\n[SUCCESS] Instagram connected as @{detected_username or 'user'}!")
                    break
            except Exception:
                pass
            time.sleep(1)

        ctx.close()
        if not logged_in:
            print("\n[ERROR] Login timed out or browser was closed before completion.")


if __name__ == "__main__":
    if "--mcp" in sys.argv or "--stdio" in sys.argv:
        run_mcp_stdio()
    elif "--login" in sys.argv or "--browser-login" in sys.argv:
        launch_browser_login()
    else:
        print("Instagram MCP module ready. Run with --login to connect via browser, or --mcp to launch as an MCP server.")
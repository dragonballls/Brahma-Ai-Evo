import asyncio
import atexit
import threading
import concurrent.futures
import ipaddress
import platform
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from core.browser_pinned_proxy import PinnedBrowserProxy


def _log(message: str) -> None:
    try:
        print(message.encode("ascii", "replace").decode("ascii"))
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"))


def _get_default_browser_id() -> str:
    """Returns raw default browser identifier string for current OS."""
    system = platform.system()
    try:
        if system == "Windows":
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
            )
            prog_id = winreg.QueryValueEx(key, "ProgId")[0].lower()
            winreg.CloseKey(key)
            return prog_id

        elif system == "Darwin":
            result = subprocess.run(
                ["defaults", "read",
                 "com.apple.LaunchServices/com.apple.launchservices.secure",
                 "LSHandlers"],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.lower()

        elif system == "Linux":
            result = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.lower()

    except Exception:
        pass

    return ""


_BROWSER_BINARIES = {
    "Windows": {
        "opera":   ["opera.exe"],
        "brave":   ["brave.exe"],
        "vivaldi": ["vivaldi.exe"],
        "chrome":  ["chrome.exe"],
        "firefox": ["firefox.exe"],
    },
    "Darwin": {
        "opera":   ["opera"],
        "brave":   ["brave browser", "brave"],
        "vivaldi": ["vivaldi"],
        "chrome":  ["google chrome", "google-chrome"],
        "firefox": ["firefox"],
    },
    "Linux": {
        "opera":   ["opera", "opera-stable"],
        "brave":   ["brave-browser", "brave"],
        "vivaldi": ["vivaldi-stable", "vivaldi"],
        "chrome":  ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"],
        "firefox": ["firefox"],
    },
}


def _get_opera_executable() -> str | None:
    if platform.system() != "Windows":
        return None
    try:
        import winreg
        candidate_keys = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\opera.exe",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\launcher.exe",
            r"SOFTWARE\Clients\StartMenuInternet\OperaStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\OperaGXStable\shell\open\command",
        ]
        for key_path in candidate_keys:
            for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
                try:
                    key = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(key, None)
                    winreg.CloseKey(key)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        _log(f"[Browser] 🔍 Opera found via registry: {exe}")
                        return exe
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _find_browser_executable(prog_id: str) -> tuple:
    """
    Returns (engine_name, exe_path, channel, is_opera).
    Prioritizes Google Chrome.
    """
    system  = platform.system()
    os_bins = _BROWSER_BINARIES.get(system, {})

    # Check for Google Chrome explicitly first
    chrome_candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe") if system == "Windows" else None,
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
    ]
    for c in chrome_candidates:
        if c and os.path.exists(c):
            _log(f"[Browser] 🔍 Preferred Google Chrome found at: {c}")
            return "chromium", c, "chrome", False

    if any(x in prog_id for x in ["firefox", "mozilla"]):
        return "firefox", None, None, False

    if "safari" in prog_id:
        return "webkit", None, None, False

    if "opera" in prog_id:
        exe = _get_opera_executable()
        if exe:
            return "chromium", exe, None, True
        for binary in os_bins.get("opera", []):
            path = shutil.which(binary)
            if path:
                return "chromium", path, None, True

    browser_patterns = {
        "brave":   ["brave"],
        "vivaldi": ["vivaldi"],
        "chrome":  ["chrome"],
    }
    for browser_name, patterns in browser_patterns.items():
        if not any(p in prog_id for p in patterns):
            continue
        binaries = os_bins.get(browser_name, [])
        for binary in binaries:
            path = shutil.which(binary)
            if path:
                _log(f"[Browser] 🔍 Found {browser_name} at: {path}")
                return "chromium", path, None, False

    if "edge" in prog_id:
        return "chromium", None, "msedge", False

    return "chromium", None, "chrome", False


class _BrowserThread:

    def __init__(self):
        self._loop       = None
        self._thread     = None
        self._ready      = threading.Event()
        self._playwright = None
        self._browser    = None
        self._context    = None
        self._page       = None
        self._pages      = []
        self._engine_name = "chromium"
        self._exe_path   = None
        self._channel    = None
        self._is_opera   = False
        self._startup_error = None
        self._network_log = []
        self._console_log = []
        self._active_dialog_handler = None
        self._pinned_proxy = None

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return bool(self._playwright is not None and self._startup_error is None)
        self._loop = None
        self._playwright = None
        self._startup_error = None
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="BrowserThread"
        )
        self._thread.start()
        self._ready.wait(timeout=15)
        return bool(self._playwright is not None and self._startup_error is None and self._thread.is_alive())

    def _run_loop(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._init())
        except Exception as exc:
            self._startup_error = exc
            _log(f"[Browser] startup failed: {exc}")
        finally:
            self._ready.set()
        if self._startup_error is not None or self._loop is None:
            return
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()

    async def _init(self):
        self._playwright = await async_playwright().start()

    def run(self, coro, timeout: int = 30):
        if not self._loop:
            raise RuntimeError("BrowserThread not started.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ── Tarayıcı ve sayfa yönetimi ───────────────────────────────────────────

    async def _launch_browser_if_needed(self):
        """
        Tarayıcıyı başlatır. Zaten açıksa hiçbir şey yapmaz.
        Her zaman default tarayıcıyı kullanır, özel sekme açmaz.
        """
        if self._browser and self._browser.is_connected():
            return

        prog_id = _get_default_browser_id()
        self._engine_name, self._exe_path, self._channel, self._is_opera = _find_browser_executable(prog_id)
        engine = getattr(self._playwright, self._engine_name)

        # Temel chromium argümanları
        chromium_args = ["--start-maximized"]

        if self._is_opera:
            # Opera GX bazı sürümlerde varsayılan olarak private modda başlar.
            # Aşağıdaki flag'ler bunu engeller.
            chromium_args += [
                "--disable-features=OperaPrivacyMode",
                "--no-private",
            ]
            _log("[Browser] 🎭 Opera detected — disabling private-mode flags")

        if self._pinned_proxy is not None:
            self._pinned_proxy.close()
        self._pinned_proxy = PinnedBrowserProxy()
        proxy_url = self._pinned_proxy.start()
        launch_kwargs = {"headless": False, "proxy": {"server": proxy_url}}

        if self._engine_name == "chromium":
            launch_kwargs["args"] = chromium_args
        if self._exe_path:
            launch_kwargs["executable_path"] = self._exe_path
        elif self._channel:
            launch_kwargs["channel"] = self._channel

        try:
            self._browser = await engine.launch(**launch_kwargs)
            _log(
                f"[Browser] ✅ Launched ({self._engine_name}"
                f"{' / ' + self._channel if self._channel else ''}"
                f"{' / ' + self._exe_path if self._exe_path else ''})"
            )
        except Exception as e:
            _log(f"[Browser] ⚠️ Launch failed ({e}), falling back to built-in Chromium")
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                args=["--start-maximized"],
                proxy={"server": proxy_url},
            )

    async def _get_page(self):
        """
        Mevcut sayfayı döndürür.
        - Tarayıcı kapalıysa açar.
        - Context yoksa oluşturur.
        - Sayfa kapalıysa yeni sekme açar (aynı pencerede).
        - Sayfa zaten açıksa aynı sayfayı döndürür (yeni pencere açmaz).
        """
        await self._launch_browser_if_needed()

        if self._context is None:
            self._context = await self._browser.new_context(
                viewport=None,
                service_workers="block",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            await self._context.route("**/*", self._guard_request)

        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
            if self._page not in self._pages:
                self._pages.append(self._page)
            self._record_page(self._page)

        return self._page

    async def _guard_request(self, route, request) -> None:
        """Enforce Brahma's browser network policy before request bytes are received."""
        from urllib.parse import urlsplit
        url = str(request.url or "")
        resource_type = str(getattr(request, "resource_type", "") or "")
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            await route.abort("blockedbyclient")
            return

        scheme = parsed.scheme.lower()
        is_http = scheme in {"http", "https"}
        is_websocket = scheme in {"ws", "wss"}
        if is_http or is_websocket:
            from actions.playwright_mcp_client import validate_browser_url
            validation_url = url
            if is_websocket:
                suffix = url[url.find(":") + 1:]
                validation_url = ("https:" if scheme == "wss" else "http:") + suffix
            try:
                validate_browser_url(validation_url)
            except ValueError:
                await route.abort("blockedbyclient")
                return

            redirected_from = getattr(request, "redirected_from", None)
            if redirected_from is not None:
                previous = urlsplit(str(getattr(redirected_from, "url", "") or ""))
                previous_host = (previous.hostname or "").rstrip(".").lower()
                current_host = (parsed.hostname or "").rstrip(".").lower()
                previous_port = previous.port or (443 if previous.scheme in {"https", "wss"} else 80)
                current_port = parsed.port or (443 if scheme in {"https", "wss"} else 80)
                previous_scheme = previous.scheme.lower()
                if (
                    previous_host and current_host
                    and (
                        previous_host != current_host
                        or previous_port != current_port
                        or previous_scheme != scheme
                    )
                ):
                    await route.abort("blockedbyclient")
                    return
        elif resource_type == "document":
            # Top-level navigation to non-http(s) schemes is never part of the
            # public browser contract; reject before page content becomes visible.
            from actions.playwright_mcp_client import validate_browser_url
            try:
                validate_browser_url(url)
            except ValueError:
                await route.abort("blockedbyclient")
                return
        elif parsed.scheme.lower() in {"file", "javascript", "vbscript", "chrome", "chrome-extension", "edge", "ms-browser-extension"}:
            await route.abort("blockedbyclient")
            return

        try:
            await route.continue_()
        except Exception:
            try:
                await route.abort("failed")
            except Exception:
                pass

    def _record_page(self, page) -> None:
        try:
            page.on("request", lambda req: self._network_log.append(
                f"{getattr(req, 'method', 'GET')} {getattr(req, 'resource_type', '')} {str(getattr(req, 'url', ''))[:500]}"
            ))
            page.on("console", lambda msg: self._console_log.append(
                f"{getattr(msg, 'type', '')}: {str(getattr(msg, 'text', ''))[:1000]}"
            ))
            self._network_log = self._network_log[-200:]
            self._console_log = self._console_log[-200:]
        except Exception:
            pass

    async def _new_tab(self, url: str | None = None) -> str:
        await self._launch_browser_if_needed()
        if self._context is None:
            self._context = await self._browser.new_context(
                viewport=None,
                service_workers="block",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            await self._context.route("**/*", self._guard_request)
        page = await self._context.new_page()
        if page not in self._pages:
            self._pages.append(page)
        self._record_page(page)
        self._page = page
        if url:
            return await self._go_to(url)
        return f"Opened new tab ({len(self._pages)} total)."

    async def _switch_tab(self, index: int = 1) -> str:
        await self._launch_browser_if_needed()
        if not self._pages:
            await self._get_page()
        if not self._pages:
            return "No open tabs."
        idx = max(1, index) - 1
        if idx >= len(self._pages):
            return f"Tab {index} does not exist."
        page = self._pages[idx]
        if page.is_closed():
            return f"Tab {index} is already closed."
        self._page = page
        try:
            await page.bring_to_front()
        except Exception:
            pass
        return f"Switched to tab {index}: {page.url}"

    async def _list_tabs(self) -> str:
        await self._launch_browser_if_needed()
        if not self._pages and self._page:
            self._pages = [self._page]
        if not self._pages:
            return "No tabs open."
        rows = []
        for i, page in enumerate(self._pages, 1):
            if page.is_closed():
                continue
            title = await page.title() if page.url else ""
            rows.append(f"{i}. {title or 'Untitled'} | {page.url or 'about:blank'}")
        return "\n".join(rows) if rows else "No open tabs."

    async def _back(self) -> str:
        page = await self._get_page()
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=10000)
            return f"Back: {page.url}"
        except Exception as e:
            return f"Back error: {e}"

    async def _forward(self) -> str:
        page = await self._get_page()
        try:
            await page.go_forward(wait_until="domcontentloaded", timeout=10000)
            return f"Forward: {page.url}"
        except Exception as e:
            return f"Forward error: {e}"

    async def _reload(self) -> str:
        page = await self._get_page()
        try:
            await page.reload(wait_until="domcontentloaded", timeout=15000)
            return f"Reloaded: {page.url}"
        except Exception as e:
            return f"Reload error: {e}"

    # ── Eylemler ─────────────────────────────────────────────────────────────

    async def _go_to(self, url: str) -> str:
        from actions.playwright_mcp_client import validate_browser_url
        try:
            url = validate_browser_url(url)
        except ValueError as exc:
            return f"Navigation blocked: {exc}"
        page = await self._get_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            final_url = str(page.url or "")
            if final_url:
                final_parts = urlsplit(final_url)
                requested_parts = urlsplit(url)
                if final_parts.hostname and requested_parts.hostname:
                    requested_host = requested_parts.hostname.rstrip(".").lower()
                    final_host = final_parts.hostname.rstrip(".").lower()
                    if requested_host != final_host:
                        return f"Navigation blocked: redirect changed host from {requested_host} to {final_host}."
                try:
                    validate_browser_url(final_url)
                except ValueError as exc:
                    return f"Navigation blocked: final destination rejected by browser policy: {exc}"
            return f"Opened: {final_url or url}"
        except PlaywrightTimeout:
            return f"Timeout loading: {url}"
        except Exception as e:
            return f"Navigation error: {e}"

    async def _search(self, query: str, engine: str = "google") -> str:
        engines = {
            "google":     f"https://www.google.com/search?q={query.replace(' ', '+')}",
            "bing":       f"https://www.bing.com/search?q={query.replace(' ', '+')}",
            "duckduckgo": f"https://duckduckgo.com/?q={query.replace(' ', '+')}",
        }
        url = engines.get(engine.lower(), engines["google"])
        return await self._go_to(url)

    async def _click(self, selector=None, text=None) -> str:
        page = await self._get_page()
        try:
            if text:
                await page.get_by_text(text, exact=False).first.click(timeout=8000)
                return f"Clicked: '{text}'"
            elif selector:
                await page.click(selector, timeout=8000)
                return f"Clicked: {selector}"
            return "No selector or text provided."
        except PlaywrightTimeout:
            return "Element not found or not clickable."
        except Exception as e:
            return f"Click error: {e}"

    async def _type(self, selector=None, text: str = "", clear_first: bool = True) -> str:
        page = await self._get_page()
        try:
            element = page.locator(selector).first if selector else page.locator(":focus")
            if clear_first:
                await element.clear()
            await element.type(text, delay=50)
            return "Text typed."
        except Exception as e:
            return f"Type error: {e}"

    async def _scroll(self, direction: str = "down", amount: int = 500) -> str:
        page = await self._get_page()
        try:
            y = amount if direction == "down" else -amount
            await page.mouse.wheel(0, y)
            return f"Scrolled {direction}."
        except Exception as e:
            return f"Scroll error: {e}"

    async def _press(self, key: str) -> str:
        page = await self._get_page()
        try:
            await page.keyboard.press(key)
            return f"Pressed: {key}"
        except Exception as e:
            return f"Key error: {e}"

    async def _get_text(self) -> str:
        page = await self._get_page()
        try:
            text = await page.inner_text("body")
            return text[:4000] if len(text) > 4000 else text
        except Exception as e:
            return f"Could not get page text: {e}"

    async def _fill_form(self, fields: dict) -> str:
        page    = await self._get_page()
        results = []
        for selector, value in fields.items():
            try:
                el = page.locator(selector).first
                await el.clear()
                await el.type(str(value), delay=40)
                results.append(f"✓ {selector}")
            except Exception as e:
                results.append(f"✗ {selector}: {e}")
        return "Form filled: " + ", ".join(results)

    async def _smart_click(self, description: str) -> str:
        page       = await self._get_page()
        desc_lower = description.lower()

        role_hints = {
            "button":    ["button", "buton", "btn"],
            "link":      ["link", "bağlantı"],
            "searchbox": ["search", "arama"],
            "textbox":   ["input", "field", "alan"],
        }
        for role, keywords in role_hints.items():
            if any(k in desc_lower for k in keywords):
                try:
                    await page.get_by_role(role).first.click(timeout=5000)
                    return f"Clicked ({role}): '{description}'"
                except Exception:
                    pass

        try:
            await page.get_by_text(description, exact=False).first.click(timeout=5000)
            return f"Clicked (text): '{description}'"
        except Exception:
            pass

        try:
            await page.get_by_placeholder(description, exact=False).first.click(timeout=5000)
            return f"Clicked (placeholder): '{description}'"
        except Exception:
            pass

        return f"Could not find: '{description}'"

    async def _smart_type(self, description: str, text: str) -> str:
        page = await self._get_page()

        for method, locator in [
            ("placeholder", page.get_by_placeholder(description, exact=False)),
            ("label",       page.get_by_label(description, exact=False)),
            ("role",        page.get_by_role("textbox")),
        ]:
            try:
                el = locator.first
                await el.clear()
                await el.type(text, delay=50)
                return f"Typed into ({method}): '{description}'"
            except Exception:
                continue

        return f"Could not find input: '{description}'"

    async def _snapshot(self) -> str:
        page = await self._get_page()
        try:
            text = await page.inner_text("body", timeout=10000)
            return text[:12000] if text else ""
        except Exception as exc:
            return f"Snapshot unavailable: {exc}"

    async def _find(self, text: str) -> str:
        query = str(text or "").strip()
        if not query:
            return "Find requires text."
        page = await self._get_page()
        try:
            count = await page.get_by_text(query, exact=False).count()
            return f"Found {count} matching element(s) for '{query}'."
        except Exception as exc:
            return f"Find error: {exc}"

    async def _hover(self, selector=None, text=None) -> str:
        page = await self._get_page()
        try:
            if text:
                await page.get_by_text(str(text), exact=False).first.hover(timeout=8000)
                return f"Hovered: '{text}'"
            if selector:
                await page.locator(selector).first.hover(timeout=8000)
                return f"Hovered: {selector}"
            return "No selector or text provided."
        except PlaywrightTimeout:
            return "Element not found or not hoverable."
        except Exception as exc:
            return f"Hover error: {exc}"

    async def _select_option(self, values: list[str], selector=None) -> str:
        page = await self._get_page()
        if not selector:
            return "Select option requires a selector."
        try:
            await page.locator(selector).first.select_option(values=values, timeout=8000)
            return f"Selected {len(values)} option(s)."
        except Exception as exc:
            return f"Select option error: {exc}"

    def _safe_user_path(self, raw_path: str | Path) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        home = Path.home().resolve()
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(home)
        except ValueError as exc:
            raise ValueError("Browser file paths must stay inside the user's home directory.") from exc
        current = Path(path.anchor) if path.anchor else Path(".")
        parts = path.parts[1:] if path.anchor else path.parts
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Browser file paths may not contain symlinked components.")
            is_junction = getattr(current, "is_junction", None)
            if is_junction is not None and is_junction():
                raise ValueError("Browser file paths may not contain junctions.")
        return resolved

    async def _screenshot(self, output_path: str) -> str:
        import os
        import tempfile
        target = self._safe_user_path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or (getattr(target, "is_junction", lambda: False)()):
            return "Screenshot blocked: destination is a link/reparse point."
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        os.close(fd)
        try:
            page = await self._get_page()
            await page.screenshot(path=tmp_name)
            os.replace(tmp_name, target)
            return f"Screenshot saved: {target}"
        except Exception as exc:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
            return f"Screenshot failed: {exc}"

    async def _wait_for(self, text=None, time_ms=2000) -> str:
        page = await self._get_page()
        try:
            if text:
                await page.get_by_text(str(text), exact=False).first.wait_for(timeout=max(1, int(time_ms)))
                return f"Found: {text}"
            await asyncio.sleep(max(0, int(time_ms)) / 1000)
            return f"Waited {int(time_ms)} ms."
        except PlaywrightTimeout:
            return f"Timed out waiting for: {text}"
        except Exception as exc:
            return f"Wait error: {exc}"

    async def _handle_dialog(self, accept: bool = True, prompt_text=None) -> str:
        page = await self._get_page()
        async def _handler(dialog):
            try:
                if accept:
                    await dialog.accept(prompt_text)
                else:
                    await dialog.dismiss()
            except Exception:
                pass
        try:
            page.once("dialog", _handler)
            return "Dialog handler armed for the next browser dialog."
        except Exception as exc:
            return f"Dialog handler could not be armed: {exc}"

    async def _file_upload(self, paths: list[str], selector=None) -> str:
        if not paths:
            return "File upload requires at least one path."
        if not selector:
            return "File upload requires a selector."
        safe_paths = []
        try:
            for raw in paths:
                candidate = self._safe_user_path(raw)
                if not candidate.exists() or not candidate.is_file() or candidate.is_symlink():
                    return f"File upload blocked: unsafe or missing file '{raw}'."
                safe_paths.append(str(candidate))
        except Exception as exc:
            return f"File upload blocked: {exc}"
        try:
            page = await self._get_page()
            await page.locator(selector).first.set_input_files(safe_paths, timeout=10000)
            return f"Selected {len(safe_paths)} file(s) for upload."
        except Exception as exc:
            return f"File upload failed: {exc}"

    async def _console_messages(self) -> str:
        return "\n".join(self._console_log[-100:]) or "No console messages recorded."

    async def _network_requests(self) -> str:
        return "\n".join(self._network_log[-100:]) or "No network requests recorded."

    async def _shutdown_resources(self) -> None:
        await self._close_browser()
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def _close_browser(self) -> str:
        if self._browser:
            await self._browser.close()
            self._browser = None
            if self._pinned_proxy is not None:
                self._pinned_proxy.close()
                self._pinned_proxy = None
            self._context = None
            self._page    = None
            self._pages   = []
            self._network_log = []
            self._console_log = []

        return "Browser closed."


# ── Singleton browser thread ─────────────────────────────────────────────────

_bt         = _BrowserThread()
_bt_started = False
_bt_lock    = threading.Lock()


def shutdown_browser() -> None:
    global _bt_started
    with _bt_lock:
        thread = _bt._thread
        if _bt._loop and thread and thread.is_alive():
            try:
                future = asyncio.run_coroutine_threadsafe(_bt._shutdown_resources(), _bt._loop)
                future.result(timeout=10)
            except Exception as exc:
                _log(f"[Browser] shutdown failed: {exc}")
            try:
                _bt._loop.call_soon_threadsafe(_bt._loop.stop)
            except Exception:
                pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=10)
        _bt._thread = None
        _bt._loop = None
        _bt._playwright = None
        _bt._browser = None
        _bt._context = None
        _bt._page = None
        _bt._pages = []
        _bt._startup_error = None
        _bt_started = False


atexit.register(shutdown_browser)


def browser_evaluate_internal(expression: str) -> str:
    """Evaluate fixed, first-party browser logic outside the public action router."""
    expr = str(expression or "").strip()
    if not expr:
        raise ValueError("Browser evaluation expression cannot be empty.")
    try:
        from actions.playwright_mcp_client import get_playwright_mcp_client
        mcp = get_playwright_mcp_client()
        result = mcp.evaluate(expr)
        if result and not str(result).startswith("Tool '"):
            return str(result)
    except Exception as exc:
        _log(f"[Browser/Internal] MCP evaluate unavailable ({exc}) — using native browser")
    _ensure_started()
    return _bt.run(_bt._evaluate(expr))


def _ensure_started():
    global _bt_started
    with _bt_lock:
        if _bt_started and _bt._thread and _bt._thread.is_alive() and _bt._playwright is not None:
            return
        if not _bt.start():
            _bt_started = False
            detail = f": {_bt._startup_error}" if _bt._startup_error else ""
            raise RuntimeError(f"Browser backend could not start{detail}")
        _bt_started = True


# ── Public API ───────────────────────────────────────────────────────────────

def browser_control(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None
) -> str:
    """
    Complete browser automation powered by Microsoft Playwright MCP (@playwright/mcp),
    with automatic failover to the built-in Playwright thread.

    parameters:
        action      : go_to | navigate | search | click | type | scroll | fill_form |
                      smart_click | smart_type | get_text | press | back | forward |
                      refresh | open_tab | new_tab | switch_tab | list_tabs | close |
                      snapshot | find | hover | screenshot |
                      wait_for | select_option | dialog | upload | console | network
        url         : URL for go_to / navigate
        query       : search query
        engine      : google | bing | duckduckgo (default: google)
        selector    : CSS selector for click/type/hover/select
        element     : Snapshot element reference (e.g. 'e2')
        text        : text to click, type, or wait for
        description : element description for smart_click/smart_type
        direction   : up | down for scroll
        amount      : scroll amount in pixels (default: 500)
        key         : key name for press (e.g. Enter, Escape, Tab, Backspace)
        fields      : {selector: value} dict or list of fields for fill_form
        path        : output path for screenshot
        tab         : 1-based tab index for switch_tab
        time_ms     : milliseconds to wait
    """
    import time
    from actions.playwright_mcp_client import get_playwright_mcp_client, validate_browser_url

    action = (parameters or {}).get("action", "").lower().strip()
    result = "Unknown action."

    # High-level JEV Ultrafast path. It is intentionally opt-in through the
    # caller's action and credentials; every unsupported/failed run falls back
    # to the existing browser stack below.
    if action in {"goal", "agent", "ultrafast"}:
        try:
            from core.jev_browser import run_goal
            url = str(parameters.get("url") or "").strip()
            goal = str(parameters.get("goal") or parameters.get("query") or "").strip()
            result = run_goal(url, goal, max_steps=int(parameters.get("max_steps", 60)))
            safe = str(result).encode("ascii", "replace").decode("ascii")
            _log(f"[Browser/Jev] {safe[:100]}")
            if player and hasattr(player, "write_log"):
                player.write_log(f"[browser/jev] {safe[:80]}")
            return result
        except Exception as jev_err:
            _log(f"[Browser/Jev] unavailable ({jev_err}) — using existing browser stack")

    # Arbitrary browser JavaScript/code is intentionally not exposed through the
    # planner-facing action dispatcher. Trusted internal callers use
    # browser_evaluate_internal() instead.
    if action in {"evaluate", "eval", "run_code", "execute"}:
        return "Unsupported browser action: arbitrary code execution is not exposed through the public browser-control contract."

    # Use Brahma's guarded native Playwright backend for all public browser actions.
    # The upstream MCP server cannot enforce redirect/network security boundaries.
    try:
        _ensure_started()
        if action in {"go_to", "navigate"}:
            url = str(parameters.get("url", "") or "").strip()
            if not url and parameters.get("query"):
                action = "search"
            else:
                result = _bt.run(_bt._go_to(url))
        if action == "search":
            result = _bt.run(_bt._search(parameters.get("query", ""), parameters.get("engine", "google")))
        elif action in {"click", "smart_click"}:
            result = _bt.run(_bt._smart_click(parameters.get("description") or parameters.get("text") or "")) if not parameters.get("selector") else _bt.run(_bt._click(selector=parameters.get("selector")))
        elif action in {"hover", "smart_hover"}:
            result = _bt.run(_bt._hover(selector=parameters.get("selector"), text=parameters.get("text") or parameters.get("description")))
        elif action in {"type", "smart_type"}:
            if action == "smart_type" or parameters.get("description"):
                result = _bt.run(_bt._smart_type(parameters.get("description") or parameters.get("text", ""), parameters.get("text", "")))
            else:
                result = _bt.run(_bt._type(selector=parameters.get("selector"), text=parameters.get("text", "")))
        elif action == "press":
            result = _bt.run(_bt._press(parameters.get("key", "Enter")))
        elif action == "scroll":
            result = _bt.run(_bt._scroll(direction=parameters.get("direction", "down"), amount=int(parameters.get("amount", 500))))
        elif action in {"snapshot", "inspect"}:
            result = _bt.run(_bt._snapshot())
        elif action == "find":
            result = _bt.run(_bt._find(parameters.get("query") or parameters.get("text") or ""))
        elif action == "get_text":
            result = _bt.run(_bt._get_text())
        elif action in {"screenshot", "take_screenshot"}:
            out_dir = Path.home() / "Desktop" / "BrahmaAI"
            out_dir.mkdir(parents=True, exist_ok=True)
            custom_path = parameters.get("path") or str(out_dir / f"browser_screenshot_{int(time.time())}.png")
            result = _bt.run(_bt._screenshot(custom_path))
        elif action == "fill_form":
            result = _bt.run(_bt._fill_form(parameters.get("fields", {})))
        elif action == "select_option":
            vals = parameters.get("values") or [parameters.get("value")]
            result = _bt.run(_bt._select_option([str(v) for v in vals if v], selector=parameters.get("selector")))
        elif action in {"tabs", "list_tabs"}:
            result = _bt.run(_bt._list_tabs())
        elif action in {"open_tab", "new_tab"}:
            result = _bt.run(_bt._new_tab(parameters.get("url")))
        elif action == "switch_tab":
            result = _bt.run(_bt._switch_tab(int(parameters.get("tab", 1))))
        elif action == "back":
            result = _bt.run(_bt._back())
        elif action == "forward":
            result = _bt.run(_bt._forward())
        elif action in {"refresh", "reload"}:
            result = _bt.run(_bt._reload())
        elif action in {"wait_for", "wait"}:
            text = parameters.get("text")
            t_ms = parameters.get("time_ms") or parameters.get("time")
            result = _bt.run(_bt._wait_for(text=text, time_ms=int(t_ms) if t_ms else 2000))
        elif action in {"dialog", "handle_dialog"}:
            result = _bt.run(_bt._handle_dialog(
                accept=bool(parameters.get("accept", True)),
                prompt_text=parameters.get("prompt_text"),
            ))
        elif action in {"upload", "file_upload"}:
            paths = [str(p) for p in (parameters.get("paths") or [parameters.get("path")]) if p]
            result = _bt.run(_bt._file_upload(paths, selector=parameters.get("selector")))
        elif action == "console":
            result = _bt.run(_bt._console_messages())
        elif action == "network":
            result = _bt.run(_bt._network_requests())
        elif action == "close":
            result = _bt.run(_bt._close_browser())
    except Exception as browser_err:
        result = f"Browser error: {browser_err}"
    # Safe log printing without Windows charmap crashes
    safe_res = str(result).encode("ascii", "replace").decode("ascii")
    _log(f"[Browser] {safe_res[:100]}")
    if player and hasattr(player, "write_log"):
        player.write_log(f"[browser] {safe_res[:80]}")

    return result

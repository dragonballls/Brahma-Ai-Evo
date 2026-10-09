"""Accounts & Integrations settings page for the shared secure registry."""
from __future__ import annotations

import json
import hmac
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from PyQt6.QtCore import QThread, QTimer, Qt, pyqtSignal, QUrl
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.account_integrations import (
    ConnectionStatus,
    GitHubConnector,
    RobloxConnector,
    IntegrationError,
    IntegrationManager,
    get_default_manager,
)
from core.custom_account_integrations_ui import CustomProvidersWidget


class _LoopbackOAuthCallback:
    """Small loopback-only callback receiver with strict path and OAuth-state checks."""

    def __init__(self, redirect_uri: str, expected_state: str):
        parsed = RobloxConnector._redirect_parts(redirect_uri)
        self.redirect_uri = redirect_uri
        self.expected_state = str(expected_state)
        self._result: dict[str, str] | None = None
        self._lock = threading.Lock()
        self.event = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                # Authorization query values must never be written to the app log.
                return

            def _reply(self, status: int, message: str):
                body = (
                    "<!doctype html><html><head><meta charset=utf-8>"
                    "<meta http-equiv=Content-Security-Policy content=\"default-src 'none'; style-src 'unsafe-inline'\">"
                    "<meta name=viewport content=\"width=device-width,initial-scale=1\"></head>"
                    "<body style=\"font-family:Segoe UI,sans-serif;padding:2rem\">"
                    "<h2>Roblox authorization</h2><p>" + message +
                    "</p><p>You may return to Brahma Evo now.</p></body></html>"
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Pragma", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed_path = urllib.parse.urlparse(self.path)
                if parsed_path.path != parsed.path:
                    self._reply(404, "This callback path is not registered.")
                    return
                expected_host = f"{parsed.hostname}:{parsed.port}"
                if self.headers.get("Host", "") != expected_host:
                    self._reply(400, "This callback host was not expected.")
                    return
                try:
                    query = urllib.parse.parse_qs(parsed_path.query, keep_blank_values=True, max_num_fields=20)
                except ValueError:
                    self._reply(400, "The authorization response was malformed.")
                    return
                states = query.get("state") or []
                if len(states) != 1 or not hmac.compare_digest(str(states[0]), outer.expected_state):
                    # Do not let a random local process cancel the waiting flow by
                    # sending a forged callback with a different state.
                    self._reply(400, "The authorization response did not match this request.")
                    return
                errors = query.get("error") or []
                codes = query.get("code") or []
                with outer._lock:
                    if errors:
                        outer._result = {"error": "access_denied" if errors[0] == "access_denied" else "provider_error"}
                    elif len(codes) == 1 and codes[0] and len(codes[0]) <= 2048:
                        outer._result = {"callback_url": outer.redirect_uri + "?" + parsed_path.query}
                    else:
                        outer._result = {"error": "missing_code"}
                    outer.event.set()
                accepted = bool(outer._result and outer._result.get("callback_url"))
                self._reply(200 if accepted else 400, "Authorization was received." if accepted else "Authorization was not completed.")

        self.server = ThreadingHTTPServer((parsed.hostname, parsed.port), Handler)
        self.server.daemon_threads = True
        self.server.timeout = 0.5
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="BrahmaRobloxOAuthCallback")
        self.thread.start()

    def result(self) -> dict[str, str] | None:
        with self._lock:
            return dict(self._result) if self._result else None

    def close(self):
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass
        if self.thread.is_alive():
            self.thread.join(timeout=1.0)


class _AccountWorker(QThread):
    completed = pyqtSignal(object)

    def __init__(
        self,
        command: str,
        manager: IntegrationManager,
        connector: Any,
        *,
        client_id: str = "",
        client_secret: str = "",
        flow: dict[str, Any] | None = None,
        callback_url: str = "",
        account_id: str = "",
        action: str = "",
    ):
        super().__init__()
        self.command = command
        self.manager = manager
        self.connector = connector
        self.client_id = client_id
        self.client_secret = client_secret
        self.flow = flow
        self.callback_url = callback_url
        self.account_id = account_id
        self.action = action

    def run(self):
        try:
            if self.command == "start":
                flow = self.connector.begin_device_authorization(self.client_id)
                self.completed.emit({"ok": True, "kind": "flow_started", "flow": flow})
                return
            if self.command == "poll":
                flow = dict(self.flow or {})
                outcome = self.connector.poll_device_authorization(flow)
                if outcome.get("pending"):
                    self.completed.emit({"ok": True, "kind": "flow_pending", "flow": flow, "retry_after": outcome.get("retry_after", 5)})
                    return
                summary = self.manager.connect("github", outcome.get("credentials") or {})
                self.completed.emit({"ok": True, "kind": "connected", "provider_id": "github", "account_id": summary.account_id, "identity": summary.identity})
                return
            if self.command == "roblox_exchange":
                credentials = self.connector.complete_authorization(dict(self.flow or {}), self.callback_url)
                summary = self.manager.connect("roblox", credentials)
                self.completed.emit({"ok": True, "kind": "connected", "provider_id": "roblox", "account_id": summary.account_id, "identity": summary.identity})
                return
            if self.command == "test":
                self.completed.emit({"ok": True, "kind": "test", "data": self.manager.test_connection(self.account_id)})
                return
            if self.command == "profile":
                account = next((item for item in self.manager.list_accounts() if item.account_id == self.account_id), None)
                if account is None:
                    self.completed.emit({"ok": False, "error_code": "account_not_found", "message": "The connected account was not found."})
                    return
                operation = "roblox.profile.read" if account.provider_id == "roblox" else "github.profile.read"
                result = self.manager.execute(self.account_id, operation, {})
                self.completed.emit({"ok": True, "kind": "action", "data": result.to_dict()})
                return
            if self.command == "repositories":
                result = self.manager.execute(self.account_id, "github.repositories.public.list", {})
                self.completed.emit({"ok": True, "kind": "action", "data": result.to_dict()})
                return
            if self.command == "refresh":
                account = self.manager.refresh_authorization(self.account_id)
                self.completed.emit({"ok": True, "kind": "refreshed", "identity": account.identity, "provider_id": account.provider_id})
                return
            if self.command == "disconnect":
                self.completed.emit({"ok": True, "kind": "disconnected", "data": self.manager.disconnect(self.account_id)})
                return
            self.completed.emit({"ok": False, "message": "Unknown account action."})
        except IntegrationError as exc:
            self.completed.emit({"ok": False, "error_code": exc.code.value, "message": str(exc)})
        except Exception:
            self.completed.emit({"ok": False, "error_code": "unexpected_error", "message": "The operation failed. Provider details and credentials were not exposed."})


class AccountsIntegrationsPage(QWidget):
    """One account view with truthful provider support and authorization state."""

    def __init__(self, manager: IntegrationManager | None = None, parent=None):
        super().__init__(parent)
        self.manager = manager or get_default_manager()
        adapter = self.manager.connector("github")
        self.github = adapter if isinstance(adapter, GitHubConnector) else GitHubConnector()
        roblox_adapter = self.manager.connector("roblox")
        self.roblox = roblox_adapter if isinstance(roblox_adapter, RobloxConnector) else None
        self._flow: dict[str, Any] | None = None
        self._roblox_flow: dict[str, Any] | None = None
        self._roblox_listener: _LoopbackOAuthCallback | None = None
        self._worker: _AccountWorker | None = None
        self._selected_account_id = ""
        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(True)
        self._poll_timer.timeout.connect(self._poll_authorization)
        self._roblox_callback_timer = QTimer(self)
        self._roblox_callback_timer.setInterval(250)
        self._roblox_callback_timer.timeout.connect(self._check_roblox_callback)

        self.setObjectName("AccountsIntegrationsPage")
        self.setStyleSheet("""
            QWidget#AccountsIntegrationsPage { background: transparent; color: #f1f4f8; }
            QFrame#AccountIntegrationCard {
                background: rgba(10, 12, 18, 190);
                border: 1px solid rgba(0, 229, 255, 0.19);
                border-radius: 14px;
            }
            QLabel { background: transparent; }
            QLineEdit {
                background: rgba(13, 15, 19, 240);
                color: #f1f4f8;
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 8px;
                padding: 8px;
                min-height: 25px;
            }
            QPushButton {
                background: rgba(0, 229, 255, 0.08);
                color: #f1f4f8;
                border: 1px solid rgba(0, 229, 255, 0.25);
                border-radius: 8px;
                padding: 8px 11px;
            }
            QPushButton:hover { background: rgba(0, 229, 255, 0.16); }
            QPushButton:disabled { color: #88919d; border-color: rgba(255,255,255,0.08); }
            QListWidget {
                background: rgba(13, 15, 19, 220);
                color: #f1f4f8;
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 8px;
                padding: 4px;
            }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        root.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)

        title = QLabel("Accounts & Integrations")
        title.setFont(QFont("Segoe UI", 24, QFont.Weight.Bold))
        layout.addWidget(title)
        intro = QLabel(
            "Connect accounts using official authorization flows. Brahma only shows actions "
            "declared by a provider adapter, and a task is not marked successful unless the "
            "provider response supplies verification evidence. Unsupported providers stay clearly labelled."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        github_card = self._card()
        gh_layout = QVBoxLayout(github_card)
        gh_layout.setContentsMargins(14, 14, 14, 14)
        gh_layout.setSpacing(8)
        gh_title = QLabel("GitHub — OAuth device authorization")
        gh_title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        gh_layout.addWidget(gh_title)
        help_text = QLabel(
            "Requires a GitHub OAuth App client ID with Device Flow enabled. Brahma requests "
            "only read:user. The connected capabilities are profile read, connection test, and "
            "public repositories list; private repository access and writes are not enabled here."
        )
        help_text.setWordWrap(True)
        gh_layout.addWidget(help_text)

        self._client_id = QLineEdit()
        self._client_id.setPlaceholderText("GitHub OAuth App client ID (public identifier)")
        self._client_secret = QLineEdit()
        self._client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._client_secret.setPlaceholderText("Optional OAuth App client secret (stored only with the account in Credential Manager)")
        gh_layout.addWidget(self._client_id)
        gh_layout.addWidget(self._client_secret)
        connect_row = QHBoxLayout()
        self._connect_btn = QPushButton("Connect GitHub")
        self._connect_btn.clicked.connect(self._start_authorization)
        connect_row.addWidget(self._connect_btn)
        self._open_device_btn = QPushButton("Open GitHub authorization page")
        self._open_device_btn.clicked.connect(self._open_authorization_page)
        self._open_device_btn.setEnabled(False)
        connect_row.addWidget(self._open_device_btn)
        self._cancel_btn = QPushButton("Cancel authorization")
        self._cancel_btn.clicked.connect(self._cancel_authorization)
        self._cancel_btn.setEnabled(False)
        connect_row.addWidget(self._cancel_btn)
        gh_layout.addLayout(connect_row)

        self._code_label = QLabel("Authorization code: not started")
        self._code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        gh_layout.addWidget(self._code_label)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        gh_layout.addWidget(self._status)
        layout.addWidget(github_card)

        # Roblox uses its official OAuth authorization-code flow with PKCE.
        roblox_card = self._card()
        roblox_layout = QVBoxLayout(roblox_card)
        roblox_layout.setContentsMargins(14, 14, 14, 14)
        roblox_layout.setSpacing(8)
        roblox_title = QLabel("Roblox — OAuth 2.0 / PKCE")
        roblox_title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        roblox_layout.addWidget(roblox_title)
        roblox_help = QLabel(
            "Supports verified account identity and basic profile reads only. Create a Roblox OAuth app "
            "with the openid and profile scopes, and register the exact loopback Redirect URI shown below. "
            "Roblox labels its OAuth API as beta; game automation and private account changes are not included."
        )
        roblox_help.setWordWrap(True)
        roblox_layout.addWidget(roblox_help)
        self._roblox_client_id = QLineEdit()
        self._roblox_client_id.setPlaceholderText("Roblox OAuth client ID")
        self._roblox_client_secret = QLineEdit()
        self._roblox_client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._roblox_client_secret.setPlaceholderText("Roblox OAuth client secret (stored only in Credential Manager after validation)")
        self._roblox_redirect_uri = QLineEdit("http://127.0.0.1:8765/roblox/callback")
        self._roblox_redirect_uri.setToolTip("Must exactly match the loopback Redirect URI registered in your Roblox OAuth app.")
        roblox_layout.addWidget(self._roblox_client_id)
        roblox_layout.addWidget(self._roblox_client_secret)
        roblox_layout.addWidget(self._roblox_redirect_uri)
        roblox_row = QHBoxLayout()
        self._roblox_connect_btn = QPushButton("Connect Roblox")
        self._roblox_connect_btn.clicked.connect(self._start_roblox_authorization)
        self._roblox_cancel_btn = QPushButton("Cancel Roblox authorization")
        self._roblox_cancel_btn.clicked.connect(self._cancel_roblox_authorization)
        self._roblox_cancel_btn.setEnabled(False)
        roblox_row.addWidget(self._roblox_connect_btn)
        roblox_row.addWidget(self._roblox_cancel_btn)
        roblox_layout.addLayout(roblox_row)
        self._roblox_status = QLabel("Status: not connected")
        self._roblox_status.setWordWrap(True)
        roblox_layout.addWidget(self._roblox_status)
        if self.roblox is None:
            self._roblox_connect_btn.setEnabled(False)
            self._roblox_status.setText("Status: unavailable — no Roblox adapter is registered.")
        layout.addWidget(roblox_card)

        # Custom provider onboarding uses the same secure store and capability manager.
        self._custom_providers = CustomProvidersWidget(self.manager, self)
        self._custom_providers.accountsChanged.connect(self.refresh)
        layout.addWidget(self._custom_providers)

        account_card = self._card()
        account_layout = QVBoxLayout(account_card)
        account_layout.setContentsMargins(14, 14, 14, 14)
        account_layout.setSpacing(8)
        account_title = QLabel("Connected accounts")
        account_title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        account_layout.addWidget(account_title)
        self._accounts = QListWidget()
        self._accounts.currentItemChanged.connect(self._account_selected)
        account_layout.addWidget(self._accounts)
        action_row = QHBoxLayout()
        self._refresh_btn = QPushButton("Refresh list")
        self._refresh_btn.clicked.connect(self.refresh)
        action_row.addWidget(self._refresh_btn)
        self._test_btn = QPushButton("Test connection")
        self._test_btn.clicked.connect(lambda: self._run_account_action("test"))
        action_row.addWidget(self._test_btn)
        self._refresh_auth_btn = QPushButton("Refresh authorization")
        self._refresh_auth_btn.clicked.connect(lambda: self._run_account_action("refresh"))
        action_row.addWidget(self._refresh_auth_btn)
        self._profile_btn = QPushButton("Read profile")
        self._profile_btn.clicked.connect(lambda: self._run_account_action("profile"))
        action_row.addWidget(self._profile_btn)
        self._repos_btn = QPushButton("List public repositories")
        self._repos_btn.clicked.connect(lambda: self._run_account_action("repositories"))
        action_row.addWidget(self._repos_btn)
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.clicked.connect(self._confirm_disconnect)
        action_row.addWidget(self._disconnect_btn)
        account_layout.addLayout(action_row)
        self._action_output = QLabel("Select a connected account to run its declared read-only operations.")
        self._action_output.setWordWrap(True)
        self._action_output.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        account_layout.addWidget(self._action_output)
        layout.addWidget(account_card)

        catalog_card = self._card()
        catalog_layout = QVBoxLayout(catalog_card)
        catalog_layout.setContentsMargins(14, 14, 14, 14)
        catalog_layout.setSpacing(8)
        catalog_title = QLabel("Provider catalog and limitations")
        catalog_title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        catalog_layout.addWidget(catalog_title)
        self._catalog = QLabel("")
        self._catalog.setWordWrap(True)
        catalog_layout.addWidget(self._catalog)
        layout.addWidget(catalog_card)

        activity_card = self._card()
        activity_layout = QVBoxLayout(activity_card)
        activity_layout.setContentsMargins(14, 14, 14, 14)
        activity_title = QLabel("Recent activity")
        activity_title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        activity_layout.addWidget(activity_title)
        self._activity = QLabel("No actions recorded in this session.")
        self._activity.setWordWrap(True)
        activity_layout.addWidget(self._activity)
        layout.addWidget(activity_card)
        layout.addStretch(1)

        self._set_account_actions_enabled(False)
        self.refresh()

    @staticmethod
    def _card() -> QFrame:
        frame = QFrame()
        frame.setObjectName("AccountIntegrationCard")
        return frame

    def _set_account_actions_enabled(self, enabled: bool):
        try:
            account = next((item for item in self.manager.list_accounts() if item.account_id == self._selected_account_id), None) if self._selected_account_id else None
        except IntegrationError:
            account = None
        provider_id = account.provider_id if account else ""
        self._test_btn.setEnabled(bool(enabled and account))
        self._disconnect_btn.setEnabled(bool(enabled and account))
        self._profile_btn.setEnabled(bool(enabled and account and provider_id in ("github", "roblox")))
        self._repos_btn.setEnabled(bool(enabled and account and provider_id == "github"))
        adapter = self.manager.connector(provider_id) if provider_id else None
        self._refresh_auth_btn.setEnabled(bool(enabled and adapter and callable(getattr(adapter, "refresh_credentials", None))))

    def refresh(self):
        if not self.manager.store.available:
            self._status.setText(
                "Secure account storage is unavailable. No account token will be stored. "
                "On Windows, enable Windows Credential Manager support and restart Brahma."
            )
            self._connect_btn.setEnabled(False)
            if hasattr(self, "_roblox_connect_btn"):
                self._roblox_connect_btn.setEnabled(False)
        else:
            idle = self._worker is None and self._flow is None and self._roblox_flow is None
            self._connect_btn.setEnabled(idle)
            if hasattr(self, "_roblox_connect_btn"):
                self._roblox_connect_btn.setEnabled(idle and self.roblox is not None)
            self._status.setText("Secure account storage is ready. An account is connected only after the provider validates its identity.")
        try:
            accounts = self.manager.list_accounts()
        except IntegrationError as exc:
            accounts = []
            self._status.setText(str(exc))
        selected = self._selected_account_id
        self._accounts.blockSignals(True)
        self._accounts.clear()
        for account in accounts:
            item = QListWidgetItem(
                f"{account.identity}  •  {account.provider_id}  •  {account.status.value}"
            )
            item.setData(Qt.ItemDataRole.UserRole, account.account_id)
            item.setToolTip(f"Granted permissions: {', '.join(account.scopes) or 'none reported'}")
            self._accounts.addItem(item)
            if account.account_id == selected:
                self._accounts.setCurrentItem(item)
        self._accounts.blockSignals(False)
        if self._accounts.currentItem() is None and self._accounts.count():
            self._accounts.setCurrentRow(0)
        self._account_selected(self._accounts.currentItem(), None)
        lines = []
        for manifest in self.manager.catalog():
            lines.append(f"{manifest.display_name} — {manifest.status.value}: {manifest.status_detail}")
        self._catalog.setText("\n\n".join(lines))
        history = self.manager.activity_history()
        if history:
            recent = history[-12:][::-1]
            self._activity.setText("\n".join(
                f"{item.get('provider_id')} · {item.get('action')} · {item.get('status')}"
                for item in recent
            ))
        else:
            self._activity.setText("No actions recorded in this session.")
    
    def _account_selected(self, current, _previous):
        self._selected_account_id = str(current.data(Qt.ItemDataRole.UserRole)) if current else ""
        self._set_account_actions_enabled(bool(self._selected_account_id) and self._worker is None)

    def _start_roblox_authorization(self):
        if self.roblox is None:
            self._roblox_status.setText("Roblox adapter is unavailable in this build.")
            return
        if self._worker is not None or self._flow is not None or self._roblox_flow is not None:
            self._roblox_status.setText("Finish or cancel the current authorization before starting another.")
            return
        if not self.manager.store.available:
            self._roblox_status.setText("Secure Windows Credential Manager storage is unavailable; no token will be saved.")
            return
        client_id = self._roblox_client_id.text().strip()
        client_secret = self._roblox_client_secret.text()
        redirect_uri = self._roblox_redirect_uri.text().strip()
        if not client_id or not client_secret:
            QMessageBox.information(self, "Roblox OAuth setup required", "Enter the Client ID and Client Secret from your Roblox OAuth app. The app must grant openid and profile scopes and register the exact loopback Redirect URI shown in this form.")
            return
        try:
            flow = self.roblox.begin_authorization(client_id, client_secret, redirect_uri)
            listener = _LoopbackOAuthCallback(redirect_uri, flow["state"])
        except IntegrationError as exc:
            self._roblox_status.setText(f"{exc.code.value}: {exc}")
            return
        except OSError:
            self._roblox_status.setText("The local callback port is unavailable. Close the app using 127.0.0.1:8765 or register and enter a different supported loopback port.")
            return
        self._roblox_flow = flow
        self._roblox_listener = listener
        if not QDesktopServices.openUrl(QUrl(flow["authorization_url"])):
            self._cancel_roblox_authorization()
            self._roblox_status.setText("The authorization browser could not be opened. No credentials were stored.")
            return
        self._roblox_status.setText("Waiting for Roblox authorization in the browser. The callback listener is bound only to 127.0.0.1 and validates the OAuth state.")
        self._roblox_cancel_btn.setEnabled(True)
        self._roblox_connect_btn.setEnabled(False)
        self._connect_btn.setEnabled(False)
        self._roblox_callback_timer.start()

    def _check_roblox_callback(self):
        if not self._roblox_flow or not self._roblox_listener:
            self._roblox_callback_timer.stop()
            return
        if time.time() >= float(self._roblox_flow.get("expires_at") or 0):
            self._cancel_roblox_authorization()
            self._roblox_status.setText("Roblox authorization expired. Start a new connection.")
            return
        if not self._roblox_listener.event.is_set():
            return
        listener, self._roblox_listener = self._roblox_listener, None
        result = listener.result() or {"error": "missing_callback"}
        listener.close()
        self._roblox_callback_timer.stop()
        if result.get("error"):
            self._roblox_flow = None
            self._roblox_cancel_btn.setEnabled(False)
            self._roblox_status.setText("Roblox authorization was not completed. Cancelled or invalid callbacks were discarded; start a new connection.")
            self.refresh()
            return
        flow, self._roblox_flow = self._roblox_flow, None
        self._roblox_cancel_btn.setEnabled(False)
        self._roblox_status.setText("Roblox authorization received; validating the token and account identity securely.")
        self._launch_worker("roblox_exchange", connector=self.roblox, flow=flow, callback_url=result["callback_url"])

    def _cancel_roblox_authorization(self):
        self._roblox_callback_timer.stop()
        listener, self._roblox_listener = self._roblox_listener, None
        if listener is not None:
            listener.close()
        self._roblox_flow = None
        self._roblox_cancel_btn.setEnabled(False)
        self._roblox_status.setText("Roblox authorization cancelled locally. No token was saved.")
        idle = self._worker is None and self._flow is None
        self._roblox_connect_btn.setEnabled(bool(idle and self.manager.store.available and self.roblox is not None))
        self._connect_btn.setEnabled(bool(idle and self.manager.store.available))

    def _start_authorization(self):
        client_id = self._client_id.text().strip()
        if not client_id:
            QMessageBox.information(self, "GitHub OAuth setup required",
                "Create or configure a GitHub OAuth App, enable Device Flow, then enter its public client ID here. Do not paste account passwords or authentication codes into chat.")
            return
        if not self.manager.store.available:
            self.refresh()
            return
        self._code_label.setText("Authorization code: requesting from GitHub…")
        self._launch_worker("start", client_id=client_id, client_secret=self._client_secret.text())

    def _open_authorization_page(self):
        if self._flow:
            # This is the fixed official URL, never a URL supplied by a provider response.
            QDesktopServices.openUrl(QUrl("https://github.com/login/device"))

    def _cancel_authorization(self):
        self._poll_timer.stop()
        self._flow = None
        self._code_label.setText("Authorization cancelled locally; no further polling will occur.")
        self._open_device_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)
        self._connect_btn.setEnabled(self.manager.store.available and self._worker is None)

    def _poll_authorization(self):
        if self._flow and self._worker is None:
            self._launch_worker("poll", flow=self._flow)

    def _run_account_action(self, command: str):
        if self._selected_account_id:
            self._launch_worker(command, account_id=self._selected_account_id)

    def _confirm_disconnect(self):
        if not self._selected_account_id:
            return
        answer = QMessageBox.question(
            self,
            "Disconnect account",
            "Delete Brahma's locally stored credential for this account? This does not revoke the authorization at GitHub; use GitHub's authorized-app settings for remote revocation.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._launch_worker("disconnect", account_id=self._selected_account_id)

    def _launch_worker(self, command: str, **kwargs):
        if self._worker is not None:
            return
        self._set_account_actions_enabled(False)
        self._connect_btn.setEnabled(False)
        self._roblox_connect_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)
        self._roblox_cancel_btn.setEnabled(False)
        connector = kwargs.pop("connector", self.github)
        self._worker = _AccountWorker(command, self.manager, connector, **kwargs)
        self._worker.completed.connect(self._worker_completed)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _worker_finished(self):
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        self.refresh()

    def _worker_completed(self, result: dict[str, Any]):
        if not result.get("ok"):
            command = self._worker.command if self._worker is not None else ""
            if command in ("start", "poll"):
                self._poll_timer.stop()
                self._flow = None
                self._code_label.setText("Authorization code: not active")
                self._open_device_btn.setEnabled(False)
                self._cancel_btn.setEnabled(False)
            if command == "roblox_exchange":
                self._roblox_flow = None
                self._roblox_callback_timer.stop()
                listener, self._roblox_listener = self._roblox_listener, None
                if listener is not None:
                    listener.close()
                self._roblox_cancel_btn.setEnabled(False)
                self._roblox_status.setText("Roblox authorization did not complete: the token or identity could not be validated.")
            self._status.setText(
                f"{result.get('error_code', 'error')}: {result.get('message', 'Operation failed.')}"
            )
            self.refresh()
            return
        kind = result.get("kind")
        if kind == "flow_started":
            self._flow = dict(result["flow"])
            self._code_label.setText(f"Authorization code: {self._flow.get('user_code', '')}")
            self._status.setText("Open the official GitHub device page and enter the displayed code there. Brahma will poll at GitHub's required interval. Do not share this code in chat.")
            self._open_device_btn.setEnabled(True)
            self._cancel_btn.setEnabled(True)
            self._connect_btn.setEnabled(False)
            self._poll_timer.start(max(1000, int(self._flow.get("interval", 5)) * 1000))
        elif kind == "flow_pending":
            self._flow = dict(result.get("flow") or self._flow or {})
            self._status.setText("Waiting for GitHub authorization. This remains pending until you approve or the code expires.")
            self._poll_timer.start(max(1000, int(result.get("retry_after", 5)) * 1000))
        elif kind == "connected":
            self._poll_timer.stop()
            self._flow = None
            self._roblox_flow = None
            self._code_label.setText("Authorization code: completed")
            self._open_device_btn.setEnabled(False)
            self._cancel_btn.setEnabled(False)
            provider_id = str(result.get("provider_id") or "github")
            self._status.setText(f"Connected and validated {provider_id} account: {result.get('identity')}.")
            if provider_id == "roblox":
                self._roblox_status.setText(f"Connected and validated Roblox user ID: {result.get('identity')}.")
                self._roblox_cancel_btn.setEnabled(False)
            self._selected_account_id = str(result.get("account_id") or "")
        elif kind == "refreshed":
            self._action_output.setText(f"Authorization refreshed and account identity revalidated: {result.get('identity')} ({result.get('provider_id')}).")
        elif kind == "test":
            data = result.get("data") or {}
            account = data.get("account") or {}
            self._action_output.setText(
                f"Connection test: {'passed' if data.get('ok') else 'failed'}\n"
                f"Identity: {account.get('identity', 'unknown')}\n"
                f"Status: {account.get('status', 'unknown')}\n"
                f"Evidence: {json.dumps(data.get('evidence') or {}, ensure_ascii=False)}"
            )
        elif kind == "action":
            data = result.get("data") or {}
            rendered = data.get("result")
            self._action_output.setText(
                f"Action status: {data.get('status')}\n"
                f"Message: {data.get('message')}\n"
                f"Verification evidence: {json.dumps(data.get('verification_evidence') or {}, ensure_ascii=False)}\n"
                f"Result: {json.dumps(rendered, ensure_ascii=False)[:7000]}"
            )
        elif kind == "disconnected":
            data = result.get("data") or {}
            self._selected_account_id = ""
            self._action_output.setText(
                f"Local status: {data.get('status')}\n"
                f"Provider revocation: {data.get('provider_revocation')}\n"
                f"{data.get('message')}"
            )
            revocation = str(data.get("provider_revocation") or "not_supported")
            self._status.setText(f"Local account credentials were removed. Provider revocation: {revocation}.")

    def closeEvent(self, event):
        self._poll_timer.stop()
        self._roblox_callback_timer.stop()
        if self._roblox_listener is not None:
            self._roblox_listener.close()
            self._roblox_listener = None
        self._roblox_flow = None
        if event:
            super().closeEvent(event)

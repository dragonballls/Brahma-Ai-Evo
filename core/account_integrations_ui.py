"""Accounts & Integrations settings page for the shared secure registry."""
from __future__ import annotations

import json
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
    IntegrationError,
    IntegrationManager,
    get_default_manager,
)


class _AccountWorker(QThread):
    completed = pyqtSignal(object)

    def __init__(
        self,
        command: str,
        manager: IntegrationManager,
        connector: GitHubConnector,
        *,
        client_id: str = "",
        client_secret: str = "",
        flow: dict[str, Any] | None = None,
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
        self.account_id = account_id
        self.action = action

    def run(self):
        try:
            if self.command == "start":
                flow = self.connector.begin_device_authorization(self.client_id)
                # Keep the app secret only in the transient flow object. It is never
                # placed in settings, logs, the activity history, or user-facing output.
                flow["client_secret"] = self.client_secret
                self.completed.emit({"ok": True, "kind": "flow_started", "flow": flow})
                return
            if self.command == "poll":
                flow = dict(self.flow or {})
                outcome = self.connector.poll_device_authorization(flow)
                if outcome.get("pending"):
                    self.completed.emit({"ok": True, "kind": "flow_pending", "flow": flow, "retry_after": outcome.get("retry_after", 5)})
                    return
                summary = self.manager.connect("github", outcome.get("credentials") or {})
                self.completed.emit({"ok": True, "kind": "connected", "account_id": summary.account_id, "identity": summary.identity})
                return
            if self.command == "test":
                self.completed.emit({"ok": True, "kind": "test", "data": self.manager.test_connection(self.account_id)})
                return
            if self.command == "profile":
                result = self.manager.execute(self.account_id, "github.profile.read", {})
                self.completed.emit({"ok": True, "kind": "action", "data": result.to_dict()})
                return
            if self.command == "repositories":
                result = self.manager.execute(self.account_id, "github.repositories.public.list", {})
                self.completed.emit({"ok": True, "kind": "action", "data": result.to_dict()})
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
        self._flow: dict[str, Any] | None = None
        self._worker: _AccountWorker | None = None
        self._selected_account_id = ""
        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(True)
        self._poll_timer.timeout.connect(self._poll_authorization)

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
        for control in (self._test_btn, self._profile_btn, self._repos_btn, self._disconnect_btn):
            control.setEnabled(bool(enabled))

    def refresh(self):
        if not self.manager.store.available:
            self._status.setText(
                "Secure account storage is unavailable. No account token will be stored. "
                "On Windows, enable Windows Credential Manager support and restart Brahma."
            )
            self._connect_btn.setEnabled(False)
        else:
            self._connect_btn.setEnabled(self._worker is None and self._flow is None)
            self._status.setText("Secure account storage is ready. No account is reported connected until GitHub validates the token.")
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
        self._worker = _AccountWorker(command, self.manager, self.github, **kwargs)
        self._worker.completed.connect(self._worker_completed)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _worker_finished(self):
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        self._set_account_actions_enabled(bool(self._selected_account_id))
        self._connect_btn.setEnabled(self.manager.store.available and self._flow is None)
        self.refresh()

    def _worker_completed(self, result: dict[str, Any]):
        if not result.get("ok"):
            self._poll_timer.stop()
            self._flow = None
            self._code_label.setText("Authorization code: not active")
            self._open_device_btn.setEnabled(False)
            self._cancel_btn.setEnabled(False)
            self._status.setText(
                f"{result.get('error_code', 'error')}: {result.get('message', 'Operation failed.')}"
            )
            self._connect_btn.setEnabled(self.manager.store.available)
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
            self._code_label.setText("Authorization code: completed")
            self._open_device_btn.setEnabled(False)
            self._cancel_btn.setEnabled(False)
            self._status.setText(f"Connected and validated GitHub account: {result.get('identity')}.")
            self._selected_account_id = str(result.get("account_id") or "")
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
            self._status.setText("The local account credential was removed. Remote provider revocation was not performed.")

    def closeEvent(self, event):
        self._poll_timer.stop()
        if event:
            super().closeEvent(event)

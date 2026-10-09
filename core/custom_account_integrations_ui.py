"""Dynamic UI for reviewed custom API integrations and confirmed state-changing operations."""
from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any, Mapping

from PyQt6.QtCore import QThread, QTimer, Qt, pyqtSignal, QUrl
from PyQt6.QtGui import QDesktopServices, QFont, QStandardItemModel
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from core.account_integrations import IntegrationError, IntegrationErrorCode, IntegrationManager
from core.custom_integration_registry import register_saved_custom_providers, save_custom_provider
from core.universal_integrations import APIKeyConnector, OAuth2PKCEConnector, configure_provider_connector

_SAMPLE_CONFIG = {
    "provider_id": "my_service", "display_name": "My Service", "auth_type": "api_key",
    "trusted_hosts": ["api.example.com"], "api_key_header": "X-API-Key",
    "identity_url": "https://api.example.com/v1/me", "identity_field": "id",
    "documentation_url": "https://docs.example.com/api",
}
def _safe_confirmation_arguments(value: Any, *, depth: int = 0) -> Any:
    """Redact likely secret fields before the user reviews a state-changing action."""
    secret_markers = ("token", "secret", "password", "credential", "authorization", "cookie", "api_key", "private_key")
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 80:
                result["_truncated"] = True
                break
            name = str(key)
            normalized = name.casefold().replace("-", "_")
            result[name] = "[REDACTED]" if any(marker in normalized for marker in secret_markers) else _safe_confirmation_arguments(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_confirmation_arguments(item, depth=depth + 1) for item in value[:80]]
    if isinstance(value, str):
        return value[:1500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


_SAMPLE_OPENAPI = {
    "openapi": "3.0.3", "info": {"title": "Example API", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com"}],
    "components": {"securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}},
    "security": [{"apiKey": []}],
    "paths": {"/v1/items/{item_id}": {
        "get": {
            "operationId": "readItem", "summary": "Read one item",
            "parameters": [{"name": "item_id", "in": "path", "required": True, "schema": {"type": "string"}}],
            "responses": {"200": {"description": "Provider response"}},
        },
        "patch": {
            "operationId": "updateItem", "summary": "Update one item",
            "parameters": [{"name": "item_id", "in": "path", "required": True, "schema": {"type": "string"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "properties": {"name": {"type": "string", "maxLength": 120}},
                "required": ["name"], "additionalProperties": False
            }}}},
            "responses": {"200": {"description": "Updated item"}},
        },
    }},
}


class _CustomProviderWorker(QThread):
    completed = pyqtSignal(object)

    def __init__(self, command: str, manager: IntegrationManager, **kwargs):
        super().__init__()
        self.command, self.manager, self.options = command, manager, kwargs

    def run(self):
        try:
            o = self.options
            if self.command == "preview":
                config_text, spec_text = o["config_text"], o["openapi_text"]
                if len(config_text.encode("utf-8")) > 256 * 1024 or len(spec_text.encode("utf-8")) > 1024 * 1024:
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider configuration or OpenAPI JSON exceeds its safety limit.")
                config = json.loads(config_text)
                if not isinstance(config, dict):
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider configuration must be a JSON object.")
                forbidden = {"client_secret", "api_key", "access_token", "refresh_token", "password", "authorization", "cookie", "oidc_metadata"}
                if any(str(k).casefold().replace("-", "_") in forbidden for k in config):
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Keep secrets out of provider JSON and use the separate protected credential fields.")
                secret = o.get("client_secret", "")
                if secret:
                    config["client_secret"] = secret
                adapter, preview = configure_provider_connector(config, spec_text)
                public = dict(config)
                public.pop("client_secret", None)
                self.completed.emit({"ok": True, "kind": "preview", "connector": adapter, "config": public, "openapi_text": spec_text, "preview": preview})
                return
            if self.command == "connect_api":
                account = self.manager.connect(o["provider_id"], {"api_key": o["credential"]})
                self.completed.emit({"ok": True, "kind": "connected", "provider_id": account.provider_id, "account_id": account.account_id, "identity": account.identity})
                return
            if self.command == "oauth_exchange":
                adapter = o["connector"]
                if not isinstance(adapter, OAuth2PKCEConnector):
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Selected connector does not use the supported OAuth adapter.")
                credentials = adapter.complete_authorization(o["flow"], o["callback_url"])
                account = self.manager.connect(o["provider_id"], credentials)
                self.completed.emit({"ok": True, "kind": "connected", "provider_id": account.provider_id, "account_id": account.account_id, "identity": account.identity})
                return
            if self.command == "test":
                self.completed.emit({"ok": True, "kind": "test", "data": self.manager.test_connection(o["account_id"])})
                return
            if self.command == "execute":
                args = json.loads(o.get("arguments_text") or "{}")
                if not isinstance(args, dict):
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Operation arguments must be a JSON object.")
                result = self.manager.execute(o["account_id"], o["action"], args)
                data = result.to_dict()
                payload = {"ok": True, "kind": "action", "data": data}
                if data.get("status") == "waiting_for_confirmation" and data.get("confirmation_id"):
                    account = next((item for item in self.manager.list_accounts() if item.account_id == o["account_id"]), None)
                    adapter = self.manager.connector(account.provider_id) if account else None
                    capability = next((item for item in getattr(getattr(adapter, "manifest", None), "capabilities", ())
                                       if item.action == o["action"]), None)
                    payload["confirmation_prompt"] = {
                        "provider_id": account.provider_id if account else "selected provider",
                        "operation": o["action"],
                        "description": capability.description if capability else o["action"],
                        "risk": capability.risk.value if capability else "state-changing action",
                        "arguments": _safe_confirmation_arguments(args),
                    }
                self.completed.emit(payload)
                return
            if self.command == "confirm":
                result = self.manager.confirm_action(o["confirmation_id"], approved=bool(o.get("approved")))
                self.completed.emit({"ok": True, "kind": "action", "data": result.to_dict()})
                return
            if self.command == "refresh":
                account = self.manager.refresh_authorization(o["account_id"])
                self.completed.emit({"ok": True, "kind": "refreshed", "provider_id": account.provider_id, "identity": account.identity})
                return
            if self.command == "disconnect":
                self.completed.emit({"ok": True, "kind": "disconnected", "data": self.manager.disconnect(o["account_id"])})
                return
            raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "Unknown custom provider operation.")
        except IntegrationError as exc:
            self.completed.emit({"ok": False, "error_code": exc.code.value, "message": str(exc)})
        except json.JSONDecodeError:
            self.completed.emit({"ok": False, "error_code": IntegrationErrorCode.INVALID_REQUEST.value, "message": "The provider configuration or operation arguments contain invalid JSON; GraphQL SDL documents are plain text."})
        except (TypeError, ValueError):
            self.completed.emit({"ok": False, "error_code": IntegrationErrorCode.INVALID_REQUEST.value, "message": "The provider configuration or operation arguments are invalid."})
        except Exception:
            self.completed.emit({"ok": False, "error_code": "unexpected_error", "message": "The custom provider operation failed. Provider responses and credentials were not exposed."})


class CustomProvidersWidget(QWidget):
    """User-facing custom provider catalog, OAuth, key connection, testing and actions."""
    accountsChanged = pyqtSignal()

    def __init__(self, manager: IntegrationManager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self._worker: _CustomProviderWorker | None = None
        self._confirmation_request: dict[str, Any] | None = None
        self._pending: dict[str, Any] | None = None
        self._pending_snapshot: tuple[str, str, str] | None = None
        self._flow: dict[str, Any] | None = None
        self._listener: Any = None
        self._active_provider = ""
        self._restore_result = register_saved_custom_providers(manager)
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._check_callback)

        self.setObjectName("CustomProvidersWidget")
        self.setStyleSheet("""
            QWidget#CustomProvidersWidget { background: transparent; color: #f1f4f8; }
            QFrame#CustomProviderCard { background: rgba(10,12,18,190); border: 1px solid rgba(0,229,255,0.19); border-radius: 14px; }
            QLineEdit, QPlainTextEdit, QComboBox { background: rgba(13,15,19,240); color: #f1f4f8; border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; padding: 7px; }
            QPushButton { background: rgba(0,229,255,0.08); color: #f1f4f8; border: 1px solid rgba(0,229,255,0.25); border-radius: 8px; padding: 8px 11px; }
            QPushButton:disabled { color: #88919d; border-color: rgba(255,255,255,0.08); }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame(self)
        card.setObjectName("CustomProviderCard")
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)
        title = QLabel("Add a documented provider")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        layout.addWidget(title)
        intro = QLabel("Use the provider's official API documentation and OpenAPI JSON or GraphQL SDL/introspection schema. Set protocol to graphql in provider configuration and list the exact named operations you want reviewed. State-changing requests require a separate confirmation showing provider, risk, and target arguments. Never enter a website password or put tokens/secrets into JSON.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addWidget(QLabel("Provider configuration JSON (replace example endpoints with the real documented provider)"))
        self._config = QPlainTextEdit(json.dumps(_SAMPLE_CONFIG, indent=2))
        self._config.setMinimumHeight(130)
        self._config.setMaximumHeight(210)
        layout.addWidget(self._config)
        layout.addWidget(QLabel("API schema: OpenAPI 3.0/3.1 JSON or GraphQL SDL/introspection JSON (choose protocol in provider configuration)"))
        self._spec = QPlainTextEdit(json.dumps(_SAMPLE_OPENAPI, indent=2))
        self._spec.setMinimumHeight(160)
        self._spec.setMaximumHeight(250)
        layout.addWidget(self._spec)
        row = QHBoxLayout()
        row.addWidget(QLabel("OAuth client secret (only if required)"))
        self._client_secret = QLineEdit()
        self._client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._client_secret.setPlaceholderText("Protected storage; never stored in provider JSON")
        row.addWidget(self._client_secret, 1)
        layout.addLayout(row)
        row = QHBoxLayout()
        self._preview_btn = QPushButton("Validate and preview")
        self._preview_btn.clicked.connect(self._preview_provider)
        row.addWidget(self._preview_btn)
        self._save_btn = QPushButton("Approve and save provider")
        self._save_btn.clicked.connect(self._save_provider)
        self._save_btn.setEnabled(False)
        row.addWidget(self._save_btn)
        layout.addLayout(row)
        layout.addWidget(QLabel("Provider and account status"))
        self._status = QLabel("No custom provider selected.")
        self._status.setWordWrap(True)
        self._status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._status)
        row = QHBoxLayout()
        self._providers = QComboBox()
        self._providers.currentIndexChanged.connect(self._provider_changed)
        row.addWidget(self._providers, 2)
        self._connect_btn = QPushButton("Connect account")
        self._connect_btn.clicked.connect(self._connect_provider)
        row.addWidget(self._connect_btn)
        self._cancel_btn = QPushButton("Cancel OAuth")
        self._cancel_btn.clicked.connect(self._cancel_oauth)
        self._cancel_btn.setEnabled(False)
        row.addWidget(self._cancel_btn)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("API key / bearer token"))
        self._credential = QLineEdit()
        self._credential.setEchoMode(QLineEdit.EchoMode.Password)
        self._credential.setPlaceholderText("Sent in the configured header after validation")
        row.addWidget(self._credential, 1)
        layout.addLayout(row)
        self._accounts = QComboBox()
        self._accounts.currentIndexChanged.connect(self._account_changed)
        layout.addWidget(QLabel("Connected custom accounts"))
        layout.addWidget(self._accounts)
        self._account_detail = QLabel("No account selected.")
        self._account_detail.setWordWrap(True)
        layout.addWidget(self._account_detail)
        row = QHBoxLayout()
        self._test_btn = QPushButton("Test connection")
        self._test_btn.clicked.connect(self._test_account)
        row.addWidget(self._test_btn)
        self._refresh_btn = QPushButton("Refresh authorization")
        self._refresh_btn.clicked.connect(self._refresh_authorization)
        row.addWidget(self._refresh_btn)
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.clicked.connect(self._disconnect_account)
        row.addWidget(self._disconnect_btn)
        self._operation = QComboBox()
        row.addWidget(self._operation, 2)
        self._execute_btn = QPushButton("Run selected operation")
        self._execute_btn.clicked.connect(self._run_operation)
        row.addWidget(self._execute_btn)
        layout.addLayout(row)
        layout.addWidget(QLabel("Operation arguments (JSON object; undeclared parameters are rejected)"))
        self._arguments = QPlainTextEdit("{}")
        self._arguments.setMaximumHeight(75)
        layout.addWidget(self._arguments)

        self._config.textChanged.connect(self._invalidate_preview)
        self._spec.textChanged.connect(self._invalidate_preview)
        self._client_secret.textChanged.connect(self._invalidate_preview)
        if self._restore_result["errors"]:
            self._status.setText("Some saved provider configurations need attention:\n" + "\n".join(self._restore_result["errors"][:5]))
        elif self._restore_result["loaded"]:
            self._status.setText("Saved custom providers restored: " + ", ".join(self._restore_result["loaded"]) + ". Account tokens remain in protected storage and are checked when tested.")
        self._refresh_providers()
        self._refresh_accounts()

    def _snapshot(self) -> tuple[str, str, str]:
        return self._config.toPlainText(), self._spec.toPlainText(), self._client_secret.text()

    def _invalidate_preview(self, *_):
        self._pending = None
        self._pending_snapshot = None
        self._save_btn.setEnabled(False)

    def _busy(self, value: bool):
        self._preview_btn.setEnabled(not value)
        self._save_btn.setEnabled(bool(not value and self._pending and self._pending_snapshot == self._snapshot()))
        self._connect_btn.setEnabled(bool(not value and self._providers.currentData() and not self._flow))
        account = self._current_account()
        self._test_btn.setEnabled(bool(not value and account))
        self._disconnect_btn.setEnabled(bool(not value and account))
        self._refresh_btn.setEnabled(bool(not value and account and self._supports_refresh(account, self.manager)))
        self._execute_btn.setEnabled(bool(not value and account and self._operation.currentData()))

    def _start_worker(self, command: str, **kwargs):
        if self._worker is not None:
            return
        self._busy(True)
        self._worker = _CustomProviderWorker(command, self.manager, **kwargs)
        self._worker.completed.connect(self._completed)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _worker_finished(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        self._refresh_providers()
        self._refresh_accounts()
        self._busy(False)
        pending = self._confirmation_request
        self._confirmation_request = None
        if pending:
            prompt = pending["prompt"]
            message = (
                "Provider: " + str(prompt["provider_id"]) +
                "\nOperation: " + str(prompt["operation"]) +
                "\nDeclared action: " + str(prompt["description"]) +
                "\nRisk classification: " + str(prompt["risk"]) +
                "\n\nExact arguments/target:\n" +
                json.dumps(prompt["arguments"], ensure_ascii=False, indent=2)[:7000] +
                "\n\nApprove sending this request to the provider? It may change remote state. "
                "Rejecting sends no operation request."
            )
            answer = QMessageBox.question(
                self, "Confirm provider operation", message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            self._start_worker(
                "confirm", confirmation_id=pending["confirmation_id"],
                approved=(answer == QMessageBox.StandardButton.Yes),
            )

    def _preview_provider(self):
        if self._worker is not None:
            return
        if not self.manager.store.available:
            self._status.setText("Secure credential storage is unavailable; provider registration is disabled.")
            return
        self._pending = None
        self._pending_snapshot = None
        self._save_btn.setEnabled(False)
        self._status.setText("Validating provider configuration and reviewing its OpenAPI contract…")
        self._start_worker("preview", config_text=self._config.toPlainText(), openapi_text=self._spec.toPlainText(), client_secret=self._client_secret.text())

    def _save_provider(self):
        if not self._pending or self._pending_snapshot != self._snapshot() or self._worker is not None:
            self._status.setText("This preview is stale. Validate the current configuration again before saving.")
            self._save_btn.setEnabled(False)
            return
        preview = self._pending["preview"]
        operations = preview.get("review_candidates") or []
        blocked = [item for item in (preview.get("mutation_candidates") or []) if item.get("supported") is not True]
        rows = [
            str(item.get("method") or "GET") + " [" + str(item.get("risk") or "read_only") + "] " +
            str(item.get("action")) + " — " + str(item.get("summary")) + " — " + str(item.get("path")) +
            ("\nGraphQL operation document:\n" + str(item.get("document") or "") if item.get("protocol") == "graphql" else "")
            for item in operations
        ]
        details = "Capabilities to be enabled:\n\n" + ("\n".join(rows) or "(none)")
        if blocked:
            details += "\n\nUnsupported operations remain blocked:\n" + "\n".join(
                str(item.get("method")) + " " + str(item.get("path")) + ": " + str(item.get("unsupported_reason") or "unsupported contract")
                for item in blocked
            )
        if preview.get("warnings"):
            details += "\n\nImport warnings:\n" + "\n".join(str(item) for item in preview.get("warnings", []))
        details += "\n\nNo requests are sent during registration. Every non-read action requires individual confirmation at execution time."
        dialog = QDialog(self)
        dialog.setWindowTitle("Review provider capabilities")
        dialog.resize(780, 600)
        dialog_layout = QVBoxLayout(dialog)
        review_text = QPlainTextEdit(dialog)
        review_text.setReadOnly(True)
        review_text.setPlainText(details)
        dialog_layout.addWidget(review_text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No, parent=dialog)
        buttons.button(QDialogButtonBox.StandardButton.Yes).setText("Approve listed capabilities")
        buttons.button(QDialogButtonBox.StandardButton.No).setText("Cancel")
        buttons.button(QDialogButtonBox.StandardButton.No).setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dialog_layout.addWidget(buttons)
        answer = dialog.exec()
        if answer != QDialog.DialogCode.Accepted:
            self._status.setText("Provider save cancelled; nothing was registered.")
            return
        pending = self._pending
        try:
            saved = save_custom_provider(self.manager, pending["config"], pending["openapi_text"], pending["connector"], client_secret=self._client_secret.text())
        except IntegrationError as exc:
            self._status.setText(exc.code.value + ": " + str(exc))
            return
        self._pending = None
        self._pending_snapshot = None
        self._client_secret.clear()
        self._save_btn.setEnabled(False)
        self._status.setText("Registered " + saved["display_name"] + " (" + saved["provider_id"] + "). Connect an account to use its verified operations.")
        self._refresh_providers(saved["provider_id"])
        self.accountsChanged.emit()

    def _refresh_providers(self, selected: str | None = None):
        current = selected or str(self._providers.currentData() or "")
        self._providers.blockSignals(True)
        self._providers.clear()
        for manifest in self.manager.catalog():
            provider = manifest.provider_id
            adapter = self.manager.connector(provider)
            if provider in ("github", "roblox") or not isinstance(adapter, (APIKeyConnector, OAuth2PKCEConnector)):
                continue
            self._providers.addItem(manifest.display_name + " (" + provider + ")", provider)
        if current:
            index = self._providers.findData(current)
            if index >= 0:
                self._providers.setCurrentIndex(index)
        self._providers.blockSignals(False)
        self._provider_changed()

    def _provider_changed(self, *_):
        provider = str(self._providers.currentData() or "")
        adapter = self.manager.connector(provider) if provider else None
        is_key = isinstance(adapter, APIKeyConnector)
        self._credential.setEnabled(is_key)
        if is_key and adapter.credential_auth_type == "bearer":
            self._credential.setPlaceholderText("Bearer token; sent as Authorization: Bearer …")
        elif is_key:
            self._credential.setPlaceholderText("API key; sent raw in " + adapter.api_key_header)
        else:
            self._credential.clear()
            self._credential.setPlaceholderText("OAuth providers use the official browser flow")
        self._connect_btn.setEnabled(bool(provider and self._worker is None and not self._flow))

    def _connect_provider(self):
        provider = str(self._providers.currentData() or "")
        adapter = self.manager.connector(provider) if provider else None
        if not provider or adapter is None:
            self._status.setText("Choose a saved custom provider.")
            return
        if not self.manager.store.available:
            self._status.setText("Secure credential storage is unavailable; no credential will be stored.")
            return
        if self._worker is not None or self._flow:
            self._status.setText("Finish or cancel the active authorization first.")
            return
        if isinstance(adapter, APIKeyConnector):
            token = self._credential.text()
            if not token:
                self._status.setText("Enter a provider API key or bearer token, not a website password.")
                return
            self._start_worker("connect_api", provider_id=provider, credential=token)
            return
        if not isinstance(adapter, OAuth2PKCEConnector):
            self._status.setText("This adapter has no supported custom authorization flow.")
            return
        redirect = urllib.parse.urlparse(adapter.redirect_uri)
        if redirect.scheme != "http" or redirect.hostname != "127.0.0.1" or not redirect.port:
            self._status.setText("In-app OAuth currently requires an exact http://127.0.0.1:<port>/<path> redirect URI registered with the provider.")
            return
        try:
            # Reuse the existing strict callback receiver; imported only after the page
            # module is fully loaded to avoid a module-import cycle.
            from core.account_integrations_ui import _LoopbackOAuthCallback
            flow = adapter.begin_authorization()
            listener = _LoopbackOAuthCallback(
                adapter.redirect_uri, str(flow.get("state") or ""),
                provider_name=getattr(getattr(adapter, "manifest", None), "display_name", provider),
            )
        except (ValueError, OSError, IntegrationError) as exc:
            self._status.setText("OAuth could not start: " + str(exc))
            return
        self._active_provider, self._flow, self._listener = provider, flow, listener
        if not QDesktopServices.openUrl(QUrl(str(flow.get("authorization_url") or ""))):
            self._cancel_oauth()
            self._status.setText("The authorization browser did not open. No credential was stored.")
            return
        self._status.setText("Waiting for official " + provider + " authorization. The loopback callback verifies the registered redirect and state.")
        self._cancel_btn.setEnabled(True)
        self._connect_btn.setEnabled(False)
        self._timer.start()

    def _check_callback(self):
        if not self._flow or not self._listener:
            self._timer.stop()
            return
        if time.time() >= float(self._flow.get("expires_at") or 0):
            self._cancel_oauth()
            self._status.setText("OAuth authorization expired; start a new connection.")
            return
        if not self._listener.event.is_set():
            return
        listener, self._listener = self._listener, None
        callback = listener.result() or {"error": "missing_callback"}
        listener.close()
        self._timer.stop()
        flow, self._flow = self._flow, None
        self._cancel_btn.setEnabled(False)
        if callback.get("error"):
            self._status.setText("Authorization was denied or the callback was invalid. No token was saved.")
            self._connect_btn.setEnabled(True)
            return
        provider = self._active_provider
        self._start_worker("oauth_exchange", connector=self.manager.connector(provider), provider_id=provider, flow=flow, callback_url=callback["callback_url"])

    def _cancel_oauth(self):
        self._timer.stop()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        self._flow = None
        self._cancel_btn.setEnabled(False)
        self._status.setText("Authorization cancelled locally; no token was saved.")
        self._busy(False)

    def _current_account(self):
        account_id = str(self._accounts.currentData() or "")
        if not account_id:
            return None
        try:
            return next((account for account in self.manager.list_accounts() if account.account_id == account_id), None)
        except IntegrationError:
            return None

    @staticmethod
    def _supports_refresh(account, manager=None):
        return bool(manager and callable(getattr(manager.connector(account.provider_id), "refresh_credentials", None)))

    def _refresh_accounts(self):
        selected = str(self._accounts.currentData() or "")
        providers = {str(self._providers.itemData(i) or "") for i in range(self._providers.count())}
        try:
            accounts = [account for account in self.manager.list_accounts() if account.provider_id in providers]
        except IntegrationError as exc:
            accounts = []
            self._status.setText(exc.code.value + ": " + str(exc))
        self._accounts.blockSignals(True)
        self._accounts.clear()
        for account in accounts:
            self._accounts.addItem(account.identity + " • " + account.provider_id + " • " + account.status.value, account.account_id)
        index = self._accounts.findData(selected)
        if index >= 0:
            self._accounts.setCurrentIndex(index)
        self._accounts.blockSignals(False)
        self._account_changed()

    def _account_changed(self, *_):
        account = self._current_account()
        self._operation.clear()
        if account is None:
            self._account_detail.setText("No account selected.")
            self._busy(self._worker is not None)
            return
        self._account_detail.setText("Identity: " + account.identity + "\nProvider: " + account.provider_id + "\nStatus: " + account.status.value + "\nGranted scopes: " + (", ".join(account.scopes) or "none reported"))
        adapter = self.manager.connector(account.provider_id)
        for cap in getattr(getattr(adapter, "manifest", None), "capabilities", ()):
            if not cap.supported or cap.action == account.provider_id + ".connection.test":
                continue
            missing = sorted(set(cap.required_scopes) - set(account.scopes))
            label = cap.action + " [" + ("READ" if cap.risk.value == "read_only" else cap.risk.value.upper()) + "] — " + cap.description
            if missing:
                label += " (missing scopes: " + ", ".join(missing) + ")"
            self._operation.addItem(label, cap.action)
            if missing and isinstance(self._operation.model(), QStandardItemModel):
                item = self._operation.model().item(self._operation.count() - 1)
                if item is not None:
                    item.setEnabled(False)
        self._busy(self._worker is not None)

    def _test_account(self):
        account = self._current_account()
        if account:
            self._start_worker("test", account_id=account.account_id)

    def _refresh_authorization(self):
        account = self._current_account()
        if account and self._supports_refresh(account, self.manager):
            self._start_worker("refresh", account_id=account.account_id)

    def _disconnect_account(self):
        account = self._current_account()
        if not account:
            return
        answer = QMessageBox.question(self, "Disconnect custom account",
            "Delete Brahma's local credential record for " + account.provider_id + " / " + account.identity
            + "? Provider revocation is attempted only if this adapter declares an official revocation operation.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._start_worker("disconnect", account_id=account.account_id)

    def _run_operation(self):
        account = self._current_account()
        action = str(self._operation.currentData() or "")
        if account and action:
            self._start_worker("execute", account_id=account.account_id, action=action, arguments_text=self._arguments.toPlainText())
        else:
            self._status.setText("Select an account and one enabled operation declared by its provider.")

    def _completed(self, result: dict[str, Any]):
        if not result.get("ok"):
            self._status.setText(str(result.get("error_code") or "error") + ": " + str(result.get("message") or "Operation failed."))
            if self._worker is not None and self._worker.command in ("connect_api", "oauth_exchange"):
                self._credential.clear()
            return
        kind = result.get("kind")
        if kind == "preview":
            self._pending, self._pending_snapshot = result, self._snapshot()
            preview = result["preview"]
            rows = [
                str(item.get("method") or "GET") + " [" + str(item.get("risk") or "read_only") + "] " +
                str(item.get("action")) + " — " + str(item.get("summary"))
                for item in preview.get("review_candidates", [])
            ]
            body = "Reviewed candidates (preview only; no API calls sent):\n" + ("\n".join(rows[:80]) or "(none)")
            if len(rows) > 80:
                body += "\n" + str(len(rows) - 80) + " additional candidates are shown in the scrollable approval dialog."
            blocked = [item for item in (preview.get("mutation_candidates") or []) if item.get("supported") is not True]
            body += "\nUnsupported state-changing operations: " + str(len(blocked))
            if preview.get("warnings"):
                body += "\nReview warnings:\n" + "\n".join(str(x) for x in preview["warnings"][:10])
            body += "\n\nNothing is registered or executed until you approve the preview."
            self._status.setText(body)
            self._save_btn.setEnabled(True)
        elif kind == "connected":
            self._credential.clear()
            self._status.setText("Connected and validated " + str(result.get("provider_id")) + " account " + str(result.get("identity")) + ".")
            self._refresh_accounts()
            self.accountsChanged.emit()
        elif kind == "test":
            data = result.get("data") or {}
            account = data.get("account") or {}
            self._status.setText("Connection test: " + ("passed" if data.get("ok") else "failed") + "\nIdentity: " + str(account.get("identity") or "unknown") + "\nStatus: " + str(account.get("status") or "unknown") + "\nEvidence: " + json.dumps(data.get("evidence") or {}, ensure_ascii=False)[:2500])
            self.accountsChanged.emit()
        elif kind == "action":
            data = result.get("data") or {}
            if data.get("status") == "waiting_for_confirmation" and data.get("confirmation_id"):
                self._confirmation_request = {
                    "confirmation_id": data["confirmation_id"],
                    "prompt": result.get("confirmation_prompt") or {
                        "provider_id": data.get("provider_id") or "selected provider",
                        "operation": data.get("action") or "unknown operation",
                        "description": data.get("action") or "state-changing operation",
                        "risk": "state-changing action", "arguments": {},
                    },
                }
                self._status.setText("Waiting for confirmation. The request has not been sent; review its target and consequence in the confirmation dialog.")
            else:
                self._status.setText("Operation status: " + str(data.get("status")) + "\nMessage: " + str(data.get("message")) + "\nVerification evidence: " + json.dumps(data.get("verification_evidence") or {}, ensure_ascii=False)[:1600] + "\nResult: " + json.dumps(data.get("result"), ensure_ascii=False)[:3000])
        elif kind == "refreshed":
            self._status.setText("Authorization refreshed and identity revalidated for " + str(result.get("provider_id")) + ": " + str(result.get("identity")))
            self.accountsChanged.emit()
        elif kind == "disconnected":
            data = result.get("data") or {}
            self._status.setText("Local credentials removed: " + str(data.get("ok")) + "\nProvider revocation: " + str(data.get("provider_revocation")) + "\n" + str(data.get("message")))
            self.accountsChanged.emit()

    def closeEvent(self, event):
        self._timer.stop()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        self._flow = None
        if event:
            super().closeEvent(event)

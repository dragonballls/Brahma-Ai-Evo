from __future__ import annotations

import secrets
import time
import threading
from typing import Any

from .authentication import generate_pairing_token
from .models import PairingOffer


class PairingManager:
    def __init__(self, service_name: str = "_BRAHMA._tcp.local.", ttl_seconds: int = 300):
        self.service_name = service_name
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._offers: dict[str, PairingOffer] = {}
        self._code_index: dict[str, str] = {}
        self._claimed: set[str] = set()
        self._lock = threading.RLock()

    def _prune(self) -> None:
        now = time.time()
        stale = [token for token, offer in self._offers.items() if offer.expires_at <= now]
        for token in stale:
            offer = self._offers.pop(token, None)
            if offer is not None:
                self._code_index.pop(offer.pairing_code, None)
            self._claimed.discard(token)

    def create_offer(self, host: str, port: int) -> PairingOffer:
        with self._lock:
            self._prune()
        token = generate_pairing_token()
        code = ""
        for _ in range(20):
            candidate = f"{secrets.randbelow(1_000_000):06d}"
            if candidate not in self._code_index:
                code = candidate
                break
        if not code:
            raise RuntimeError("Unable to allocate a unique pairing code; try again.")
        offer = PairingOffer(
            service=self.service_name,
            host=host,
            port=int(port),
            pairing_token=token,
            pairing_code=code,
            expires_at=time.time() + self.ttl_seconds,
        )
            self._offers[token] = offer
            self._code_index[code] = token
            return offer

    def get_offer(self, pairing_token: str) -> PairingOffer | None:
        with self._lock:
            self._prune()
            return self._offers.get(pairing_token)

    def get_offer_by_code(self, pairing_code: str) -> PairingOffer | None:
        with self._lock:
            self._prune()
            token = self._code_index.get(str(pairing_code).strip())
            if not token:
                return None
            return self._offers.get(token)

    def claim(self, pairing_token: str) -> PairingOffer | None:
        """Atomically reserve a single offer for one pairing transaction."""
        with self._lock:
            self._prune()
            if pairing_token in self._claimed:
                return None
            offer = self._offers.get(pairing_token)
            if offer is None:
                return None
            self._claimed.add(pairing_token)
            return offer

    def release(self, pairing_token: str) -> None:
        with self._lock:
            self._claimed.discard(pairing_token)

    def approve(self, pairing_token: str) -> PairingOffer | None:
        with self._lock:
            self._prune()
            offer = self._offers.pop(pairing_token, None)
            self._claimed.discard(pairing_token)
            if offer is None:
                return None
            self._code_index.pop(offer.pairing_code, None)
            return offer

    def reject(self, pairing_token: str) -> bool:
        with self._lock:
            self._prune()
            offer = self._offers.pop(pairing_token, None)
            self._claimed.discard(pairing_token)
            if offer is None:
                return False
            self._code_index.pop(offer.pairing_code, None)
            return True

    def to_qr_payload(self, offer: PairingOffer) -> dict[str, Any]:
        return offer.to_dict()

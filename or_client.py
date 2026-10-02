from core.user_paths import get_user_data_dir
import json
import sys
import time
import base64
import logging
import os
import threading
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("openrouter_client")

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR     = _get_base_dir()
API_KEY_PATH = get_user_data_dir() / "config" / "api_keys.json"


def normalize_api_key(value: str | None) -> str:
    """Normalize common clipboard/paste variants without altering valid keys."""
    key = (value or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in {"'", '"'}:
        key = key[1:-1].strip()
    return key


def validate_api_key_format(value: str | None) -> tuple[bool, str]:
    key = normalize_api_key(value)
    if not key:
        return False, "OpenRouter API key is empty."
    if not key.startswith("sk-or-"):
        return False, "OpenRouter keys should start with 'sk-or-'."
    if any(ch.isspace() for ch in key):
        return False, "OpenRouter API key contains whitespace."
    return True, ""


def _load_api_key() -> str:
    """Load the file key first, then fall back to OPENROUTER_API_KEY."""
    try:
        if API_KEY_PATH.exists():
            with open(API_KEY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            key = normalize_api_key(data.get("openrouter_api_key"))
            if key:
                return key
    except (OSError, json.JSONDecodeError, TypeError) as e:
        logger.warning(f"[OpenRouter] Failed to load API key file: {e}")

    return normalize_api_key(os.environ.get("OPENROUTER_API_KEY", ""))


def save_api_key(value: str | None) -> tuple[bool, str]:
    """Persist the OpenRouter key atomically while preserving other credentials."""
    key = normalize_api_key(value)
    if key:
        ok, error = validate_api_key_format(key)
        if not ok:
            return False, error

    API_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        current: dict = {}
        if API_KEY_PATH.exists():
            with API_KEY_PATH.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    current = loaded
        current["openrouter_api_key"] = key
        temp_path = API_KEY_PATH.with_name(f".{API_KEY_PATH.name}.tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(current, indent=4, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, API_KEY_PATH)
        return True, ""
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return False, f"Could not save OpenRouter API key: {exc}"

TEXT_MODELS: list[str] = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "minimax/minimax-m2.5:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "qwen/qwen3-coder:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-3-27b-it:free",
    "arcee-ai/trinity-large-preview:free",
    "z-ai/glm-4.5-air:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
    "google/gemma-3-12b-it:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "google/gemma-3-4b-it:free",
    "google/gemma-3n-e4b-it:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "google/gemma-3n-e2b-it:free",
    "liquid/lfm-2.5-1.2b-instruct:free",
    "liquid/lfm-2.5-1.2b-thinking:free",
]

VISION_MODELS: list[str] = [
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/llama-nemotron-embed-vl-1b-v2:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-3n-e4b-it:free",
    "google/gemma-3n-e2b-it:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
]

API_URL               = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MAX_TOKENS    = 4096
DEFAULT_TEMPERATURE   = 0.7
REQUEST_TIMEOUT       = 60   # seconds per request
MAX_RETRIES_PER_MODEL = 2    # attempts before moving to next model
RETRY_DELAY           = 2    # seconds between retries
RATE_LIMIT_COOLDOWN   = 60   # seconds before retrying a rate-limited model
MODEL_CATALOG_URL      = "https://openrouter.ai/api/v1/models"
MODEL_CATALOG_TTL      = 900
FREE_ROUTER_MODEL      = "openrouter/free"

_rate_limited: dict[str, float] = {}
_rate_limit_lock = threading.RLock()
_model_catalog_lock = threading.RLock()
_model_catalog_ids: set[str] = set()
_model_catalog_meta: dict[str, dict] = {}
_model_catalog_cached_at = 0.0

class OpenRouterClient:

    def __init__(self) -> None:
        self.api_key  = _load_api_key()
        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://github.com/brahma-ai",
            "X-Title":       "Brahma Evo",
        }

    def _get_model_catalog(self, *, force: bool = False) -> dict[str, dict]:
        """Fetch and cache the public OpenRouter model catalog."""
        global _model_catalog_ids, _model_catalog_meta, _model_catalog_cached_at
        now = time.time()
        with _model_catalog_lock:
            if (
                not force
                and _model_catalog_cached_at
                and now - _model_catalog_cached_at < MODEL_CATALOG_TTL
            ):
                return dict(_model_catalog_meta)

        try:
            response = requests.get(
                MODEL_CATALOG_URL,
                headers={"Accept": "application/json", "User-Agent": "Brahma-Evo"},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            catalog = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                model_id = str(row.get("id") or "").strip()
                if model_id:
                    catalog[model_id] = row

            with _model_catalog_lock:
                _model_catalog_meta = catalog
                _model_catalog_ids = set(catalog)
                _model_catalog_cached_at = now
                return dict(catalog)
        except requests.exceptions.RequestException as exc:
            logger.warning(f"[OpenRouter] Model catalog unavailable: {exc}")
        except (ValueError, TypeError) as exc:
            logger.warning(f"[OpenRouter] Model catalog returned invalid data: {exc}")

        with _model_catalog_lock:
            return dict(_model_catalog_meta)

    def _model_pool(self, *, vision: bool = False) -> list[str]:
        """Return a live-validated pool, with the provider's resilient free router first."""
        catalog = self._get_model_catalog()
        live_ids = set(catalog)

        static_pool = VISION_MODELS if vision else TEXT_MODELS
        pool = [model for model in static_pool if not live_ids or model in live_ids]

        if FREE_ROUTER_MODEL in live_ids:
            pool.insert(0, FREE_ROUTER_MODEL)
        elif not pool:
            pool = [FREE_ROUTER_MODEL]

        # Remove duplicates while preserving order.
        return list(dict.fromkeys(pool))

    def test_api_key(self, key: str | None = None) -> tuple[bool, str, dict]:
        """Validate credentials against OpenRouter without consuming model inference."""
        candidate = normalize_api_key(key) if key is not None else _load_api_key()
        ok, error = validate_api_key_format(candidate)
        if not ok:
            return False, error, {}

        headers = {
            "Authorization": f"Bearer {candidate}",
            "Accept": "application/json",
            "HTTP-Referer": "https://github.com/brahma-ai",
            "X-Title": "Brahma Evo",
        }
        try:
            response = requests.get(
                "https://openrouter.ai/api/v1/key",
                headers=headers,
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json() if response.content else {}
                return True, "OpenRouter API key verified.", data if isinstance(data, dict) else {}
            if response.status_code == 401:
                return False, "OpenRouter rejected the API key (401 Unauthorized).", {}
            if response.status_code == 403:
                return False, "OpenRouter rejected the API key (403 Forbidden).", {}
            if response.status_code == 429:
                return False, "OpenRouter rate-limited the key check (429). Try again shortly.", {}
            detail = ""
            try:
                payload = response.json()
                detail = str(payload.get("error", {}).get("message", "")) if isinstance(payload, dict) else ""
            except ValueError:
                detail = ""
            return False, f"OpenRouter key check failed (HTTP {response.status_code}){': ' + detail if detail else ''}.", {}
        except requests.exceptions.Timeout:
            return False, "OpenRouter key check timed out.", {}
        except requests.exceptions.RequestException as exc:
            return False, f"Could not reach OpenRouter: {exc}", {}

    def _refresh_api_key(self) -> None:
        """Reload the key when the credentials file changes without restarting."""
        key = _load_api_key()
        if key == self.api_key:
            return
        self.api_key = key
        self._headers["Authorization"] = f"Bearer {key}"

    def _is_rate_limited(self, model: str) -> bool:
        with _rate_limit_lock:
            ts = _rate_limited.get(model)
            if ts is None:
                return False
            if time.time() - ts > RATE_LIMIT_COOLDOWN:
                _rate_limited.pop(model, None)
                return False
            return True

    def _mark_rate_limited(self, model: str) -> None:
        with _rate_limit_lock:
            _rate_limited[model] = time.time()
        logger.warning(
            f"[OpenRouter] Rate limited: {model} — "
            f"cooling down for {RATE_LIMIT_COOLDOWN}s"
        )

    def _call(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        response_format: Optional[dict] = None,
    ) -> Optional[str]:
        self._refresh_api_key()

        payload: dict = {
            "model":       model,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format

        if not self.api_key:
            raise PermissionError(
                "[OpenRouter] API key is missing. Add a valid sk-or- key in Settings or OPENROUTER_API_KEY."
            )
        valid, error = validate_api_key_format(self.api_key)
        if not valid:
            raise PermissionError(f"[OpenRouter] {error}")

        for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
            try:
                resp = requests.post(
                    API_URL,
                    headers=self._headers,
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )

                if resp.status_code == 401:
                    raise PermissionError(
                        f"[OpenRouter] Authentication failed for model {model}. "
                        "Check your API key in config/api_keys.json."
                    )

                if resp.status_code == 403:
                    raise PermissionError(
                        f"[OpenRouter] Access denied for model {model} (HTTP 403). "
                        "Check your account permissions and model access."
                    )

                if resp.status_code == 429:
                    self._mark_rate_limited(model)
                    return None

                if resp.status_code == 200:
                    data    = resp.json()
                    content = (
                        data.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                    )
                    return content.strip() if content else None

                logger.warning(
                    f"[OpenRouter] {model} → HTTP {resp.status_code} "
                    f"(attempt {attempt}/{MAX_RETRIES_PER_MODEL})"
                )

            except requests.exceptions.Timeout:
                logger.warning(
                    f"[OpenRouter] {model} → Timeout "
                    f"(attempt {attempt}/{MAX_RETRIES_PER_MODEL})"
                )
            except Exception as e:
                logger.error(f"[OpenRouter] {model} → Unexpected error: {e}")

            if attempt < MAX_RETRIES_PER_MODEL:
                time.sleep(RETRY_DELAY)

        return None

    def _call_with_fallback(
        self,
        pool: list[str],
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        response_format: Optional[dict] = None,
    ) -> str:
        if model and not self._is_rate_limited(model):
            try:
                result = self._call(model, messages, max_tokens, temperature, response_format)
                if result:
                    return result
                logger.info(
                    f"[OpenRouter] Requested model failed, "
                    f"falling back to pool: {model}"
                )
            except PermissionError:
                raise

        for m in pool:
            if self._is_rate_limited(m):
                continue
            logger.info(f"[OpenRouter] Trying: {m}")
            result = self._call(m, messages, max_tokens, temperature, response_format)
            if result:
                logger.info(f"[OpenRouter] ✓ Success: {m}")
                return result

        raise RuntimeError(
            "[OpenRouter] All models failed or are rate-limited. "
            "Check your API key and network connection."
        )

    def chat(
        self,
        prompt: str,
        system: str = (
            "You are a component of Brahma Evo, an autonomous self-evolving personal assistant. "
            "Be concise, helpful, and precise."
        ),
        history: Optional[list[dict]] = None,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        messages = [{"role": "system", "content": system}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})

        return self._call_with_fallback(
            self._model_pool(vision=False), messages, model, max_tokens, temperature
        )

    def chat_json(
        self,
        prompt: str,
        system: str = (
            "Return ONLY valid JSON. "
            "No markdown fences, no extra text, no explanation."
        ),
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict:
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ]
        raw = self._call_with_fallback(
            self._model_pool(vision=False), messages, model, max_tokens, temperature=0.2
        )

        clean = raw.strip()
        if clean.startswith("```"):
            parts = clean.split("```")
            clean = parts[1] if len(parts) > 1 else clean
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip().rstrip("`").strip()

        try:
            return json.loads(clean)
        except json.JSONDecodeError as e:
            logger.error(
                f"[OpenRouter] JSON parse failed: {e}\n"
                f"Raw response (first 300 chars): {raw[:300]}"
            )
            raise ValueError(
                f"Model returned unparseable JSON: {e}\n"
                f"Raw output: {raw[:200]}"
            )

    def vision(
        self,
        prompt: str,
        image_b64: str,
        mime: str = "image/png",
        system: str = "Analyze the image and describe what you see clearly and concisely.",
        model: Optional[str] = None,
        max_tokens: int = 1024,
    ) -> str:
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{image_b64}"
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            },
        ]
        return self._call_with_fallback(
            self._model_pool(vision=True), messages, model, max_tokens, temperature=0.2
        )

    def vision_from_file(
        self,
        prompt: str,
        image_path: str,
        system: str = "Analyze the image and describe what you see clearly and concisely.",
        model: Optional[str] = None,
        max_tokens: int = 1024,
    ) -> str:
        path = Path(image_path)
        mime_map = {
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif":  "image/gif",
        }
        mime = mime_map.get(path.suffix.lower(), "image/png")

        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        return self.vision(prompt, image_b64, mime, system, model, max_tokens)

    def multi_turn(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
    
        return self._call_with_fallback(
            self._model_pool(vision=False), messages, model, max_tokens, temperature
        )

    def available_models(self) -> dict:
        catalog = self._get_model_catalog()
        return {
            "text_models": self._model_pool(vision=False),
            "vision_models": self._model_pool(vision=True),
            "catalog_models": sorted(catalog),
            "rate_limited": list(_rate_limited.keys()),
            "total_text": len(self._model_pool(vision=False)),
            "total_vision": len(self._model_pool(vision=True)),
            "catalog_count": len(catalog),
        }

client = OpenRouterClient()

if __name__ == "__main__":
    print("=" * 55)
    print("  Brahma Evo — OpenRouter Client Self-Test")
    print("=" * 55)

    print("\n[TEST 1] Basic chat...")
    try:
        reply = client.chat("Introduce yourself in one sentence.")
        print(f"  Response : {reply}")
        print(f"  Status   : PASS ✓")
    except Exception as e:
        print(f"  Status   : FAIL ✗ — {e}")

    print("\n[TEST 2] JSON mode...")
    try:
        data = client.chat_json(
            'List 3 programming languages. Format: {"languages": ["a", "b", "c"]}',
            system="Return only valid JSON. No extra text."
        )
        print(f"  Response : {data}")
        print(f"  Status   : PASS ✓")
    except Exception as e:
        print(f"  Status   : FAIL ✗ — {e}")

    print("\n[TEST 3] Multi-turn conversation...")
    try:
        history = [
            {"role": "system",    "content": "You are a helpful assistant. Be brief."},
            {"role": "user",      "content": "My name is User."},
            {"role": "assistant", "content": "Hello User, how can I help you?"},
            {"role": "user",      "content": "What is my name?"},
        ]
        reply = client.multi_turn(history)
        print(f"  Response : {reply}")
        print(f"  Status   : PASS ✓")
    except Exception as e:
        print(f"  Status   : FAIL ✗ — {e}")

    print("\n[TEST 4] Model pool info...")
    info = client.available_models()
    print(f"  Text models   : {info['total_text']}")
    print(f"  Vision models : {info['total_vision']}")
    print(f"  Rate limited  : {info['rate_limited'] or 'none'}")
    print(f"  Status        : PASS ✓")

    print("\n" + "=" * 55)
    print("  All tests complete.")
    print("=" * 55)

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any, Optional
import yaml

DEFAULT_LOCAL_MODELS = [
    {
        "id": "phi4mini_q4",
        "label": "Phi-4-mini Instruct (Q4_K_M, bundled)",
        "filename": "microsoft_Phi-4-mini-instruct-Q4_K_M.gguf",
        "tier": "quality",
        "default": True,
        "bundle": True,
        "license": "MIT",
        "download_url": "https://huggingface.co/bartowski/microsoft_Phi-4-mini-instruct-GGUF/resolve/main/microsoft_Phi-4-mini-instruct-Q4_K_M.gguf",
    },
    {
        "id": "qwen25_15b_q4",
        "label": "Qwen2.5 1.5B Instruct (Q4_K_M, speed)",
        "filename": "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
        "tier": "speed",
        "download_url": "https://huggingface.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
    },
    {
        "id": "qwen25_3b_q4",
        "label": "Qwen2.5 3B Instruct (Q4_K_M, balanced)",
        "filename": "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
        "tier": "balanced",
        "download_url": "https://huggingface.co/bartowski/Qwen2.5-3B-Instruct-GGUF/resolve/main/Qwen2.5-3B-Instruct-Q4_K_M.gguf",
    },
    {
        "id": "smollm2_17b_q4",
        "label": "SmolLM2 1.7B Instruct (Q4_K_M, tiny)",
        "filename": "SmolLM2-1.7B-Instruct-Q4_K_M.gguf",
        "tier": "tiny",
        "license": "Apache-2.0",
        "download_url": "https://huggingface.co/bartowski/SmolLM2-1.7B-Instruct-GGUF/resolve/main/SmolLM2-1.7B-Instruct-Q4_K_M.gguf",
    },
]

_MODEL_CACHE_LOCK = threading.Lock()
_MODEL_CACHE_KEY: Optional[tuple[str, str, int, int, int]] = None
_MODEL_CACHE_OBJ = None

APP_NAME = "WhisperDictate"


def _this_dir() -> Path:
    return Path(__file__).resolve().parent


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _runtime_base_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return _this_dir()


def _default_app_support_dir() -> Path:
    if not _is_frozen():
        return _this_dir()
    return (Path.home() / "Library" / "Application Support" / APP_NAME).resolve()


def _app_support_dir() -> Path:
    custom = (os.environ.get("APP_SUPPORT_DIR") or "").strip()
    if custom:
        path = Path(custom).expanduser()
        return path if path.is_absolute() else (Path.cwd() / path).resolve()
    return _default_app_support_dir()


def _to_int(value: str, default: int) -> int:
    try:
        return int((value or "").strip())
    except Exception:
        return default


def _registry_path() -> Path:
    custom = (os.environ.get("LOCAL_MODELS_REGISTRY") or "").strip()
    if custom:
        path = Path(custom).expanduser()
        return path if path.is_absolute() else (_app_support_dir() / path).resolve()
    return _app_support_dir() / "local_models.json"


def _credentials_path() -> Path:
    custom = (os.environ.get("WHISPER_CREDENTIALS_PATH") or "").strip()
    if custom:
        path = Path(custom).expanduser()
        return path if path.is_absolute() else (_app_support_dir() / path).resolve()
    return _app_support_dir() / "credentials.yaml"


def _normalize_local_models(raw_models: Any) -> list[dict[str, Any]]:
    if isinstance(raw_models, str):
        raw_models = [raw_models]
    if not isinstance(raw_models, list):
        return []

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_models:
        if isinstance(item, str):
            model_id = item.strip()
            label = model_id
            filename = ""
            tier = ""
            default = False
            download_url = ""
            license_name = ""
            bundle = False
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
            label = str(item.get("label") or model_id).strip()
            filename = str(item.get("filename") or item.get("file") or "").strip()
            tier = str(item.get("tier") or "").strip()
            default = bool(item.get("default", False))
            download_url = str(item.get("download_url") or item.get("url") or "").strip()
            license_name = str(item.get("license") or item.get("license_name") or "").strip()
            bundle = bool(item.get("bundle", False))
        else:
            continue

        if not model_id:
            continue
        if model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        out.append(
            {
                "id": model_id,
                "label": label or model_id,
                "filename": filename,
                "tier": tier,
                "default": default,
                "download_url": download_url,
                "license": license_name,
                "bundle": bundle,
            }
        )
    return out


def _load_credentials_models() -> list[dict[str, Any]]:
    path = _credentials_path()
    if not path.exists():
        return []
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    providers = payload.get("providers")
    if not isinstance(providers, dict):
        return []
    local = providers.get("local")
    if not isinstance(local, dict):
        return []
    return _normalize_local_models(local.get("models"))


def _load_registry_models() -> list[dict[str, Any]]:
    path = _registry_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    return _normalize_local_models(payload.get("models"))


def available_local_models() -> list[dict[str, Any]]:
    models = _load_credentials_models()
    if models:
        return models
    models = _load_registry_models()
    return models or list(DEFAULT_LOCAL_MODELS)


def default_local_model_id() -> str:
    models = available_local_models()
    for item in models:
        if item.get("default"):
            return str(item.get("id") or "").strip()
    return str(models[0].get("id") or "").strip() if models else ""


def _lookup_model(model_id: str) -> Optional[dict[str, Any]]:
    wanted = (model_id or "").strip()
    if not wanted:
        return None
    for item in available_local_models():
        if str(item.get("id") or "").strip() == wanted:
            return item
    return None


def _candidate_paths(model_id: str) -> list[Path]:
    model_id = (model_id or "").strip()
    if not model_id:
        return []

    entry = _lookup_model(model_id)
    model_filename = ""
    if entry is not None:
        model_filename = str(entry.get("filename") or "").strip()
    if not model_filename:
        model_filename = model_id if model_id.endswith(".gguf") else f"{model_id}.gguf"

    candidates: list[Path] = []

    explicit = (os.environ.get("LOCAL_MODEL_PATH") or "").strip()
    if explicit:
        explicit_path = Path(explicit).expanduser()
        candidates.append(explicit_path if explicit_path.is_absolute() else (_app_support_dir() / explicit_path))

    model_dir_raw = (os.environ.get("LOCAL_MODELS_DIR") or "models").strip()
    model_dir = Path(model_dir_raw).expanduser()
    if model_dir.is_absolute():
        candidates.append(model_dir / model_filename)
    else:
        candidates.extend([
            _app_support_dir() / model_dir / model_filename,
            _runtime_base_dir() / model_dir / model_filename,
            _this_dir() / model_dir / model_filename,
            Path.cwd() / model_dir / model_filename,
        ])

    candidates.extend([
        _app_support_dir() / "models" / model_filename,
        _runtime_base_dir() / "models" / model_filename,
        _this_dir() / "models" / model_filename,
        _app_support_dir() / model_filename,
        _runtime_base_dir() / model_filename,
        _this_dir() / model_filename,
    ])

    unique: list[Path] = []
    seen = set()
    for c in candidates:
        key = str(c.resolve()) if c.exists() else str(c)
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique


def resolve_local_model_path(model_id: str) -> Path:
    candidates = _candidate_paths(model_id)
    if not candidates:
        return Path("")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def is_local_model_downloaded(model_id: str) -> bool:
    """True if the .gguf file for this model id is present on disk (downloaded
    or bundled). Used by the GUI to only offer / mark models that exist."""
    try:
        return resolve_local_model_path(model_id).is_file()
    except Exception:
        return False


def _load_llm(model_id: str):
    from llama_cpp import Llama

    effective_id = (model_id or "").strip() or default_local_model_id()
    if not effective_id:
        raise RuntimeError("LOCAL_MODEL_ID is not set.")
    model_path = resolve_local_model_path(effective_id)
    if not model_path or not model_path.exists():
        raise RuntimeError(f"Local model not found: id={effective_id} path={model_path}")

    n_ctx = _to_int(os.environ.get("LOCAL_CTX_SIZE", "2048"), 2048)
    n_threads = _to_int(os.environ.get("LOCAL_THREADS", "0"), 0)
    n_gpu_layers = _to_int(os.environ.get("LOCAL_GPU_LAYERS", "-1"), -1)
    key = (effective_id, str(model_path), n_ctx, n_threads, n_gpu_layers)

    global _MODEL_CACHE_KEY, _MODEL_CACHE_OBJ
    with _MODEL_CACHE_LOCK:
        if _MODEL_CACHE_OBJ is not None and _MODEL_CACHE_KEY == key:
            return _MODEL_CACHE_OBJ
        kwargs: dict[str, Any] = {
            "model_path": str(model_path),
            "n_ctx": max(256, n_ctx),
            "n_gpu_layers": n_gpu_layers,
            "verbose": False,
        }
        if n_threads > 0:
            kwargs["n_threads"] = n_threads
        llm = Llama(**kwargs)
        _MODEL_CACHE_KEY = key
        _MODEL_CACHE_OBJ = llm
        return llm


def _infer_text(llm, instruction: str, text: str, max_tokens: int, temperature: float) -> str:
    try:
        resp = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": text},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        out = (
            resp.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        if out:
            return str(out).strip()
    except Exception:
        pass

    prompt = f"{instruction}\n\nText:\n{text}\n\nKorrigierter Text:\n"
    resp = llm.create_completion(
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        stop=["</s>"],
    )
    out = resp.get("choices", [{}])[0].get("text", "")
    return str(out).strip()


def refine_local_text(
    text: str,
    instruction: str,
    model_id: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: float = 0.2,
    timeout_sec: Optional[int] = None,
) -> str:
    if not text:
        return text
    llm = _load_llm(model_id or "")
    max_tok = max_tokens if (max_tokens is not None and max_tokens > 0) else _to_int(os.environ.get("LOCAL_MAX_TOKENS", "512"), 512)
    timeout = timeout_sec if (timeout_sec is not None and timeout_sec > 0) else _to_int(os.environ.get("LOCAL_TIMEOUT_SEC", "15"), 15)

    def _run() -> str:
        return _infer_text(llm, instruction, text, max_tok, temperature)

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_run)
        try:
            out = fut.result(timeout=timeout)
        except FuturesTimeoutError:
            raise RuntimeError(f"Local LLM timed out after {timeout}s")
    return out or text

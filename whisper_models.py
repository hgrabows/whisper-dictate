"""Catalog and downloader for whisper.cpp (GGML) speech models.

Kept free of GUI/macOS dependencies so it can be unit-tested and reused by the
CLI. The app resolves the on-disk location of each model itself; this module
only knows what can be downloaded and how to fetch + verify it safely.
"""

from __future__ import annotations

import hashlib
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from i18n import tr

_HF_WHISPER_CPP = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
_HF_DE_TURBO = "https://huggingface.co/cstr/whisper-large-v3-turbo-german-ggml/resolve/main"
# Pinned to the revision cactus-compute 4.2.x verifies against.
_HF_WHISTLE = "https://huggingface.co/Cactus-Compute/whistle/resolve/88cb00eeb8928ddbcd410b491584f938ba030b07"

ENGINE_WHISPER_CPP = "whisper.cpp"
ENGINE_WHISTLE = "whistle"


@dataclass(frozen=True)
class WhisperModelSpec:
    key: str
    label_de: str
    label_en: str
    description_de: str
    description_en: str
    filename: str
    url: str
    size_bytes: int
    sha256: str
    engine: str = ENGINE_WHISPER_CPP

    # Resolved on access (not at import) so they follow the UI language that
    # the app sets after this module has been imported.
    @property
    def label(self) -> str:
        return tr(self.label_de, self.label_en)

    @property
    def description(self) -> str:
        return tr(self.description_de, self.description_en)


# Order = display order in the GUI. Sizes and SHA-256 hashes are the values
# Hugging Face reports for the LFS objects (x-linked-size / x-linked-etag).
WHISPER_MODEL_CATALOG: dict[str, WhisperModelSpec] = {
    spec.key: spec
    for spec in (
        WhisperModelSpec(
            key="large-turbo",
            label_de="large-v3-turbo (empfohlen)",
            label_en="large-v3-turbo (recommended)",
            description_de="OpenAI large-v3-turbo, q5_0 – Qualität wie large, ca. 2× schneller, klein",
            description_en="OpenAI large-v3-turbo, q5_0 – large-level quality, about 2× faster, small",
            filename="ggml-large-v3-turbo-q5_0.bin",
            url=f"{_HF_WHISPER_CPP}/ggml-large-v3-turbo-q5_0.bin",
            size_bytes=574_041_195,
            sha256="394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
        ),
        # Measured on real German dictations (M1 Max): 0.03-0.5 s instead of
        # 0.8-1.6 s, everyday German nearly on par with turbo, but clearly
        # weaker on English tech terms ("Main Ector", "Non-Eisolated").
        WhisperModelSpec(
            key="whistle",
            label_de="Whistle – schnell",
            label_en="Whistle – fast",
            description_de="Cactus Whistle, läuft direkt in der App – sehr schnell, schwächer bei Fachbegriffen",
            description_en="Cactus Whistle, runs inside the app – very fast, weaker on technical terms",
            filename="whistle.cact",
            url=f"{_HF_WHISTLE}/whistle.cact",
            size_bytes=16_919_407,
            sha256="b6e02f048568ac5d01a2042556c658061e699acbc0aa2a1439f52f3d461dffeb",
            engine=ENGINE_WHISTLE,
        ),
        # Measured: writes Swiss orthography (ss instead of ß: "Strasse",
        # "Grösse") and mangles English tech terms ("deployen" -> "decloyen").
        # Kept for Swiss users; not recommended for German from Germany.
        WhisperModelSpec(
            key="de-turbo",
            label_de="Deutsch turbo (schreibt ss statt ß)",
            label_en="German turbo (writes ss instead of ß)",
            description_de="large-v3-turbo, deutsch feinjustiert (primeline), q5_0 – schreibt immer ss statt ß",
            description_en="large-v3-turbo fine-tuned for German (primeline), q5_0 – always writes ss instead of ß",
            filename="ggml-large-v3-turbo-german-q5_0.bin",
            url=f"{_HF_DE_TURBO}/ggml-model-q5_0.bin",
            size_bytes=574_041_195,
            sha256="15e92e3db0993c52fffa781513eec9253475331c1be808f8fb409285c9d9d030",
        ),
        WhisperModelSpec(
            key="large",
            label_de="large-v3 (mehrsprachig, genau)",
            label_en="large-v3 (multilingual, accurate)",
            description_de="OpenAI large-v3, volle Präzision – langsam, groß",
            description_en="OpenAI large-v3, full precision – slow, large",
            filename="ggml-large-v3.bin",
            url=f"{_HF_WHISPER_CPP}/ggml-large-v3.bin",
            size_bytes=3_095_033_483,
            sha256="64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2",
        ),
        WhisperModelSpec(
            key="medium",
            label_de="medium (mehrsprachig)",
            label_en="medium (multilingual)",
            description_de="OpenAI medium – in der App enthalten",
            description_en="OpenAI medium – included in the app",
            filename="ggml-medium.bin",
            url=f"{_HF_WHISPER_CPP}/ggml-medium.bin",
            size_bytes=1_533_763_059,
            sha256="6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208",
        ),
    )
}

# Preference order when the requested model is missing on disk. `medium` comes
# first because it is the one bundled with the .app.
WHISPER_FALLBACK_ORDER: tuple[str, ...] = ("medium", "large-turbo", "large", "de-turbo", "whistle")


class DownloadCancelled(Exception):
    """Raised when a download was cancelled via its cancel event."""


def format_size(size_bytes: int) -> str:
    if size_bytes >= 1_000_000_000:
        return f"{size_bytes / 1_000_000_000:.1f} GB"
    return f"{size_bytes / 1_000_000:.0f} MB"


ProgressCallback = Callable[[int, int], None]


def download_whisper_model(
    spec: WhisperModelSpec,
    target_dir: Path,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    opener: Callable[..., object] = urllib.request.urlopen,
    chunk_size: int = 1024 * 1024,
    timeout: float = 60.0,
) -> Path:
    """Download `spec` into `target_dir` and return the final path.

    The file is streamed into a hidden `.part` file, checked against the
    catalog's size and SHA-256, and only then atomically moved into place, so
    an interrupted or corrupt download never replaces a working model.
    """
    if not spec.url.lower().startswith("https://"):
        raise ValueError(f"Only https downloads are allowed: {spec.url}")

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / spec.filename
    part_path = target_dir / f".{spec.filename}.part"

    digest = hashlib.sha256()
    downloaded = 0
    try:
        req = urllib.request.Request(spec.url, headers={"User-Agent": "WhisperDictate/1.0"})
        with opener(req, timeout=timeout) as resp, part_path.open("wb") as fout:
            header_total = int(resp.headers.get("Content-Length", "0") or "0")
            total = header_total or spec.size_bytes
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled()
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                fout.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                if progress_cb is not None:
                    progress_cb(downloaded, total)

        if spec.size_bytes and downloaded != spec.size_bytes:
            raise ValueError(
                f"Incomplete download: received {downloaded} of {spec.size_bytes} bytes."
            )
        actual = digest.hexdigest()
        if spec.sha256 and actual != spec.sha256:
            raise ValueError("Checksum mismatch (SHA-256) – file discarded.")
        part_path.replace(target_path)
        return target_path
    except BaseException:
        try:
            part_path.unlink()
        except FileNotFoundError:
            pass
        raise

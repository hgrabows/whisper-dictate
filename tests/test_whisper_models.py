import hashlib
import io
import threading
from dataclasses import replace

import pytest

from whisper_models import (
    ENGINE_WHISPER_CPP,
    ENGINE_WHISTLE,
    WHISPER_FALLBACK_ORDER,
    WHISPER_MODEL_CATALOG,
    DownloadCancelled,
    download_whisper_model,
    format_size,
)


class _FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, content_length: bool = True) -> None:
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))} if content_length else {}


def _spec_for(payload: bytes, **overrides):
    base = WHISPER_MODEL_CATALOG["de-turbo"]
    return replace(
        base,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        **overrides,
    )


def _opener_for(payload: bytes):
    def _open(req, timeout=None):
        return _FakeResponse(payload)

    return _open


def test_catalog_is_consistent() -> None:
    assert set(WHISPER_FALLBACK_ORDER) == set(WHISPER_MODEL_CATALOG)
    filenames = [spec.filename for spec in WHISPER_MODEL_CATALOG.values()]
    assert len(filenames) == len(set(filenames))
    for key, spec in WHISPER_MODEL_CATALOG.items():
        assert spec.key == key
        assert spec.url.startswith("https://")
        expected_suffix = ".cact" if spec.engine == ENGINE_WHISTLE else ".bin"
        assert spec.filename.endswith(expected_suffix)
        assert spec.engine in {ENGINE_WHISPER_CPP, ENGINE_WHISTLE}
        assert len(spec.sha256) == 64
        assert spec.size_bytes > 0


def test_download_writes_verified_file(tmp_path) -> None:
    payload = b"ggml" * 1000
    spec = _spec_for(payload)
    progress = []

    path = download_whisper_model(
        spec,
        tmp_path,
        opener=_opener_for(payload),
        chunk_size=512,
        progress_cb=lambda done, total: progress.append((done, total)),
    )

    assert path == tmp_path / spec.filename
    assert path.read_bytes() == payload
    assert progress[-1] == (len(payload), len(payload))
    assert not list(tmp_path.glob(".*.part"))


def test_checksum_mismatch_keeps_existing_model(tmp_path) -> None:
    payload = b"corrupt-data"
    spec = replace(_spec_for(payload), sha256="0" * 64)
    existing = tmp_path / spec.filename
    existing.write_bytes(b"working model")

    with pytest.raises(ValueError, match="Checksum"):
        download_whisper_model(spec, tmp_path, opener=_opener_for(payload))

    assert existing.read_bytes() == b"working model"
    assert not list(tmp_path.glob(".*.part"))


def test_truncated_download_is_rejected(tmp_path) -> None:
    payload = b"x" * 100
    spec = replace(_spec_for(payload), size_bytes=200)

    with pytest.raises(ValueError, match="Incomplete"):
        download_whisper_model(spec, tmp_path, opener=_opener_for(payload))

    assert not (tmp_path / spec.filename).exists()


def test_cancel_removes_partial_file(tmp_path) -> None:
    payload = b"y" * 4096
    spec = _spec_for(payload)
    cancel = threading.Event()

    def _progress(done, total):
        cancel.set()

    with pytest.raises(DownloadCancelled):
        download_whisper_model(
            spec,
            tmp_path,
            opener=_opener_for(payload),
            chunk_size=1024,
            progress_cb=_progress,
            cancel_event=cancel,
        )

    assert not (tmp_path / spec.filename).exists()
    assert not list(tmp_path.glob(".*.part"))


def test_rejects_non_https_url(tmp_path) -> None:
    spec = _spec_for(b"z", url="http://example.com/model.bin")
    with pytest.raises(ValueError, match="https"):
        download_whisper_model(spec, tmp_path, opener=_opener_for(b"z"))


def test_format_size() -> None:
    assert format_size(574_041_195) == "574 MB"
    assert format_size(3_095_033_483) == "3.1 GB"

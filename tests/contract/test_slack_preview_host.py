"""Cached Slack previews remain usable by Hermes' actual local vision path."""
import asyncio
import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from inkbox_plugin.slack import download_file_preview


@pytest.mark.parametrize("image_format,mimetype", [
    ("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp"),
])
def test_real_cache_to_vision(monkeypatch, tmp_path, image_format, mimetype):
    import gateway.platforms.base as host
    if not hasattr(host, "cache_image_from_bytes"):
        pytest.skip("Real Hermes image cache is not installed")
    image = pytest.importorskip("PIL.Image")
    vision = pytest.importorskip("tools.vision_tools")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cache = tmp_path / "cache" / "images"
    cache.mkdir(parents=True)
    monkeypatch.setattr(host, "get_image_cache_dir", lambda: cache)
    buffer = io.BytesIO()
    image.new("RGB", (2, 2), "blue").save(buffer, format=image_format)
    data = buffer.getvalue()
    resource = SimpleNamespace(download_file_preview=lambda connection, file: data)
    result = download_file_preview(resource, "connection", "FEXAMPLE")
    path = Path(result["file_path"])
    assert path.is_absolute() and path.parent == cache
    assert path.read_bytes() == data
    assert result["mimetype"] == mimetype
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    prepared = asyncio.run(vision._prepare_image(str(path), None, None, validate_decode=True))
    try:
        assert prepared.mime == mimetype
    finally:
        prepared.path.unlink(missing_ok=True)

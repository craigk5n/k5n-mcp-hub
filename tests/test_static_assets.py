import struct
from pathlib import Path
from xml.etree import ElementTree

import pytest


STATIC_DIR = Path(__file__).parent.parent / "src" / "devhub" / "static"
IMAGES_DIR = STATIC_DIR / "images"


class TestStaticAssets:
    def test_images_directory_exists(self) -> None:
        assert IMAGES_DIR.exists(), f"Expected {IMAGES_DIR} to exist"
        assert IMAGES_DIR.is_dir(), f"Expected {IMAGES_DIR} to be a directory"

    def test_favicon_exists_and_non_empty(self) -> None:
        favicon_path = IMAGES_DIR / "favicon.ico"
        assert favicon_path.exists(), f"Expected {favicon_path} to exist"
        content = favicon_path.read_bytes()
        assert len(content) > 0, "favicon.ico must be non-empty"

    def test_favicon_is_valid_ico_format(self) -> None:
        favicon_path = IMAGES_DIR / "favicon.ico"
        content = favicon_path.read_bytes()
        assert len(content) >= 22, "ICO file must be at least 22 bytes (header + 1 image entry)"
        magic = content[0:4]
        assert magic == b"\x00\x00\x01\x00", f"Invalid ICO magic bytes: {magic!r}"
        num_images = struct.unpack("<H", content[4:6])[0]
        assert num_images >= 1, f"Expected at least 1 image in ICO, got {num_images}"

    def test_logo_exists_and_non_empty(self) -> None:
        logo_path = IMAGES_DIR / "logo.svg"
        assert logo_path.exists(), f"Expected {logo_path} to exist"
        content = logo_path.read_bytes()
        assert len(content) > 0, "logo.svg must be non-empty"

    def test_logo_is_valid_svg(self) -> None:
        logo_path = IMAGES_DIR / "logo.svg"
        content = logo_path.read_bytes()
        text = content.decode("utf-8")
        assert "<svg" in text.lower(), "logo.svg must contain <svg> element"
        ElementTree.fromstring(text)

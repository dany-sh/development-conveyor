from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from development_conveyor.context_pack import (
    ContextReadError,
    build_context_pack,
    read_context_file,
)


class ContextPackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative: str, payload: bytes) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def test_output_png_is_excluded_before_decode(self):
        self.write("output/test-call.png", b"\x89PNG\r\n\x1a\n\xff\x00")
        pack = build_context_pack(
            self.root, ["output/test-call.png"], phase="test"
        )
        self.assertEqual(pack.rendered, "")
        self.assertEqual(
            pack.evidence["excluded_generated_paths"][0]["path"],
            "output/test-call.png",
        )

    def test_tracked_png_asset_is_bounded_metadata(self):
        self.write("Assets/AppIcon.png", b"\x89PNG\r\n\x1a\n" + b"x" * 64)
        pack = build_context_pack(
            self.root, ["Assets/AppIcon.png"], phase="test"
        )
        metadata = pack.evidence["binary_metadata_paths"][0]
        self.assertEqual(metadata["detected_type"], "png")
        self.assertEqual(metadata["size"], 72)
        self.assertIn("binary_metadata", pack.rendered)
        self.assertNotIn("iVBOR", pack.rendered)

    def test_binary_with_markdown_extension_is_detected_from_bytes(self):
        self.write("docs/not-text.md", b"\x89PNG\r\n\x1a\npayload")
        result = read_context_file(
            self.root, "docs/not-text.md", phase="selection"
        )
        self.assertEqual(result.classification, "binary")
        self.assertEqual(result.detected_type, "png")

    def test_invalid_utf8_text_candidate_has_typed_path_and_phase(self):
        self.write("docs/broken.md", b"heading\n\xff")
        with self.assertRaises(ContextReadError) as captured:
            read_context_file(
                self.root, "docs/broken.md", phase="prompt_render"
            )
        self.assertEqual(captured.exception.relative_path, "docs/broken.md")
        self.assertEqual(captured.exception.phase, "prompt_render")
        self.assertEqual(
            captured.exception.classification,
            "invalid_utf8_textual_candidate",
        )

    def test_utf8_source_and_documentation_remain_included(self):
        self.write("src/app.py", "print('café')\n".encode())
        self.write("docs/README.md", b"# Read me\n")
        pack = build_context_pack(
            self.root, ["src/app.py", "docs/README.md"], phase="test"
        )
        self.assertEqual(
            pack.evidence["included_textual_paths"],
            ["src/app.py", "docs/README.md"],
        )
        self.assertIn("café", pack.rendered)

    def test_oversized_text_is_reported_without_prompt_payload(self):
        self.write("src/large.py", b"a" * 65)
        pack = build_context_pack(
            self.root,
            ["src/large.py"],
            phase="test",
            max_text_bytes=64,
        )
        self.assertEqual(pack.rendered, "")
        self.assertEqual(pack.evidence["oversized_paths"][0]["size"], 65)

    def test_evidence_fingerprint_is_stable_and_no_lossy_fallback_exists(self):
        self.write("src/app.py", b"print('ok')\n")
        first = build_context_pack(self.root, ["src/app.py"], phase="test")
        second = build_context_pack(self.root, ["src/app.py"], phase="test")
        self.assertEqual(
            first.evidence["context_pack_fingerprint"],
            second.evidence["context_pack_fingerprint"],
        )
        self.assertEqual(first.evidence["typed_read_failures"], [])

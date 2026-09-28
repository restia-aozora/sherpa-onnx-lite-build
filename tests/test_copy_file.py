import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_support import copy_file


class CopyFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "sherpa-onnx/c-api/c-api.h"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"/* C API header */\n")

    def test_preserves_harmony_header_symlink_to_source(self):
        destination = self.root / (
            "harmony-os/SherpaOnnxHar/sherpa_onnx/src/main/cpp/"
            "include/sherpa-onnx/c-api/c-api.h"
        )
        destination.parent.mkdir(parents=True)
        relative_target = "../../../../../../../../../sherpa-onnx/c-api/c-api.h"
        try:
            destination.symlink_to(relative_target)
        except OSError as error:
            if os.name == "nt" and error.winerror == 1314:
                self.skipTest("Windows requires symlink privileges; Linux CI runs this test")
            raise
        self.assertTrue(destination.samefile(self.source))

        copy_file(self.source, destination)

        self.assertTrue(destination.is_symlink())
        self.assertEqual(os.readlink(destination), relative_target)
        self.assertEqual(destination.read_bytes(), b"/* C API header */\n")

    def test_accepts_identical_source_and_destination(self):
        copy_file(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), b"/* C API header */\n")

    def test_preserves_hard_link_to_source(self):
        destination = self.root / "linked-header.h"
        destination.hardlink_to(self.source)

        copy_file(self.source, destination)

        self.assertTrue(destination.samefile(self.source))
        self.assertEqual(destination.read_bytes(), b"/* C API header */\n")

    def test_creates_parent_directories_and_copies_new_file(self):
        destination = self.root / "output/include/c-api.h"
        copy_file(self.source, destination)
        self.assertEqual(destination.read_bytes(), self.source.read_bytes())
        self.assertFalse(destination.samefile(self.source))

    def test_overwrites_different_existing_file(self):
        destination = self.root / "old-header.h"
        destination.write_text("old header", encoding="utf-8")
        copy_file(self.source, destination)
        self.assertEqual(destination.read_bytes(), self.source.read_bytes())

    def test_missing_source_is_not_silently_ignored(self):
        missing = self.root / "missing.h"
        with self.assertRaises(FileNotFoundError):
            copy_file(missing, missing)


if __name__ == "__main__":
    unittest.main()

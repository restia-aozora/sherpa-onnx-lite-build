import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_support


class HarmonySdkTests(unittest.TestCase):
    def test_extracts_only_native_sdk_from_command_line_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            archive = work / "sdk.zip"
            prefix = "command-line-tools/sdk/default/openharmony/native"
            with zipfile.ZipFile(archive, "w") as bundle:
                for relative in ("build/cmake/ohos.toolchain.cmake", "llvm/bin/clang",
                                 "llvm/bin/llvm-readelf", "sysroot/usr/include/stdio.h"):
                    bundle.writestr(f"{prefix}/{relative}", relative)
                bundle.writestr("command-line-tools/tool/node/bin/node", "unused")
            native = build_support.extract_harmony_native_sdk(archive, work / "sdk")
            self.assertEqual(native, work / "sdk" / prefix)
            self.assertEqual((native / "llvm/bin/clang").read_text(), "llvm/bin/clang")
            self.assertTrue((native / "sysroot/usr/include/stdio.h").is_file())
            self.assertFalse((work / "sdk/command-line-tools/tool").exists())

    def test_accepts_standalone_native_sdk(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            archive = work / "sdk.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("native/build/cmake/ohos.toolchain.cmake", "toolchain")
            native = build_support.extract_harmony_native_sdk(archive, work / "sdk")
            self.assertEqual(native, work / "sdk/native")

    def test_rejects_missing_or_ambiguous_native_sdk(self):
        for prefixes in ([], ["sdk-one/native", "sdk-two/native"]):
            with self.subTest(prefixes=prefixes), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                archive = work / "sdk.zip"
                with zipfile.ZipFile(archive, "w") as bundle:
                    for prefix in prefixes:
                        bundle.writestr(f"{prefix}/build/cmake/ohos.toolchain.cmake", "toolchain")
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    build_support.extract_harmony_native_sdk(archive, work / "sdk")
                self.assertFalse((work / "sdk").exists())

    def test_rejects_native_archive_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            archive = work / "sdk.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../native/build/cmake/ohos.toolchain.cmake", "toolchain")
            with self.assertRaises(ValueError):
                build_support.extract_harmony_native_sdk(archive, work / "sdk")
            self.assertFalse((work / "native").exists())

    def test_rejects_symlink_outside_selected_native_subtree(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            archive = work / "sdk.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("native/build/cmake/ohos.toolchain.cmake", "toolchain")
                link = zipfile.ZipInfo("native/llvm/bin/clang")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                bundle.writestr(link, "../../../other-tool")
            with self.assertRaisesRegex(ValueError, "Unsafe ZIP symlink"):
                build_support.extract_harmony_native_sdk(archive, work / "sdk")

    @unittest.skipIf(os.name == "nt", "Unix symlink and executable-mode semantics run in Linux CI")
    def test_preserves_native_compiler_symlinks_and_executable_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            archive = work / "sdk.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("native/build/cmake/ohos.toolchain.cmake", "toolchain")
                compiler = zipfile.ZipInfo("native/llvm/bin/clang-15")
                compiler.create_system = 3
                compiler.external_attr = (stat.S_IFREG | 0o755) << 16
                bundle.writestr(compiler, "compiler")
                link = zipfile.ZipInfo("native/llvm/bin/clang")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                bundle.writestr(link, "clang-15")
            native = build_support.extract_harmony_native_sdk(archive, work / "sdk")
            self.assertTrue((native / "llvm/bin/clang").is_symlink())
            self.assertEqual((native / "llvm/bin/clang").read_text(), "compiler")
            self.assertTrue(os.access(native / "llvm/bin/clang", os.X_OK))


if __name__ == "__main__":
    unittest.main()

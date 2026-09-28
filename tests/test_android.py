import contextlib
import io
import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import android_aar
import artifacts
import build
from build_support import LOCK


def write_fixture_elf(filename, abi, alignment=16384):
    filename.parent.mkdir(parents=True, exist_ok=True)
    if abi == "arm64-v8a":
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", header, 18, 183)
        struct.pack_into("<Q", header, 32, 64)
        struct.pack_into("<HH", header, 54, 56, 1)
        segment = bytearray(56)
        struct.pack_into("<Q", segment, 48, alignment)
    else:
        header = bytearray(52)
        header[:6] = b"\x7fELF\x01\x01"
        struct.pack_into("<H", header, 18, 40)
        struct.pack_into("<I", header, 28, 52)
        struct.pack_into("<HH", header, 42, 32, 1)
        segment = bytearray(32)
        struct.pack_into("<I", segment, 28, alignment)
    struct.pack_into("<I", segment, 0, 1)
    filename.write_bytes(header + segment)


class AndroidTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.libraries = self.root / "jni"
        for abi in android_aar.ANDROID_ABIS:
            write_fixture_elf(self.libraries / abi / "libsherpa-onnx-jni.so", abi)

    def make_aar(self, abis, *, missing_class=None, extra_class=None, keep_rule=True,
                 licenses=True, minimum_sdk=23, corrupt_abi=None):
        # These minimal fixtures exercise packaging validation, not native/Kotlin compilation.
        compiled = io.BytesIO()
        with zipfile.ZipFile(compiled, "w") as classes:
            selected_classes = android_aar.REQUIRED_CLASSES - {missing_class}
            if extra_class:
                selected_classes = selected_classes | {extra_class}
            for class_name in sorted(selected_classes):
                classes.writestr(f"com/k2fsa/sherpa/onnx/{class_name}.class", b"\xca\xfe\xba\xbe")
        archive = self.root / "fixture.aar"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("classes.jar", compiled.getvalue())
            bundle.writestr("AndroidManifest.xml",
                            '<manifest xmlns:android="http://schemas.android.com/apk/res/android">'
                            f'<uses-sdk android:minSdkVersion="{minimum_sdk}" /></manifest>')
            bundle.writestr("proguard.txt", android_aar.JNI_KEEP_RULE if keep_rule else "")
            if licenses:
                bundle.writestr("assets/sherpa-onnx-lite/licenses/sherpa/LICENSE", "fixture notice")
            for abi in abis:
                payload = (self.libraries / abi / "libsherpa-onnx-jni.so").read_bytes()
                bundle.writestr(f"jni/{abi}/libsherpa-onnx-jni.so",
                                payload + b"corrupt" if abi == corrupt_abi else payload)
        return archive

    def test_select_single_or_both_abis_with_stable_order(self):
        self.assertEqual(android_aar.select_android_abis(), ("arm64-v8a",))
        self.assertEqual(android_aar.select_android_abis(["armeabi-v7a"]), ("armeabi-v7a",))
        self.assertEqual(android_aar.select_android_abis(["armeabi-v7a", "arm64-v8a"]),
                         android_aar.ANDROID_ABIS)
        for requested in ([], ["x86_64"], ["arm64-v8a", "arm64-v8a"]):
            with self.subTest(requested=requested), self.assertRaises(ValueError):
                android_aar.select_android_abis(requested)

    def test_elf_accepts_matching_architecture_and_rejects_mislabeled_library(self):
        for abi in android_aar.ANDROID_ABIS:
            library = self.libraries / abi / "libsherpa-onnx-jni.so"
            artifacts.inspect_elf_header(library, abi=abi)
            other_abi = next(candidate for candidate in android_aar.ANDROID_ABIS if candidate != abi)
            with self.assertRaises(ValueError):
                artifacts.inspect_elf_header(library, abi=other_abi)

    def test_arm64_alignment_and_truncated_headers_are_rejected(self):
        library = self.libraries / "arm64-v8a/libsherpa-onnx-jni.so"
        write_fixture_elf(library, "arm64-v8a", alignment=4096)
        with self.assertRaisesRegex(ValueError, "below 16 KB"):
            artifacts.inspect_elf_header(library, abi="arm64-v8a", require_16k=True)
        library.write_bytes(library.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError, "Truncated"):
            artifacts.inspect_elf_header(library)

    def test_one_aar_accepts_each_valid_architecture_selection(self):
        for abis in (("arm64-v8a",), ("armeabi-v7a",), android_aar.ANDROID_ABIS):
            with self.subTest(abis=abis):
                report = android_aar.verify_android_aar(self.make_aar(abis), self.libraries, abis)
                self.assertEqual(report["abis"], list(abis))
                self.assertEqual(report["structureVerification"], "PASSED")

    def test_aar_rejects_extra_or_missing_architecture(self):
        for packed, requested in ((android_aar.ANDROID_ABIS, ("arm64-v8a",)),
                                  (("arm64-v8a",), android_aar.ANDROID_ABIS)):
            with self.subTest(packed=packed), self.assertRaisesRegex(ValueError, "ABI/library mismatch"):
                android_aar.verify_android_aar(self.make_aar(packed), self.libraries, requested)

    def test_aar_rejects_changed_native_bytes(self):
        archive = self.make_aar(android_aar.ANDROID_ABIS, corrupt_abi="armeabi-v7a")
        with self.assertRaisesRegex(ValueError, "changed the verified native library"):
            android_aar.verify_android_aar(archive, self.libraries, android_aar.ANDROID_ABIS)

    def test_aar_rejects_missing_classes_removed_features_or_missing_safety_metadata(self):
        variants = (
            ({"missing_class": "OfflineTts"}, "missing compiled Kotlin API"),
            ({"extra_class": "KeywordSpotter"}, "excluded API"),
            ({"keep_rule": False}, "JNI consumer keep rule"),
            ({"licenses": False}, "third-party licenses"),
            ({"minimum_sdk": 21}, "minimum SDK"),
        )
        for options, message in variants:
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, message):
                android_aar.verify_android_aar(self.make_aar(("arm64-v8a",), **options),
                                               self.libraries, ("arm64-v8a",))

    def test_prepare_project_only_includes_whitelisted_sources_and_selected_abis(self):
        source = self.root / "source"
        upstream = source / "android/SherpaOnnxAar"
        wrapper = upstream / "gradle/wrapper"
        wrapper.mkdir(parents=True)
        (upstream / "gradlew").write_text("fixture wrapper", encoding="utf-8")
        (wrapper / "gradle-wrapper.jar").write_bytes(b"fixture")
        (wrapper / "gradle-wrapper.properties").write_text(
            "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.6-bin.zip\n",
            encoding="utf-8")
        kotlin = source / "sherpa-onnx/kotlin-api"
        kotlin.mkdir(parents=True)
        for filename in (*android_aar.KOTLIN_FILES, "KeywordSpotter.kt"):
            (kotlin / filename).write_text("// fixture\n", encoding="utf-8")
        output = self.root / "package"
        (output / "licenses/sherpa").mkdir(parents=True)
        (output / "licenses/sherpa/LICENSE").write_text("fixture notice", encoding="utf-8")
        metadata = {"sourceCommit": "fixture", "profile": "four-feature-test"}
        project = android_aar.prepare_aar_project(source, self.root / "gradle-project", self.libraries,
                                                  output, ("armeabi-v7a",), metadata)
        main = project / "sherpa_onnx/src/main"
        self.assertEqual({filename.name for filename in main.rglob("*.kt")}, set(android_aar.KOTLIN_FILES))
        self.assertEqual({filename.relative_to(main / "jniLibs").as_posix()
                          for filename in (main / "jniLibs").rglob("*.so")},
                         {"armeabi-v7a/libsherpa-onnx-jni.so"})
        self.assertTrue((main / "assets/sherpa-onnx-lite/licenses/sherpa/LICENSE").is_file())
        self.assertIn(LOCK["android"]["aar"]["gradleSha256"],
                      (project / "gradle/wrapper/gradle-wrapper.properties").read_text())
        with self.assertRaisesRegex(ValueError, "new isolated directory"):
            android_aar.prepare_aar_project(source, project, self.libraries, output, ("armeabi-v7a",), metadata)

    def test_package_records_selected_abis_without_changing_ios_naming(self):
        for platform, metadata, suffix in (
            ("android", {"architectures": list(android_aar.ANDROID_ABIS)}, "arm64-v8a-armeabi-v7a"),
            ("ios", {}, "arm64"),
        ):
            with self.subTest(platform=platform):
                output = self.root / platform
                output.mkdir()
                (output / "fixture.txt").write_text("fixture", encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    archive, report = artifacts.package_output(output, platform, metadata, 55)
                self.assertEqual(archive.name, f"sherpa-onnx-lite-{platform}-{suffix}.zip")
                self.assertTrue(report.is_file())
                manifest = json.loads((output / "manifest.json").read_text())
                self.assertEqual(manifest["architecture"], suffix)
                self.assertFalse(manifest["onnxruntimeOperatorPruning"])

    def test_android_build_isolates_abis_and_only_merges_after_every_native_build_passes(self):
        source = self.root / "source"
        source.mkdir()
        ndk = self.root / "ndk"
        (ndk / "build/cmake").mkdir(parents=True)
        (ndk / "build/cmake/android.toolchain.cmake").touch()
        (ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin").mkdir(parents=True)
        (ndk / "source.properties").write_text("Pkg.Revision = fixture", encoding="utf-8")
        sdk = self.root / "sdk"
        (sdk / "platforms/android-34").mkdir(parents=True)
        (sdk / "platforms/android-34/android.jar").touch()
        environment = {"ANDROID_NDK_HOME": str(ndk), "ANDROID_HOME": str(sdk)}
        metadata = {"retainedCApiSymbols": []}

        def download_fixture(url, digest, destination):
            destination.parent.mkdir(parents=True)
            with zipfile.ZipFile(destination, "w") as bundle:
                bundle.writestr("LICENSE", "fixture notice")
            return destination

        def compile_fixture(source, directory, target, options, environment, jobs):
            write_fixture_elf(directory / "lib/libsherpa-onnx-jni.so", options["ANDROID_ABI"])

        with patch.object(build, "download_verified", side_effect=download_fixture) as downloads, \
             patch.object(build, "configure_and_build", side_effect=compile_fixture) as native_builds, \
             patch.object(build, "verify_elf_package", return_value={}) as verification, \
             patch.object(build, "collect_licenses") as licenses, \
             patch.object(build, "run"), patch.object(build.shutil, "which", return_value="fixture-tool"), \
             patch.object(build, "build_android_aar", return_value=self.root / "combined.aar") as merger:
            result = build.build_android(source, self.root / "work", self.root / "output", environment,
                                         2, metadata, android_aar.ANDROID_ABIS)
            self.assertEqual(result.name, "combined.aar")
            self.assertEqual(len(downloads.call_args_list), 2)
            self.assertEqual(len(licenses.call_args_list), 2)
            self.assertEqual([call.args[3]["ANDROID_ABI"] for call in native_builds.call_args_list],
                             list(android_aar.ANDROID_ABIS))
            self.assertNotEqual(native_builds.call_args_list[0].args[1], native_builds.call_args_list[1].args[1])
            self.assertEqual([call.kwargs["abi"] for call in verification.call_args_list],
                             list(android_aar.ANDROID_ABIS))
            merger.assert_called_once()
            self.assertEqual(merger.call_args.args[4], android_aar.ANDROID_ABIS)
            self.assertEqual(metadata["architectures"], list(android_aar.ANDROID_ABIS))
            merger.reset_mock()
            native_builds.side_effect = ValueError("native build failed")
            with self.assertRaisesRegex(ValueError, "native build failed"):
                build.build_android(source, self.root / "failed-work", self.root / "output", environment,
                                    2, metadata, android_aar.ANDROID_ABIS)
            merger.assert_not_called()


if __name__ == "__main__":
    unittest.main()

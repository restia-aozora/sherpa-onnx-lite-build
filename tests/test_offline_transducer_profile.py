"""Exercise offline source generation; no native build or inference is performed."""

import contextlib
import io
import json
import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_trilingual_profile
import artifacts
import profile_lite


# The pinned upstream has one OfflineTransducerModel, not per-Zipformer classes.
OFFLINE_FACTORY = '''std::unique_ptr<OfflineRecognizerImpl> OfflineRecognizerImpl::Create(
    const OfflineRecognizerConfig &config) {
  return std::make_unique<OfflineRecognizerTransducerNeMoImpl>(config);
}
template <typename Manager>
std::unique_ptr<OfflineRecognizerImpl> OfflineRecognizerImpl::Create(
    Manager *mgr, const OfflineRecognizerConfig &config) {
  return std::make_unique<OfflineRecognizerTransducerNeMoImpl>(mgr, config);
}
#if __ANDROID_API__ >= 9
template std::unique_ptr<OfflineRecognizerImpl> OfflineRecognizerImpl::Create(
    AAssetManager *mgr, const OfflineRecognizerConfig &config);
#endif
#if __OHOS__
template std::unique_ptr<OfflineRecognizerImpl> OfflineRecognizerImpl::Create(
    NativeResourceManager *mgr, const OfflineRecognizerConfig &config);
#endif
'''


class OfflineTransducerProfileTests(unittest.TestCase):
    def setUp(self):
        test_trilingual_profile.TrilingualProfileTests.setUp(self)
        (self.csrc / "offline-recognizer-impl.cc").write_text(OFFLINE_FACTORY, encoding="utf-8")

    def prepare(self, platform="android"):
        with patch.object(profile_lite.subprocess, "check_output",
                          side_effect=[profile_lite.COMMIT, ""]):
            return profile_lite.prepare(self.source, platform)

    def assert_profile(self, platform):
        metadata = self.prepare(platform)
        factory = (self.csrc / "offline-recognizer-impl.cc").read_text(encoding="utf-8")
        # Detect restoring unsupported metadata routes or bypassing the metadata
        # check with the explicit "transducer" hint in either overload.
        self.assertEqual(factory.count('if (model_type == "zipformer" || model_type == "zipformer2")'), 2)
        self.assertEqual(factory.count('LookupCustomModelMetaData(model_metadata, "model_type", allocator)'), 2)
        self.assertEqual(factory.count("std::make_unique<OfflineRecognizerTransducerImpl>"), 2)
        self.assertEqual(re.findall(
            r'if \(model_type == "zipformer" \|\| model_type == "zipformer2"\) \{\s*'
            r'return std::make_unique<OfflineRecognizerTransducerImpl>\((.*?)\);', factory,
        ), ["config", "mgr, config"])
        for arguments in ("config", "mgr, config"):
            for family in ("Paraformer", "SenseVoice", "Transducer"):
                self.assertIn(f"std::make_unique<OfflineRecognizer{family}Impl>({arguments})", factory)
        self.assertIn("SHERPA_ONNX_TO_ORT_PATH(config.model_config.transducer.encoder_filename)", factory)
        self.assertIn("ReadFile(mgr, config.model_config.transducer.encoder_filename)", factory)
        self.assertIn("env, model_data.data(), model_data.size(), session_options", factory)
        self.assertNotIn("OfflineRecognizerTransducerNeMoImpl", factory)
        self.assertNotIn('model_type == "conformer"', factory)
        self.assertIn("AAssetManager *mgr, const OfflineRecognizerConfig &config);", factory)
        self.assertIn("NativeResourceManager *mgr, const OfflineRecognizerConfig &config);", factory)
        # The probe's lexical scope ends before constructing the full triple;
        # otherwise a resource-manager buffer and session retain another encoder.
        self.assertEqual(len(re.findall(
            r'LookupCustomModelMetaData\(model_metadata, "model_type", allocator\);\s*\}'
            r'\s*(?://[^\n]*\n\s*)?if \(model_type == "zipformer"', factory,
        )), 2)
        validation = (self.csrc / "offline-model-config.cc").read_text(encoding="utf-8")
        self.assertIn("transducer.encoder_filename.empty()", validation)
        for unsupported_hint in ('model_type != "transducer"', 'model_type != "zipformer"',
                                 'model_type != "zipformer2"'):
            self.assertIn(unsupported_hint, validation)
        self.assertIn("return upstream_validation();", validation)
        self.assertEqual(metadata["enabledModels"]["offline"], [
            "paraformer", "sense_voice", "zipformer", "zipformer2",
        ])
        self.assertEqual(metadata["enabledModels"]["online"], [
            "paraformer", "zipformer", "zipformer2", "zipformer2_ctc",
        ])
        self.assertEqual(metadata["enabledModels"]["vad"], ["silero_vad"])
        self.assertEqual(metadata["enabledModels"]["tts"], ["vits"])
        self.assertEqual(metadata["runtimeValidation"], "NOT RUN")
        return metadata

    def test_all_platforms_route_only_offline_zipformer_and_keep_existing_engines(self):
        for platform in ("android", "ios", "harmony"):
            with self.subTest(platform=platform):
                self.setUp()
                self.assert_profile(platform)

    def test_offline_signature_drift_blocks_every_write(self):
        target = self.csrc / "offline-recognizer-impl.cc"
        target.write_text(OFFLINE_FACTORY.replace("Manager *mgr,", "Manager *resources,"), encoding="utf-8")
        originals = {filename: filename.read_bytes() for filename in self.source.rglob("*") if filename.is_file()}
        with self.assertRaisesRegex(ValueError, "offline.*signature"):
            self.prepare()
        self.assertEqual(originals, {filename: filename.read_bytes() for filename in originals})

    def test_manifest_capability_does_not_claim_model_or_device_validation(self):
        for platform in ("android", "ios", "harmony"):
            with self.subTest(platform=platform):
                self.setUp()
                metadata = self.assert_profile(platform)
                package = self.source / "fixture-package"
                package.mkdir()
                with contextlib.redirect_stdout(io.StringIO()):
                    artifacts.package_output(package, platform, metadata, 1)
                manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["enabledModels"]["offline"], [
                    "paraformer", "sense_voice", "zipformer", "zipformer2",
                ])
                for status in ("runtimeValidation", "modelInference", "physicalDevice", "utsCompilation"):
                    self.assertEqual(manifest[status], "NOT RUN")

    @unittest.skipUnless(os.environ.get("SHERPA_ONNX_UPSTREAM_SOURCE"), "optional pinned upstream checkout")
    def test_pinned_upstream_source_on_all_platforms(self):
        checkout = Path(os.environ["SHERPA_ONNX_UPSTREAM_SOURCE"])
        for platform in ("android", "ios", "harmony"):
            with self.subTest(platform=platform):
                self.setUp()
                for filename in self.source.rglob("*"):
                    if filename.is_file():
                        relative = filename.relative_to(self.source).as_posix()
                        content = subprocess.check_output([
                            "git", "-C", str(checkout), "show", f"{profile_lite.COMMIT}:{relative}",
                        ])
                        filename.write_bytes(content)
                # Real upstream validation is preserved instead of the fixture sentinel.
                metadata = self.prepare(platform)
                factory = (self.csrc / "offline-recognizer-impl.cc").read_text(encoding="utf-8")
                self.assertEqual(factory.count('if (model_type == "zipformer" || model_type == "zipformer2")'), 2)
                validation = (self.csrc / "offline-model-config.cc").read_text(encoding="utf-8")
                self.assertIn("return transducer.Validate();", validation)
                self.assertIn("if (!FileExists(tokens))", validation)
                self.assertEqual(metadata["enabledModels"]["offline"], [
                    "paraformer", "sense_voice", "zipformer", "zipformer2",
                ])


if __name__ == "__main__":
    unittest.main()

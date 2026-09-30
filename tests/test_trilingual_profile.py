"""Exercise generated source patches, not model inference or native compilation."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import profile_lite


class TrilingualProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name)
        self.csrc = self.source / "sherpa-onnx/csrc"
        self.csrc.mkdir(parents=True)
        for filename, class_name in (
            ("offline-recognizer-impl.cc", "OfflineRecognizerImpl"),
            ("online-recognizer-impl.cc", "OnlineRecognizerImpl"),
            ("offline-tts-impl.cc", "OfflineTtsImpl"),
            ("vad-model.cc", "VadModel"),
        ):
            (self.csrc / filename).write_text(
                f"auto {class_name}::Create(const Config &config) {{ return unrelated(); }}\n"
                f"auto {class_name}::Create(Manager *mgr, const Config &config) {{ return unrelated(); }}\n",
                encoding="utf-8")
        for filename, class_name in (
            ("offline-model-config.cc", "OfflineModelConfig"),
            ("online-model-config.cc", "OnlineModelConfig"),
            ("offline-tts-model-config.cc", "OfflineTtsModelConfig"),
            ("vad-model-config.cc", "VadModelConfig"),
        ):
            (self.csrc / filename).write_text(
                f"bool {class_name}::Validate() const {{\n  return upstream_validation();\n}}\n",
                encoding="utf-8")
        constructors = [f"std::make_unique<Online{family}TransducerModel>({arguments});\n"
                        for family in ("Conformer", "Ebranchformer", "Lstm")
                        for arguments in ("config", "mgr, config")]
        (self.csrc / "online-transducer-model.cc").write_text("".join(constructors * 2), encoding="utf-8")
        native_sources = {
            "sherpa-onnx/jni": profile_lite.JNI_SOURCES,
            "harmony-os/SherpaOnnxHar/sherpa_onnx/src/main/cpp": profile_lite.HARMONY_SOURCES,
        }
        for relative, filenames in native_sources.items():
            directory = self.source / relative
            directory.mkdir(parents=True)
            (directory / "CMakeLists.txt").write_text(
                "set(sources\n" + "".join(f"  {name}\n" for name in sorted(filenames))
                + "  keyword-spotter.cc\n)\n", encoding="utf-8")
        harmony = self.source / "harmony-os/SherpaOnnxHar/sherpa_onnx/src/main/cpp"
        (harmony / "sherpa-onnx-node-addon-api.cc").write_text(
            "  InitStreamingAsr(env, exports);\n  InitKeywordSpotter(env, exports);\n", encoding="utf-8")
        exports = self.source / "sherpa-onnx/c-api/sherpa-onnx-symbols-c.exp"
        exports.parent.mkdir(parents=True)
        exports.write_text("_SherpaOnnxCreateOfflineRecognizer\n_SherpaOnnxCreateOnlineRecognizer\n"
                           "_SherpaOnnxCreateKeywordSpotter\n", encoding="utf-8")

    def prepare(self):
        with patch.object(profile_lite.subprocess, "check_output", side_effect=[profile_lite.COMMIT, ""]):
            return profile_lite.prepare(self.source, "android")

    def test_both_resource_and_path_factories_retain_new_and_existing_models(self):
        self.prepare()
        expectations = {
            "offline-recognizer-impl.cc": ("OfflineRecognizerParaformerImpl", "OfflineRecognizerSenseVoiceImpl"),
            "online-recognizer-impl.cc": ("OnlineRecognizerTransducerImpl", "OnlineRecognizerParaformerImpl"),
        }
        for filename, implementations in expectations.items():
            output = (self.csrc / filename).read_text(encoding="utf-8")
            for implementation in implementations:
                with self.subTest(filename=filename, implementation=implementation):
                    self.assertIn(f"std::make_unique<{implementation}>(config)", output)
                    self.assertIn(f"std::make_unique<{implementation}>(mgr, config)", output)
            self.assertNotIn("unrelated()", output)
            self.assertEqual(output.count("return nullptr;"), 2)

    def test_validation_allows_both_offline_models_and_both_streaming_families(self):
        self.prepare()
        offline = (self.csrc / "offline-model-config.cc").read_text(encoding="utf-8")
        online = (self.csrc / "online-model-config.cc").read_text(encoding="utf-8")
        self.assertIn("if (paraformer.model.empty() && sense_voice.model.empty())", offline)
        self.assertIn("paraformer.encoder.empty() && (transducer.encoder.empty()", online)
        self.assertIn('model_type != "zipformer" && model_type != "zipformer2"', online)
        for output in (offline, online):
            self.assertIn("return false;", output)
            self.assertIn("return upstream_validation();", output)

    def test_upstream_drift_blocks_every_write(self):
        target = self.csrc / "online-model-config.cc"
        target.write_text("upstream signature changed\n", encoding="utf-8")
        originals = {filename: filename.read_bytes() for filename in self.source.rglob("*") if filename.is_file()}
        with self.assertRaisesRegex(ValueError, "Missing validation anchor"):
            self.prepare()
        self.assertEqual(originals, {filename: filename.read_bytes() for filename in originals})


if __name__ == "__main__":
    unittest.main()

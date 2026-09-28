import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class ProfileTests(unittest.TestCase):
    def load_profile(self):
        self.assertIsNotNone(importlib.util.find_spec("profile_lite"),
                             "The fail-closed source profile must exist")
        import profile_lite
        return profile_lite

    def test_replaces_only_function_definitions_and_ignores_string_braces(self):
        profile = self.load_profile()
        source = '''Widget::Create(const Config &config) {
  log("}"); /* { */ if (config.ok) { return original(); }
}
template Widget::Create(const Config &config);
int untouched() { return 17; }
'''
        patched = profile.replace_factories(source, "Widget", lambda signature: "return selected();", 1)
        self.assertIn("return selected();", patched)
        self.assertIn("template Widget::Create(const Config &config);", patched)
        self.assertIn("int untouched() { return 17; }", patched)
        self.assertNotIn("original()", patched)

    def test_rejects_unexpected_upstream_function_count(self):
        profile = self.load_profile()
        with self.assertRaises(ValueError):
            profile.replace_factories("Widget::Create() {}", "Widget", lambda signature: "", 2)

    def test_c_api_preserves_consumed_features_not_unrelated_features(self):
        profile = self.load_profile()
        for symbol in ("SherpaOnnxCreateOfflineRecognizer", "SherpaOnnxOnlineStreamInputFinished",
                       "SherpaOnnxOfflineTtsGenerateWithProgressCallbackWithArg",
                       "SherpaOnnxVoiceActivityDetectorFront", "SherpaOnnxDestroySpeechSegment",
                       "SherpaOnnxCreateLinearResampler", "SherpaOnnxReadWave"):
            self.assertTrue(profile.keep_c_symbol(symbol), symbol)
        for symbol in ("SherpaOnnxCreateKeywordSpotter", "SherpaOnnxCreateAudioTagging",
                       "SherpaOnnxAudioTaggingCreateOfflineStream",
                       "SherpaOnnxSpokenLanguageIdentificationCreateOfflineStream",
                       "SherpaOnnxCreateOfflineSpeakerDiarization", "SherpaOnnxCreateOnlineSpeechDenoiser"):
            self.assertFalse(profile.keep_c_symbol(symbol), symbol)

    def test_common_c_api_excludes_harmony_resource_manager_constructors(self):
        profile = self.load_profile()
        for symbol in ("SherpaOnnxCreateOnlineRecognizerOHOS",
                       "SherpaOnnxCreateOfflineRecognizerOHOS",
                       "SherpaOnnxCreateVoiceActivityDetectorOHOS",
                       "SherpaOnnxCreateOfflineTtsOHOS"):
            with self.subTest(symbol=symbol):
                self.assertFalse(profile.keep_c_symbol(symbol), symbol)

    def test_harmony_exports_add_resource_manager_constructors_without_losing_common_api(self):
        profile = self.load_profile()
        common_symbols = ["SherpaOnnxCreateOfflineRecognizer", "SherpaOnnxOfflineTtsNumSpeakers"]
        expected = {
            "SherpaOnnxCreateOfflineRecognizer", "SherpaOnnxOfflineTtsNumSpeakers",
            "SherpaOnnxCreateOnlineRecognizerOHOS", "SherpaOnnxCreateOfflineRecognizerOHOS",
            "SherpaOnnxCreateVoiceActivityDetectorOHOS", "SherpaOnnxCreateOfflineTtsOHOS",
        }
        self.assertEqual(profile.add_harmony_exports(common_symbols), sorted(expected))
        self.assertEqual(common_symbols, ["SherpaOnnxCreateOfflineRecognizer", "SherpaOnnxOfflineTtsNumSpeakers"])

    def test_platform_selection_keeps_harmony_symbols_only_on_harmony(self):
        profile = self.load_profile()
        common_symbols = {
            "SherpaOnnxCreateOfflineRecognizer", "SherpaOnnxCreateOnlineRecognizer",
            "SherpaOnnxCreateVoiceActivityDetector", "SherpaOnnxCreateOfflineTts",
            "SherpaOnnxOfflineTtsNumSpeakers",
        }
        harmony_symbols = {
            "SherpaOnnxCreateOnlineRecognizerOHOS", "SherpaOnnxCreateOfflineRecognizerOHOS",
            "SherpaOnnxCreateVoiceActivityDetectorOHOS", "SherpaOnnxCreateOfflineTtsOHOS",
        }
        upstream_symbols = sorted(common_symbols | {"SherpaOnnxCreateKeywordSpotter"})
        for target_platform in ("android", "ios", "harmony"):
            for input_symbols in (upstream_symbols, upstream_symbols + sorted(harmony_symbols)):
                with self.subTest(platform=target_platform, includes_ohos=len(input_symbols) > len(upstream_symbols)):
                    expected = common_symbols | harmony_symbols if target_platform == "harmony" else common_symbols
                    self.assertEqual(profile.select_c_api_symbols(input_symbols, target_platform), sorted(expected))

    def test_platform_selection_rejects_unknown_platform(self):
        profile = self.load_profile()
        with self.assertRaisesRegex(ValueError, "Unsupported target platform"):
            profile.select_c_api_symbols(["SherpaOnnxCreateOfflineRecognizer"], "unknown")

    def test_platform_selection_rejects_empty_or_unreduced_exports(self):
        profile = self.load_profile()
        for symbols in ([], ["SherpaOnnxCreateKeywordSpotter"], ["SherpaOnnxCreateOfflineRecognizer"]):
            with self.subTest(symbols=symbols):
                with self.assertRaisesRegex(ValueError, "did not reduce"):
                    profile.select_c_api_symbols(symbols, "ios")


if __name__ == "__main__":
    unittest.main()

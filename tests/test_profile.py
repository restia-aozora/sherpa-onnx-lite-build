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


if __name__ == "__main__":
    unittest.main()

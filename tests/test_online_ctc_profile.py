"""Test generated CTC source profiles, not native compilation or inference."""

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


# Pinned upstream signatures and constructors, including both resource managers.
CTC_FACTORY = '''#include "sherpa-onnx/csrc/online-ctc-model.h"
#include "sherpa-onnx/csrc/online-wenet-ctc-model.h"
#include "sherpa-onnx/csrc/online-zipformer2-ctc-model.h"
#include "sherpa-onnx/csrc/online-nemo-ctc-model.h"
#include "sherpa-onnx/csrc/online-t-one-ctc-model.h"
std::unique_ptr<OnlineCtcModel> OnlineCtcModel::Create(
    const OnlineModelConfig &config) {
  if (!config.wenet_ctc.model.empty()) {
    return std::make_unique<OnlineWenetCtcModel>(config);
  } else if (!config.zipformer2_ctc.model.empty()) {
    return std::make_unique<OnlineZipformer2CtcModel>(config);
  } else if (!config.nemo_ctc.model.empty()) {
    return std::make_unique<OnlineNeMoCtcModel>(config);
  } else if (!config.t_one_ctc.model.empty()) {
    return std::make_unique<OnlineToneCtcModel>(config);
  }
  return nullptr;
}
template <typename Manager>
std::unique_ptr<OnlineCtcModel> OnlineCtcModel::Create(
    Manager *mgr, const OnlineModelConfig &config) {
  if (!config.wenet_ctc.model.empty()) {
    return std::make_unique<OnlineWenetCtcModel>(mgr, config);
  } else if (!config.zipformer2_ctc.model.empty()) {
    return std::make_unique<OnlineZipformer2CtcModel>(mgr, config);
  } else if (!config.nemo_ctc.model.empty()) {
    return std::make_unique<OnlineNeMoCtcModel>(mgr, config);
  } else if (!config.t_one_ctc.model.empty()) {
    return std::make_unique<OnlineToneCtcModel>(mgr, config);
  }
  return nullptr;
}
#if __ANDROID_API__ >= 9
template std::unique_ptr<OnlineCtcModel> OnlineCtcModel::Create(
    AAssetManager *mgr, const OnlineModelConfig &config);
#endif
#if __OHOS__
template std::unique_ptr<OnlineCtcModel> OnlineCtcModel::Create(
    NativeResourceManager *mgr, const OnlineModelConfig &config);
#endif
'''

REQUIRED_CTC_SOURCES = (
    "online-ctc-model.cc", "online-zipformer2-ctc-model.cc",
    "online-zipformer2-ctc-model-config.cc", "online-ctc-greedy-search-decoder.cc",
    "online-ctc-fst-decoder.cc", "online-ctc-fst-decoder-config.cc",
    "online-recognizer-impl.cc", "online-model-config.cc", "online-stream.cc",
    # CTC recognizer calls NormalizeFeatures even when runtime use is disabled.
    "offline-whisper-model.cc",
)


class OnlineCtcProfileTests(unittest.TestCase):
    def setUp(self):
        test_trilingual_profile.TrilingualProfileTests.setUp(self)
        (self.csrc / "online-ctc-model.cc").write_text(CTC_FACTORY, encoding="utf-8")
        (self.csrc / "CMakeLists.txt").write_text(
            "set(sources\n" + "".join(f"  {name}\n" for name in REQUIRED_CTC_SOURCES)
            + ")\n", encoding="utf-8")

    def prepare(self, platform="android"):
        with patch.object(profile_lite.subprocess, "check_output",
                          side_effect=[profile_lite.COMMIT, ""]):
            return profile_lite.prepare(self.source, platform)

    def assert_profile(self, platform):
        metadata = self.prepare(platform)
        recognizer = (self.csrc / "online-recognizer-impl.cc").read_text(encoding="utf-8")
        model = (self.csrc / "online-ctc-model.cc").read_text(encoding="utf-8")
        for arguments in ("config", "mgr, config"):
            self.assertIn(f"std::make_unique<OnlineRecognizerCtcImpl>({arguments})", recognizer)
            self.assertIn(f"std::make_unique<OnlineZipformer2CtcModel>({arguments})", model)
            for family in ("Transducer", "Paraformer"):
                self.assertIn(f"std::make_unique<OnlineRecognizer{family}Impl>({arguments})", recognizer)
        self.assertEqual(recognizer.count("if (!config.model_config.zipformer2_ctc.model.empty())"), 2)
        self.assertEqual(model.count("if (!config.zipformer2_ctc.model.empty())"), 2)
        self.assertEqual(model.count("return nullptr;"), 2)
        for family in ("OnlineWenetCtcModel", "OnlineNeMoCtcModel", "OnlineToneCtcModel"):
            self.assertNotIn(f"std::make_unique<{family}>", model)
        self.assertIn("AAssetManager *mgr, const OnlineModelConfig &config);", model)
        self.assertIn("NativeResourceManager *mgr, const OnlineModelConfig &config);", model)
        validation = (self.csrc / "online-model-config.cc").read_text(encoding="utf-8")
        self.assertIn("zipformer2_ctc.model.empty() && paraformer.encoder.empty()", validation)
        changed_files = [Path(filename).as_posix() for filename in metadata["changedFiles"]]
        self.assertIn("sherpa-onnx/csrc/online-ctc-model.cc", changed_files)
        self.assertIn("zipformer2_ctc", metadata["enabledModels"]["online"])
        self.assertEqual(metadata["runtimeValidation"], "NOT RUN")

    def test_all_platforms_route_ctc_and_preserve_existing_factories(self):
        for platform in ("android", "ios", "harmony"):
            with self.subTest(platform=platform):
                self.setUp()
                self.assert_profile(platform)

    def test_ctc_signature_drift_blocks_every_write(self):
        target = self.csrc / "online-ctc-model.cc"
        target.write_text(CTC_FACTORY.replace("Manager *mgr,", "Manager *resources,"), encoding="utf-8")
        originals = {filename: filename.read_bytes() for filename in self.source.rglob("*") if filename.is_file()}
        with self.assertRaisesRegex(ValueError, "CTC.*signature"):
            self.prepare()
        self.assertEqual(originals, {filename: filename.read_bytes() for filename in originals})

    def test_generated_ctc_factories_have_no_unknown_model_fallback(self):
        self.prepare()
        model = (self.csrc / "online-ctc-model.cc").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"if \((.*?)\) \{", model), [
            "!config.zipformer2_ctc.model.empty()",
            "!config.zipformer2_ctc.model.empty()",
        ])
        self.assertEqual(re.findall(r"return (.*?);", model), [
            "std::make_unique<OnlineZipformer2CtcModel>(config)", "nullptr",
            "std::make_unique<OnlineZipformer2CtcModel>(mgr, config)", "nullptr",
        ])

    def test_all_platform_manifests_keep_ctc_capability_separate_from_validation(self):
        for platform in ("android", "ios", "harmony"):
            with self.subTest(platform=platform):
                self.setUp()
                metadata = self.prepare(platform)
                package = self.source / "fixture-package"
                package.mkdir()
                # Exercise metadata serialization only; no native artifact is built.
                with contextlib.redirect_stdout(io.StringIO()):
                    artifacts.package_output(package, platform, metadata, 1)
                manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["enabledModels"]["online"], [
                    "paraformer", "zipformer", "zipformer2", "zipformer2_ctc",
                ])
                self.assertEqual(manifest["platform"], platform)
                for status in ("runtimeValidation", "modelInference", "physicalDevice", "utsCompilation"):
                    self.assertEqual(manifest[status], "NOT RUN")

    def test_missing_ctc_link_dependency_blocks_every_write(self):
        target = self.csrc / "CMakeLists.txt"
        target.write_text(target.read_text().replace("  offline-whisper-model.cc\n", ""), encoding="utf-8")
        originals = {filename: filename.read_bytes() for filename in self.source.rglob("*") if filename.is_file()}
        with self.assertRaisesRegex(ValueError, "Missing.*CTC.*sources"):
            self.prepare()
        self.assertEqual(originals, {filename: filename.read_bytes() for filename in originals})

    @unittest.skipUnless(os.environ.get("SHERPA_ONNX_UPSTREAM_SOURCE"), "optional pinned upstream checkout")
    def test_pinned_upstream_source_on_all_platforms(self):
        checkout = Path(os.environ["SHERPA_ONNX_UPSTREAM_SOURCE"])
        for platform in ("android", "ios", "harmony"):
            with self.subTest(platform=platform):
                self.setUp()
                for filename in self.source.rglob("*"):
                    if filename.is_file():
                        relative = filename.relative_to(self.source).as_posix()
                        content = subprocess.check_output(
                            ["git", "-C", str(checkout), "show", f"{profile_lite.COMMIT}:{relative}"])
                        filename.write_bytes(content)
                self.assert_profile(platform)


if __name__ == "__main__":
    unittest.main()

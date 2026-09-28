"""Fail-closed transformations for the pinned upstream revision only."""

import argparse
import json
import re
import subprocess
from pathlib import Path

COMMIT = "11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf"
JNI_SOURCES = {
    "common.cc", "jni.cc", "offline-recognizer.cc", "offline-stream.cc",
    "online-recognizer.cc", "online-stream.cc", "version.cc",
    "voice-activity-detector.cc", "wave-reader.cc", "wave-writer.cc", "offline-tts.cc",
}
KOTLIN_FILES = (
    "FeatureConfig.kt", "HomophoneReplacerConfig.kt", "QnnConfig.kt",
    "OfflineRecognizer.kt", "OfflineStream.kt", "OnlineRecognizer.kt",
    "OnlineStream.kt", "Tts.kt", "Vad.kt", "VersionInfo.kt", "WaveReader.kt",
)
HARMONY_SOURCES = {
    "non-streaming-asr.cc", "non-streaming-tts.cc", "streaming-asr.cc",
    "utils.cc", "vad.cc", "version.cc", "wave-reader.cc", "wave-writer.cc",
    "resampler.cc", "sherpa-onnx-node-addon-api.cc",
}
HARMONY_INITIALIZERS = {
    "InitStreamingAsr", "InitNonStreamingAsr", "InitNonStreamingTts", "InitVad",
    "InitWaveReader", "InitWaveWriter", "InitVersion", "InitResampler", "InitUtils",
}
NON_CODE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*.*?\*/', re.S)


def replace_factories(source, class_name, render_body, expected_count=2):
    # Mask comments/literals without moving offsets, so braces in diagnostics do not count.
    masked = NON_CODE.sub(lambda match: " " * len(match.group()), source)
    pattern = re.compile(re.escape(class_name) + r"::Create\([^;{}]*\)\s*\{")
    definitions = list(pattern.finditer(masked))
    if len(definitions) != expected_count:
        raise ValueError(f"Unexpected {class_name} factory count: {len(definitions)}")
    for definition in reversed(definitions):
        body_start = definition.end() - 1
        depth = 1
        body_end = body_start + 1
        while depth and body_end < len(masked):
            depth += (masked[body_end] == "{") - (masked[body_end] == "}")
            body_end += 1
        if depth:
            raise ValueError(f"Unbalanced factory: {class_name}")
        signature = source[definition.start():body_start]
        source = source[:body_start] + "{\n" + render_body(signature) + "\n}" + source[body_end:]
    return source


def keep_c_symbol(symbol):
    if symbol.endswith("OHOS"):
        return False
    excluded = ("AudioTagging", "SpokenLanguage", "Keyword", "Speaker", "Denoiser",
                "Punctuation", "Diacritization", "SourceSeparation", "WithZipvoice")
    # NumSpeakers belongs to VITS and must survive the general speaker exclusion.
    if symbol != "SherpaOnnxOfflineTtsNumSpeakers" and any(part in symbol for part in excluded):
        return False
    families = (
        "OfflineRecognizer", "OnlineRecognizer", "OfflineStream", "OnlineStream",
        "OfflineTts", "VoiceActivityDetector", "SpeechSegment", "CircularBuffer",
        "LinearResampler", "Wave",
    )
    helpers = {
        "SherpaOnnxAcceptWaveformOffline", "SherpaOnnxGetVersionStr",
        "SherpaOnnxGetGitSha1", "SherpaOnnxGetGitDate",
        "SherpaOnnxGetOnnxruntimeVersionStr", "SherpaOnnxFileExists",
    }
    return symbol in helpers or (symbol.startswith("SherpaOnnx") and any(part in symbol for part in families))


def add_harmony_exports(symbols):
    """Keep the resource-manager and display APIs used by the ArkTS bridge."""
    required = {
        "SherpaOnnxCreateOnlineRecognizerOHOS",
        "SherpaOnnxCreateOfflineRecognizerOHOS",
        "SherpaOnnxCreateVoiceActivityDetectorOHOS",
        "SherpaOnnxCreateOfflineTtsOHOS",
        # streaming-asr.cc still registers display wrappers in the lite bridge.
        "SherpaOnnxCreateDisplay",
        "SherpaOnnxDestroyDisplay",
        "SherpaOnnxPrint",
    }
    return sorted(set(symbols) | required)


def select_c_api_symbols(symbols, target_platform):
    if target_platform not in ("android", "ios", "harmony"):
        raise ValueError(f"Unsupported target platform: {target_platform}")
    common_symbols = sorted({symbol for symbol in symbols if keep_c_symbol(symbol)})
    if not common_symbols or len(common_symbols) >= len(symbols):
        raise ValueError("C API export filtering did not reduce the upstream API")
    if target_platform == "harmony":
        return add_harmony_exports(common_symbols)
    return common_symbols


def retain_cmake_sources(source, retained):
    found = set(re.findall(r"^\s+([\w-]+\.cc)\s*$", source, re.M))
    if not retained <= found:
        raise ValueError(f"Missing native sources: {retained - found}")
    return re.sub(r"^\s+([\w-]+\.cc)[ \t]*\n", lambda match:
                  match.group() if match.group(1) in retained else "", source, flags=re.M)


def prepare(source_root, target_platform):
    actual = subprocess.check_output(["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(source_root), "status", "--porcelain"], text=True).strip()
    if actual != COMMIT or dirty:
        raise ValueError("Source must be a clean checkout of the pinned commit; never patch a user's dirty tree")
    updates = {}
    factory_profiles = (
        ("offline-recognizer-impl.cc", "OfflineRecognizerImpl", "OfflineRecognizerParaformerImpl",
         "!config.model_config.paraformer.model.empty()"),
        ("online-recognizer-impl.cc", "OnlineRecognizerImpl", "OnlineRecognizerTransducerImpl",
         "!config.model_config.transducer.encoder.empty()"),
        ("offline-tts-impl.cc", "OfflineTtsImpl", "OfflineTtsVitsImpl", "!config.model.vits.model.empty()"),
        ("vad-model.cc", "VadModel", "SileroVadModel", "!config.silero_vad.model.empty()"),
    )
    for filename, class_name, implementation, condition in factory_profiles:
        target = source_root / "sherpa-onnx/csrc" / filename
        def body(signature, implementation=implementation, condition=condition):
            arguments = "mgr, config" if "Manager *mgr" in signature else "config"
            return (f"  if ({condition}) {{\n"
                    f"    return std::make_unique<{implementation}>({arguments});\n  }}\n"
                    '  SHERPA_ONNX_LOGE("Unsupported model in the four-feature lite build");\n'
                    "  return nullptr;")
        updates[target] = replace_factories(target.read_text(encoding="utf-8"), class_name, body)

    # Keep metadata-based Zipformer/Zipformer2 selection, but remove other constructor references.
    target = source_root / "sherpa-onnx/csrc/online-transducer-model.cc"
    content, count = re.subn(
        r"std::make_unique<Online(?:Conformer|Ebranchformer|Lstm)TransducerModel>\((?:mgr, )?config\)",
        "nullptr", target.read_text(encoding="utf-8"))
    if count != 12:
        raise ValueError(f"Unexpected transducer constructor count: {count}")
    updates[target] = content

    validations = (
        ("offline-model-config.cc", "OfflineModelConfig", "paraformer.model.empty()"),
        ("online-model-config.cc", "OnlineModelConfig",
         'transducer.encoder.empty() || (!model_type.empty() && model_type != "zipformer" && model_type != "zipformer2")'),
        ("offline-tts-model-config.cc", "OfflineTtsModelConfig", "vits.model.empty()"),
        ("vad-model-config.cc", "VadModelConfig", "silero_vad.model.empty()"),
    )
    for filename, class_name, rejected in validations:
        target = source_root / "sherpa-onnx/csrc" / filename
        content = target.read_text(encoding="utf-8")
        anchor = f"bool {class_name}::Validate() const {{"
        if content.count(anchor) != 1:
            raise ValueError(f"Missing validation anchor: {filename}")
        updates[target] = content.replace(anchor, anchor + f"\n  if ({rejected}) {{\n"
                                         '    SHERPA_ONNX_LOGE("Model not enabled in lite build");\n'
                                         "    return false;\n  }", 1)

    target = source_root / "sherpa-onnx/jni/CMakeLists.txt"
    updates[target] = retain_cmake_sources(target.read_text(encoding="utf-8"), JNI_SOURCES)
    # The now-empty speaker diarization block is harmless because the build disables it.
    harmony_root = source_root / "harmony-os/SherpaOnnxHar/sherpa_onnx/src/main/cpp"
    target = harmony_root / "CMakeLists.txt"
    updates[target] = retain_cmake_sources(target.read_text(encoding="utf-8"), HARMONY_SOURCES)
    target = harmony_root / "sherpa-onnx-node-addon-api.cc"
    updates[target] = re.sub(r"^  (Init\w+)\(env, exports\);\n", lambda match:
                             match.group() if match.group(1) in HARMONY_INITIALIZERS else "",
                             target.read_text(encoding="utf-8"), flags=re.M)

    exports = source_root / "sherpa-onnx/c-api/sherpa-onnx-symbols-c.exp"
    symbols = [line[1:] for line in exports.read_text().splitlines() if line.startswith("_")]
    selected = select_c_api_symbols(symbols, target_platform)
    # Apple exports must never reference OHOS-only implementations.
    updates[exports] = "".join(f"_{symbol}\n" for symbol in selected if not symbol.endswith("OHOS"))
    updates[exports.with_suffix(".lds")] = ("{\n  global:\n" +
        "".join(f"    {symbol};\n" for symbol in selected) + "  local: *;\n};\n")
    for target, content in updates.items():
        target.write_text(content, encoding="utf-8", newline="\n")
    return {"sourceCommit": actual, "changedFiles": [str(target.relative_to(source_root)) for target in updates],
            "retainedCApiSymbols": selected, "runtimeValidation": "NOT RUN"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--platform", required=True, choices=("android", "ios", "harmony"))
    arguments = parser.parse_args()
    print(json.dumps(prepare(arguments.source.resolve(), arguments.platform), indent=2))

# sherpa-onnx-lite-build

This repository builds a multilingual lite native profile of sherpa-onnx 1.13.8 for
the Aozora UTS plugin. Android supports selectable `arm64-v8a` and `armeabi-v7a`
ABIs, individually or together in one AAR. iOS and Harmony remain ARM64-only.
It retains CPU Paraformer and SenseVoice offline ASR, Paraformer and Zipformer or
Zipformer2 streaming ASR, Silero VAD and VITS TTS. Model factories and public
exports exclude unrelated features. Some upstream implementation files still
compile into intermediate static archives; final binary size must be measured.
NPU providers and simulator slices are disabled.

Android produces one compiled AAR. The other platforms keep their existing
framework or source-plus-native-library format; no Harmony HAR is generated:

```text
Android:  app-android/libs/sherpa-onnx-lite-android-1.13.8-<selected-abis>.aar
iOS:      app-ios/Frameworks/SherpaOnnxC.xcframework (iPhoneOS arm64 only)
Harmony:  app-harmony/libs/arm64-v8a/*.so
          app-harmony/wrapper/*.ets
```

## What is actually removed

| Feature | Native profile |
| --- | --- |
| Offline ASR | Paraformer and SenseVoice |
| Streaming ASR | Paraformer and Zipformer / Zipformer2 transducer; not Zipformer2 CTC |
| Voice activity detection | Silero VAD only |
| Text to speech | VITS only |
| Common helpers | Audio features, WAV I/O, resampling, buffering, version/configuration APIs |
| Other model engines | No factory entry for Whisper, other CTC engines, Matcha, Kokoro, etc. |
| Other public feature families | Keyword spotting, speaker identification/diarization, audio tagging, denoising, punctuation, etc. are excluded from selected exports/bridges |
| GPU/NPU providers | Disabled in the build configuration |

This is **feature-entry-point pruning, not a proven minimal binary**:

- Unrelated upstream C++ files can still compile into intermediate archives.
  Link-time dead stripping removes unreachable sections where possible; this
  does not prove every unused implementation has vanished from the final binary.
- ONNX Runtime uses locked prebuilt archives, not a model-specific operator
  build. `onnxruntimeOperatorPruning` remains `false` in every package manifest.
- Selected Kotlin API files still contain upstream configuration types and
  example helpers for other models. Those types do not restore removed engines.
  They are retained for compatibility with the pinned JNI configuration layout.
- The consuming UTS plugin can still advertise a broader generic model list.
  Its advertised list is not evidence that a model works with this lite runtime.
- Further operator pruning requires the exact ONNX models and separate
  inference regression tests for each selected ABI. No such pruning is claimed.

## Important limits

- The native inference engine still requires compiled C/C++ code. The Android
  AAR includes the selected JNI `.so` files and compiled Kotlin `classes.jar`;
  it is not a ZIP of Kotlin sources renamed to `.aar`.
- The build is intentionally fail-closed on the pinned upstream commit and
  SHA-256 locked ONNX Runtime archives.
- No model is included. The application must provide the SenseVoice, Paraformer,
  Zipformer, Silero VAD and VITS model files it actually uses.
- `runtimeValidation`, UTS compilation and physical-device validation are
  reported as `NOT RUN` until the generated artifact is tested in the consuming
  app. A small native archive does not by itself prove that an HBuilderX App
  upload is below its quota.

## Local build prerequisites

### Existing applications need a rebuilt runtime

The expanded profile adds offline SenseVoice and streaming Paraformer in both
filesystem and resource-manager factories, and permits them through the model
validation guards. Existing Paraformer offline, Zipformer/Zipformer2 streaming,
Silero VAD and VITS paths remain enabled. The upstream commit, CPU-only settings,
dependency locks and 55-MiB integration archive limit are unchanged.

Changing these scripts does not update an already shipped AAR or custom base.
Rebuild the selected Android ABIs, integrate that AAR yourself, and rebuild the
Android custom base before testing the new models. No consuming application's
native binaries are replaced by this source change. Android UTS/Kotlin model
adaptation does not imply equivalent iOS or Harmony plugin adaptation.

The intended application pair is offline
`sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17` with automatic language
and inverse text normalization, and streaming
`sherpa-onnx-streaming-paraformer-trilingual-zh-cantonese-en` with INT8 encoder
and decoder. Streaming Paraformer does not use a transducer joiner. Actual
recognition quality, peak memory and expanded native binary size require builds
and physical-device validation; Python tests alone do not establish these.

### Host and build commands

Use Linux, WSL2 or GitHub Actions for Android and Harmony. Use macOS with
Xcode, CMake and Ninja for iOS. Windows PowerShell is supported for editing
the repository, not for the native Linux/iOS commands themselves.

```bash
python3 scripts/build.py android --android-abis arm64-v8a
python3 scripts/build.py android --android-abis armeabi-v7a
python3 scripts/build.py android --android-abis armeabi-v7a arm64-v8a
python3 scripts/build.py harmony
python3 scripts/build.py ios
```

Android requires `ANDROID_NDK_HOME`, `ANDROID_HOME` (or `ANDROID_SDK_ROOT`),
JDK 17 and Android SDK platform/build-tools 34. The default when
`--android-abis` is omitted is `arm64-v8a`. Empty, duplicate and unsupported
ABI selections are rejected. The flag is invalid for iOS and Harmony.

Each ABI uses its own CMake directory and SHA-256 locked ONNX Runtime static
archive. Native libraries are stripped and checked for the selected ELF
architecture and unresolved dependencies. ARM64 additionally requires 16-KB
load-segment alignment. After all selected native builds pass, an isolated
Android library project compiles only the selected Kotlin files and assembles
one AAR. Tool versions match the pinned upstream AAR project (AGP 8.4.0,
Kotlin 1.7.20, Gradle 8.6); the Gradle distribution also has a locked SHA-256.

For both ABIs the AAR contains:

```text
classes.jar
AndroidManifest.xml
proguard.txt
jni/arm64-v8a/libsherpa-onnx-jni.so
jni/armeabi-v7a/libsherpa-onnx-jni.so
assets/sherpa-onnx-lite/licenses/...
```

The packager checks the exact ABI set, native file hashes, required compiled
classes, excluded feature classes, minimum SDK, JNI consumer keep rules and
license presence. Any ABI build or check failure blocks the combined package.
The Android minimum API level remains 23. Applications must also provide a
compatible Kotlin standard library (at least the compiler version above);
direct local AAR consumption does not resolve transitive dependencies for you.

Outputs are copied to `dist/`: a standalone `.aar`, an integration ZIP with the
same AAR plus manifest/notices, and `android-size-report.json`. The archive name
records the selected ABIs in stable order. Both ABIs are larger than either
alone; the existing 55-MiB integration ZIP budget remains enforced and is not
a guarantee about the consuming application's upload or installed size.

### Switching the UTS plugin from loose files to the AAR

Place only the chosen AAR in `uni_modules/aozora-sherpa-onnx/utssdk/app-android/libs/`.
Before using it, back up and remove the previous full AAR, previous loose
`libs/<abi>/libsherpa-onnx-jni.so`, and the 11 previously copied upstream Kotlin
files listed in `scripts/profile_lite.py:KOTLIN_FILES`. Do not remove the plugin's
own `AsrBackend.kt`, `TtsBackend.kt`, `VadBackend.kt`, `SherpaAndroidBridge.kt`,
audio helpers or UTS code. Keeping both the upstream Kotlin sources and their
compiled AAR classes causes duplicate classes; duplicate native libraries must
not be installed either. Rebuild the custom base/App and test every selected
ABI with the real models. This repository does not modify the consuming plugin.

Harmony requires either `OHOS_SDK_NATIVE_DIR` pointing to an installed Huawei
Linux Native SDK, or both `HARMONY_SDK_URL` and `HARMONY_SDK_SHA256`. Do not
use an SDK download whose digest has not been independently verified.

## GitHub Actions

Public repositories can use standard GitHub-hosted runners without Actions
minute charges. The iOS job uses a macOS standard runner; it does not need an
Apple signing identity because it produces an unsigned/ad-hoc native library.

Before enabling the Harmony job, configure repository Actions settings:

- Secret `HARMONY_SDK_URL`: an authorized Linux Harmony Native SDK archive URL.
  Use a secret for signed URLs; do not commit their query parameters. An existing
  repository variable with the same name remains supported as a fallback.
- Variable `HARMONY_SDK_SHA256`: the lowercase SHA-256 of that exact archive.

The ZIP must contain an unpacked Native SDK with `build/cmake/ohos.toolchain.cmake`,
`llvm/bin/clang` and `llvm/bin/llvm-readelf`. An application `.har` is not a compiler
toolchain. A Windows or macOS host SDK cannot run on the Linux Harmony runner.
The official `commandline-tools-linux-x64-26.0.0.851.zip` contains this SDK under
`command-line-tools/sdk/default/openharmony/native`. Its verified SHA-256 is
`ab604bd92721d5cbcafd154e6461d46b9f1b105e7b89eab04a9d046681198082`.
The build extracts only the Native SDK subtree, preserving executable modes and
internal symlinks, and rejects archives with missing or ambiguous toolchains.
This archive verification does not establish successful native compilation or
device compatibility.

After pushing workflow fixes, start a new **Build lite native libraries** run
from the latest `main`. The default `all` option starts Android, iOS and Harmony
as independent parallel jobs, each uploading its own artifact. A failure in one
job does not cancel the others; runner availability may delay individual jobs.
Select `android`, `ios` or `harmony` to build only one platform. Native builds
remain manual-only: pushing commits does not start them. Re-running an old
run uses its original commit. **Validate build scripts** only checks Python;
it does not build native libraries. Android setup explicitly requests
`platform-tools`, never the removed `tools` package. iOS export lists exclude
OHOS-only constructors; those are retained only for Harmony's C API checks.

The manual-run form has two independent Android checkboxes:

- `android_arm64_v8a` (default: checked).
- `android_armeabi_v7a` (default: unchecked).

Check either one or both. When Android is included in the selected platform,
unchecking both fails before SDK installation. Checking both builds both ABIs
and uploads one combined AAR, not two competing AARs. iOS and Harmony ignore
these checkboxes. Platform `all` selects all three platforms, not all Android
ABIs; the Android checkboxes still control the AAR contents. Publish the updated
workflow before expecting these inputs to appear in GitHub Actions; an old run
will not gain the new inputs.

Artifacts are retained for seven days and are not committed to Git. Do not
upload business source, model files, signing certificates, private keys or
tokens to this repository.

## Verification

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m py_compile scripts/*.py
```

The native build checks linkage, exported APIs, target architecture, license
collection and archive size. It cannot replace retained-model inference
tests in the actual Android, iOS and Harmony applications.

The Python tests also cover ABI selection, ELF32/ELF64 validation, AAR structure,
changed native bytes, JNI keep rules, source selection and per-ABI orchestration.
Workflow tests execute all four checkbox combinations in Bash and check the
exact build arguments, without invoking the native compiler or downloading SDKs.
They use synthetic archives/mocked build commands; passing them does not mean
NDK, Gradle, UTS compilation or physical-device inference has been run.

The upstream Sherpa-ONNX license and notices must remain with every released
artifact. See `dependencies.json` for the source and runtime lock.

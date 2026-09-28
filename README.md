# sherpa-onnx-lite-build

This repository builds an ARM64-only native profile of sherpa-onnx 1.13.8 for
the Aozora UTS plugin. It retains CPU Paraformer offline ASR, Zipformer or
Zipformer2 streaming ASR, Silero VAD and VITS TTS. It deliberately does not
build keyword spotting, speaker identification/diarization, punctuation,
audio tagging, speech enhancement, NPU providers or simulator slices.

The output is source plus native libraries, not an Android AAR or Harmony HAR:

```text
Android:  app-android/libs/arm64-v8a/libsherpa-onnx-jni.so
          app-android/kotlin/*.kt
iOS:      app-ios/Frameworks/SherpaOnnxC.xcframework (iPhoneOS arm64 only)
Harmony:  app-harmony/libs/arm64-v8a/*.so
          app-harmony/wrapper/*.ets
```

## Important limits

- The native inference engine still requires compiled C/C++ code. Replacing an
  AAR with source files does not remove the need for native `.so` or framework
  binaries.
- The build is intentionally fail-closed on the pinned upstream commit and
  SHA-256 locked ONNX Runtime archives.
- No model is included. The application must provide the Paraformer,
  Zipformer, Silero VAD and VITS model files it actually uses.
- `runtimeValidation`, UTS compilation and physical-device validation are
  reported as `NOT RUN` until the generated artifact is tested in the consuming
  app. A small native archive does not by itself prove that an HBuilderX App
  upload is below its quota.

## Local build prerequisites

Use Linux, WSL2 or GitHub Actions for Android and Harmony. Use macOS with
Xcode, CMake and Ninja for iOS. Windows PowerShell is supported for editing
the repository, not for the native Linux/iOS commands themselves.

```bash
python3 scripts/build.py android
python3 scripts/build.py harmony
python3 scripts/build.py ios
```

Android requires `ANDROID_NDK_HOME`; the script uses only `arm64-v8a`, links
the locked static ONNX Runtime archive into JNI, strips symbols, checks ELF
dependencies and checks 16-KB load-segment alignment.

Harmony requires either `OHOS_SDK_NATIVE_DIR` pointing to an installed Huawei
Linux Native SDK, or both `HARMONY_SDK_URL` and `HARMONY_SDK_SHA256`. Do not
use an SDK download whose digest has not been independently verified.

## GitHub Actions

Public repositories can use standard GitHub-hosted runners without Actions
minute charges. The iOS job uses a macOS standard runner; it does not need an
Apple signing identity because it produces an unsigned/ad-hoc native library.

Before enabling the Harmony job, create repository variables:

- `HARMONY_SDK_URL`: an authorized Linux Harmony Native SDK archive URL.
- `HARMONY_SDK_SHA256`: the lowercase SHA-256 of that exact archive.

Artifacts are retained for seven days and are not committed to Git. Do not
upload business source, model files, signing certificates, private keys or
tokens to this repository.

## Verification

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m py_compile scripts/*.py
```

The native build checks linkage, exported APIs, target architecture, license
collection and archive size. It cannot replace four-feature model inference
tests in the actual Android, iOS and Harmony applications.

The upstream Sherpa-ONNX license and notices must remain with every released
artifact. See `dependencies.json` for the source and runtime lock.

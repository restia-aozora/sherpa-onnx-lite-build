# Native lite build implementation plan

## Approved scope

Build native libraries for Android, iOS and HarmonyOS, retaining offline
Paraformer ASR, streaming Zipformer/Zipformer2 transducer ASR, Silero VAD and
VITS TTS. Use the same sherpa-onnx commit as the consuming UTS plugin.
Do not copy business code, models, credentials or signing identities.

## Architecture

- ARM64 devices only. No Android AAR, Harmony HAR or simulator slices.
- Android: statically link the upstream ORT distribution into JNI; ship
  matching Kotlin source files, not a classes.jar.
- iOS: statically link ORT into SherpaOnnxC.framework, wrapped in a device-only
  XCFramework for the existing Swift module import. No distribution identity
  is required; the consuming app must embed/re-sign it.
- Harmony: ship C API, ORT, N-API and required C++ runtime shared libraries,
  with the matching ArkTS wrapper source. This is not a drop-in HAR replacement.
- Reduce factory reachability and native entry points; let static archive
  selection and section garbage collection remove unreachable implementations.
  Keep configuration layouts intact for JNI/C ABI compatibility.
- Do not claim a fully minimal ORT build or guaranteed sub-60-MB upload.

## Tasks and checks

1. Add a source lock and fail-closed source transformations. Test brace-aware
   C++ function replacement, retained symbols, and patch application against
   the exact upstream source. Reject source drift and repeated application.
2. Add dependency acquisition with SHA-256 verification, platform builds,
   native symbol/dependency checks, and per-file size/hash manifests.
3. Add manually dispatched GitHub Actions jobs: Android on Linux, iOS on
   macOS, Harmony on Linux with an explicitly supplied SDK URL and digest.
   Use read-only repository permissions and short artifact retention.
4. Validate Python, shell and workflow syntax locally. Native compilation and
   model inference are separate acceptance gates, not implied by script tests.
5. Request permission before committing/pushing and dispatching the workflows.

## Runtime acceptance before integrating

- Paraformer: fixed WAV fixture produces expected nonempty text.
- Zipformer: streaming PCM and final flush produce results; repeated sessions
  and cancellation release resources.
- Silero: silence and speech fixtures exercise trim/compact modes.
- VITS: fixed text produces nonempty audio and streaming playback completes.
- Android: verify ARM64 and 16-KB ELF segment alignment, compile Kotlin/JNI.
- iOS: verify device platform, ARM64, install name, no external ORT dependency,
  Swift module import and cloud-build embedding/signing.
- Harmony: verify ARM64, N-API registration, dependency closure, wrapper import
  and actual HBuilderX/Hvigor integration.

No changes are made to the application until these gates are reviewed.

"""Build the pinned four-feature native profile without AAR/HAR packaging."""

import argparse
import json
import os
import platform
import plistlib
import re
import shutil
import sys
import tempfile
from pathlib import Path

from artifacts import collect_licenses, elf_needed, package_output, verify_elf_package
from build_support import (LOCK, ROOT, copy_file, download_verified, extract_zip,
                           prepare_harmony_sdk, require_one, run, sha256_file)
from profile_lite import COMMIT, KOTLIN_FILES, prepare


def configure_and_build(source, build, target, options, environment, jobs):
    common = {
        "CMAKE_BUILD_TYPE": "MinSizeRel", "CMAKE_POSITION_INDEPENDENT_CODE": "ON",
        "CMAKE_C_FLAGS_MINSIZEREL": "-Os -DNDEBUG -ffunction-sections -fdata-sections",
        "CMAKE_CXX_FLAGS_MINSIZEREL": "-Os -DNDEBUG -ffunction-sections -fdata-sections",
        "SHERPA_ONNX_ENABLE_TTS": "ON", "SHERPA_ONNX_ENABLE_SPEAKER_DIARIZATION": "OFF",
        "SHERPA_ONNX_ENABLE_BINARY": "OFF", "SHERPA_ONNX_ENABLE_PYTHON": "OFF",
        "SHERPA_ONNX_ENABLE_TESTS": "OFF", "SHERPA_ONNX_ENABLE_CHECK": "OFF",
        "SHERPA_ONNX_ENABLE_PORTAUDIO": "OFF", "SHERPA_ONNX_ENABLE_WEBSOCKET": "OFF",
        "SHERPA_ONNX_BUILD_C_API_EXAMPLES": "OFF", "SHERPA_ONNX_ENABLE_GPU": "OFF",
        "SHERPA_ONNX_ENABLE_RKNN": "OFF", "SHERPA_ONNX_ENABLE_QNN": "OFF",
        "SHERPA_ONNX_ENABLE_AXERA": "OFF", "SHERPA_ONNX_ENABLE_AXCL": "OFF",
        "SHERPA_ONNX_ENABLE_ASCEND_NPU": "OFF", "SHERPA_ONNX_ENABLE_DIRECTML": "OFF",
        "BUILD_PIPER_PHONMIZE_EXE": "OFF", "BUILD_PIPER_PHONMIZE_TESTS": "OFF",
        "BUILD_ESPEAK_NG_EXE": "OFF", "BUILD_ESPEAK_NG_TESTS": "OFF",
    }
    common.update(options)
    run("cmake", "-S", source, "-B", build, "-G", "Ninja",
        *(f"-D{name}={value}" for name, value in common.items()), environment=environment)
    run("cmake", "--build", build, "--target", target, "--parallel", jobs, environment=environment)


def build_android(source, work, output, archive, environment, jobs, metadata):
    ndk = Path(environment.get("ANDROID_NDK_HOME", environment.get("ANDROID_NDK", ""))).resolve()
    toolchain = ndk / "build/cmake/android.toolchain.cmake"
    if not toolchain.is_file():
        raise ValueError("Set ANDROID_NDK_HOME to an installed Android NDK (CI uses the locked version)")
    hosts = list((ndk / "toolchains/llvm/prebuilt").glob("*"))
    if len(hosts) != 1:
        raise ValueError("Expected exactly one NDK host toolchain")
    tools = hosts[0] / "bin"
    copy_file(archive, source / Path(LOCK["android"]["url"]).name)
    build = work / "build"
    configure_and_build(source, build, "sherpa-onnx-jni", {
        "CMAKE_TOOLCHAIN_FILE": toolchain, "ANDROID_ABI": "arm64-v8a",
        "ANDROID_PLATFORM": f"android-{LOCK['android']['api']}", "ANDROID_STL": "c++_static",
        "ANDROID_SUPPORT_FLEXIBLE_PAGE_SIZES": "ON", "BUILD_SHARED_LIBS": "OFF",
        "SHERPA_ONNX_ENABLE_JNI": "ON", "SHERPA_ONNX_ENABLE_C_API": "OFF",
        "SHERPA_ONNX_USE_PRE_INSTALLED_ONNXRUNTIME_IF_AVAILABLE": "OFF",
        "CMAKE_SHARED_LINKER_FLAGS": "-Wl,--gc-sections,-z,max-page-size=16384,--no-undefined",
    }, environment, jobs)
    libraries = output / "app-android/libs/arm64-v8a"
    library = libraries / "libsherpa-onnx-jni.so"
    copy_file(require_one(build / "lib", library.name), library)
    run(tools / "llvm-strip", "--strip-unneeded", library)
    for filename in KOTLIN_FILES:
        copy_file(source / "sherpa-onnx/kotlin-api" / filename, output / "app-android/kotlin" / filename)
    metadata["dependencies"] = verify_elf_package(libraries, "android", tools / "llvm-readelf",
                                                 tools / "llvm-nm", metadata["retainedCApiSymbols"])
    metadata["toolchain"] = (ndk / "source.properties").read_text()
    return build


def build_ios(source, work, output, dependencies, environment, jobs, metadata):
    framework_binary = require_one(dependencies, "ios-arm64/onnxruntime.framework/onnxruntime")
    framework_parent = framework_binary.parent.parent
    environment["SHERPA_ONNXRUNTIME_LIB_DIR"] = str(framework_parent)
    environment["SHERPA_ONNXRUNTIME_INCLUDE_DIR"] = str(framework_binary.parent / "Headers")
    minimum = LOCK["ios"]["deploymentTarget"]
    build = work / "build"
    configure_and_build(source, build, "sherpa-onnx-c-api", {
        "CMAKE_TOOLCHAIN_FILE": source / "toolchains/ios.toolchain.cmake",
        "PLATFORM": "OS64", "DEPLOYMENT_TARGET": minimum,
        "ENABLE_BITCODE": "OFF", "ENABLE_ARC": "ON", "ENABLE_VISIBILITY": "ON",
        "BUILD_SHARED_LIBS": "ON", "SHERPA_ONNX_ENABLE_JNI": "OFF",
        "SHERPA_ONNX_ENABLE_C_API": "ON", "CMAKE_SHARED_LINKER_FLAGS": "-Wl,-dead_strip",
    }, environment, jobs)
    framework = work / "framework/SherpaOnnxC.framework"
    binary = framework / "SherpaOnnxC"
    copy_file(require_one(build / "lib", "libsherpa-onnx-c-api.dylib"), binary)
    copy_file(source / "sherpa-onnx/c-api/c-api.h", framework / "Headers/sherpa-onnx/c-api/c-api.h")
    (framework / "Modules").mkdir()
    (framework / "Modules/module.modulemap").write_text(
        'framework module SherpaOnnxC {\n  header "sherpa-onnx/c-api/c-api.h"\n  export *\n}\n')
    info = {"CFBundleIdentifier": "com.k2-fsa.sherpa-onnx", "CFBundleName": "SherpaOnnxC",
            "CFBundleExecutable": "SherpaOnnxC", "CFBundlePackageType": "FMWK",
            "CFBundleVersion": "1.13.8", "CFBundleShortVersionString": "1.13.8",
            "MinimumOSVersion": minimum, "CFBundleSupportedPlatforms": ["iPhoneOS"]}
    with (framework / "Info.plist").open("wb") as stream:
        plistlib.dump(info, stream)
    run("xcrun", "strip", "-x", binary)
    run("xcrun", "install_name_tool", "-id", "@rpath/SherpaOnnxC.framework/SherpaOnnxC", binary)
    architectures = run("xcrun", "lipo", "-archs", binary, capture=True).strip()
    if architectures != "arm64":
        raise ValueError(f"Unexpected iOS architectures: {architectures}")
    load_commands = run("xcrun", "vtool", "-show-build", binary, capture=True)
    if not re.search(r"platform\s+(?:IOS|2)\b", load_commands):
        raise ValueError("iOS binary does not identify as an iPhoneOS device build")
    versions = re.findall(r"minos\s+([0-9.]+)", load_commands)
    if not versions or any(tuple(map(int, value.split("."))) > tuple(map(int, minimum.split("."))) for value in versions):
        raise ValueError("iOS binary requires a newer deployment target than configured")
    dependencies_text = run("xcrun", "otool", "-L", binary, capture=True)
    for line in dependencies_text.splitlines()[1:]:
        dependency = line.strip().split(" ", 1)[0]
        if not dependency.startswith(("/usr/lib/", "/System/Library/", "@rpath/SherpaOnnxC.framework/SherpaOnnxC")):
            raise ValueError(f"Unexpected iOS dynamic dependency: {dependency}")
    names = set(run("xcrun", "nm", "-gjU", binary, capture=True).split())
    missing = {"_" + name for name in metadata["retainedCApiSymbols"]} - names
    if missing:
        raise ValueError(f"Missing iOS C exports: {sorted(missing)}")
    run("codesign", "--force", "--sign", "-", framework)
    sdk = run("xcrun", "--sdk", "iphoneos", "--show-sdk-path", capture=True).strip()
    smoke = work / "module-smoke.swift"
    smoke.write_text("import SherpaOnnxC\nlet version = SherpaOnnxGetVersionStr()\n")
    run("xcrun", "swiftc", "-typecheck", "-sdk", sdk, "-target", f"arm64-apple-ios{minimum}",
        "-F", framework.parent, smoke)
    destination = output / "app-ios/Frameworks/SherpaOnnxC.xcframework"
    destination.parent.mkdir(parents=True)
    run("xcodebuild", "-create-xcframework", "-framework", framework, "-output", destination)
    metadata.update({"dependencies": dependencies_text, "loadCommands": load_commands,
                     "swiftModuleImport": "PASSED", "toolchain": run("xcodebuild", "-version", capture=True)})
    return build


def build_harmony(source, work, output, dependencies, native, environment, jobs, metadata):
    ort = require_one(dependencies, "libonnxruntime.so")
    environment["SHERPA_ONNXRUNTIME_LIB_DIR"] = str(ort.parent)
    environment["SHERPA_ONNXRUNTIME_INCLUDE_DIR"] = str(ort.parent.parent / "include")
    tools = native / "llvm/bin"
    build = work / "build"
    toolchain = native / "build/cmake/ohos.toolchain.cmake"
    options = {"CMAKE_TOOLCHAIN_FILE": toolchain, "OHOS_ARCH": "arm64-v8a", "BUILD_SHARED_LIBS": "ON",
               "SHERPA_ONNX_ENABLE_JNI": "OFF", "SHERPA_ONNX_ENABLE_C_API": "ON",
               "CMAKE_SHARED_LINKER_FLAGS": "-Wl,--gc-sections,--no-undefined"}
    configure_and_build(source, build, "sherpa-onnx-c-api", options, environment, jobs)
    libraries = output / "app-harmony/libs/arm64-v8a"
    copy_file(require_one(build / "lib", "libsherpa-onnx-c-api.so"), libraries / "libsherpa-onnx-c-api.so")
    copy_file(ort, libraries / "libonnxruntime.so")
    upstream_module = source / "harmony-os/SherpaOnnxHar/sherpa_onnx"
    cpp = upstream_module / "src/main/cpp"
    copy_file(source / "sherpa-onnx/c-api/c-api.h", cpp / "include/sherpa-onnx/c-api/c-api.h")
    for filename in libraries.glob("*.so"):
        copy_file(filename, cpp / "libs/arm64-v8a" / filename.name)
    bridge_build = work / "bridge-build"
    configure_and_build(cpp, bridge_build, "sherpa_onnx", options, environment, jobs)
    copy_file(require_one(bridge_build, "libsherpa_onnx.so"), libraries / "libsherpa_onnx.so")
    required = {dependency for filename in libraries.glob("*.so")
                for dependency in elf_needed(filename, tools / "llvm-readelf")}
    if "libc++_shared.so" in required:
        candidates = [filename for filename in native.rglob("libc++_shared.so") if "aarch64" in str(filename)]
        digests = {sha256_file(filename) for filename in candidates}
        if not candidates or len(digests) != 1:
            raise ValueError("Cannot select an unambiguous ARM64 libc++_shared.so from the Harmony SDK")
        copy_file(candidates[0], libraries / "libc++_shared.so")
    for filename in libraries.glob("*.so"):
        run(tools / "llvm-strip", "--strip-unneeded", filename)
    wrapper = output / "app-harmony/wrapper"
    for name in ("NonStreamingAsr", "StreamingAsr", "NonStreamingTts", "Vad"):
        copy_file(upstream_module / f"src/main/ets/components/{name}.ets", wrapper / f"{name}.ets")
    index = upstream_module / "Index.ets"
    selected_exports = re.findall(r"export\s*\{.*?\}\s*from\s*['\"][^'\"]+['\"];", index.read_text(), re.S)
    selected_exports = [block for block in selected_exports if any(
        path in block for path in ("/NonStreamingAsr'", "/StreamingAsr'", "/NonStreamingTts'", "/Vad'", '"libsherpa_onnx.so"'))]
    (wrapper / "Index.ets").write_text("\n\n".join(selected_exports).replace("./src/main/ets/components/", "./") + "\n")
    shutil.copytree(cpp / "types", output / "app-harmony/types")
    metadata["dependencies"] = verify_elf_package(libraries, "harmony", tools / "llvm-readelf",
                                                 tools / "llvm-nm", metadata["retainedCApiSymbols"])
    metadata["harmonySdkSha256"] = environment.get("HARMONY_SDK_SHA256", "local SDK; not archive-verified")
    collect_licenses(source, bridge_build, dependencies, output)
    return build


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("platform", choices=("android", "ios", "harmony"))
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--max-package-mib", type=float, default=55)
    arguments = parser.parse_args()
    if arguments.jobs < 1 or not 0 < arguments.max_package_mib < 1024:
        parser.error("jobs must be positive and package budget must be between 0 and 1024 MiB")
    if arguments.platform == "ios" and sys.platform != "darwin":
        parser.error("iOS requires macOS/Xcode; use the GitHub Actions macOS job")
    if arguments.platform in ("android", "harmony") and sys.platform != "linux":
        parser.error("This reproducible build entry point targets Linux (use Actions or WSL on Windows)")
    if LOCK["source"]["commit"] != COMMIT:
        raise ValueError("Dependency lock and source profile disagree")
    environment = dict(os.environ)
    for name in ("SHERPA_ONNXRUNTIME_LIB_DIR", "SHERPA_ONNXRUNTIME_INCLUDE_DIR", "SHERPA_ONNX_ONNXRUNTIME_ROOT"):
        environment.pop(name, None)
    for tool in ("git", "cmake", "ninja", "curl"):
        if not shutil.which(tool):
            raise ValueError(f"Required build tool not found: {tool}")
    work_parent = ROOT / ".work"
    work_parent.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=arguments.platform + "-", dir=work_parent))
    print(f"Isolated build workspace: {work}", flush=True)
    native = prepare_harmony_sdk(work) if arguments.platform == "harmony" else None
    source = work / "source"
    source.mkdir()
    run("git", "init", source)
    run("git", "-C", source, "fetch", "--depth=1", LOCK["source"]["repository"], COMMIT)
    run("git", "-C", source, "checkout", "--detach", "FETCH_HEAD")
    metadata = prepare(source)
    metadata["host"] = platform.platform()
    metadata["profile"] = "paraformer-zipformer-silero-vits-cpu-arm64"
    metadata["onnxruntime"] = LOCK[arguments.platform]["onnxruntime"]
    distribution = LOCK[arguments.platform]
    archive = download_verified(distribution["url"], distribution["sha256"], work / "onnxruntime.zip")
    dependencies = work / "onnxruntime"
    extract_zip(archive, dependencies)
    output = work / "package"
    output.mkdir()
    if arguments.platform == "android":
        build = build_android(source, work, output, archive, environment, arguments.jobs, metadata)
    elif arguments.platform == "ios":
        build = build_ios(source, work, output, dependencies, environment, arguments.jobs, metadata)
    else:
        build = build_harmony(source, work, output, dependencies, native, environment, arguments.jobs, metadata)
    collect_licenses(source, build, dependencies, output)
    copy_file(ROOT / "README.md", output / "INTEGRATION.md")
    copy_file(ROOT / "dependencies.json", output / "dependencies.json")
    package_output(output, arguments.platform, metadata, arguments.max_package_mib)
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    for filename in (work / f"sherpa-onnx-lite-{arguments.platform}-arm64.zip",
                     work / f"{arguments.platform}-size-report.json"):
        copy_file(filename, destination / filename.name)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(f"BUILD BLOCKED: {error}", file=sys.stderr)
        sys.exit(1)

"""Compile the selected Kotlin API and combine selected JNI ABIs into one AAR."""

import hashlib
import io
import json
import zipfile
from xml.etree import ElementTree

from build_support import LOCK, ROOT, copy_file, run, sha256_file
from profile_lite import KOTLIN_FILES

ANDROID_ABIS = ("arm64-v8a", "armeabi-v7a")
JNI_KEEP_RULE = "-keep class com.k2fsa.sherpa.onnx.** { *; }"
REQUIRED_CLASSES = {
    "OfflineRecognizer", "OfflineStream", "OnlineRecognizer", "OnlineStream",
    "OfflineTts", "Vad", "VersionInfo",
}
EXCLUDED_CLASSES = {
    "KeywordSpotter", "AudioTagging", "OfflineSpeakerDiarization",
    "SpeakerEmbeddingExtractor", "OnlineSpeechDenoiser", "OfflineSpeechDenoiser",
}


def select_android_abis(requested=None):
    if requested is None:
        return ("arm64-v8a",)
    if not requested or len(set(requested)) != len(requested):
        raise ValueError("Select at least one Android ABI, without duplicates")
    unknown = set(requested) - set(ANDROID_ABIS)
    if unknown:
        raise ValueError(f"Unsupported Android ABIs: {sorted(unknown)}")
    return tuple(abi for abi in ANDROID_ABIS if abi in requested)


def prepare_aar_project(source, project, native_libraries, output, abis, metadata):
    abis = select_android_abis(abis)
    if project.exists():
        raise ValueError(f"AAR project must use a new isolated directory: {project}")
    project.mkdir(parents=True)
    configuration = LOCK["android"]["aar"]
    upstream = source / "android/SherpaOnnxAar"
    for relative in ("gradlew", "gradle/wrapper/gradle-wrapper.jar", "gradle/wrapper/gradle-wrapper.properties"):
        copy_file(upstream / relative, project / relative)
    properties = project / "gradle/wrapper/gradle-wrapper.properties"
    wrapper = properties.read_text(encoding="utf-8")
    expected_url = f"https\\://services.gradle.org/distributions/gradle-{configuration['gradle']}-bin.zip"
    if f"distributionUrl={expected_url}" not in wrapper or "distributionSha256Sum=" in wrapper:
        raise ValueError("Pinned upstream Gradle wrapper changed; review before building")
    properties.write_text(wrapper.rstrip() + f"\ndistributionSha256Sum={configuration['gradleSha256']}\n",
                          encoding="utf-8")
    (project / "settings.gradle.kts").write_text('''pluginManagement {
    repositories { google(); mavenCentral(); gradlePluginPortal() }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories { google(); mavenCentral() }
}
rootProject.name = "sherpa-onnx-lite"
include(":sherpa_onnx")
''', encoding="utf-8")
    (project / "build.gradle.kts").write_text(
        'plugins {\n'
        f'    id("com.android.library") version "{configuration["androidGradlePlugin"]}" apply false\n'
        f'    id("org.jetbrains.kotlin.android") version "{configuration["kotlin"]}" apply false\n'
        '}\n', encoding="utf-8")
    (project / "gradle.properties").write_text(
        "org.gradle.jvmargs=-Xmx2g -Dfile.encoding=UTF-8\norg.gradle.workers.max=2\n",
        encoding="utf-8")
    module = project / "sherpa_onnx"
    module.mkdir()
    selected = ", ".join(json.dumps(abi) for abi in abis)
    (module / "build.gradle.kts").write_text(f'''plugins {{
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}}
android {{
    namespace = "com.k2fsa.sherpa.onnx"
    compileSdk = {configuration["compileSdk"]}
    defaultConfig {{
        minSdk = {LOCK["android"]["api"]}
        ndk {{ abiFilters.addAll(listOf({selected})) }}
        consumerProguardFiles("consumer-rules.pro")
    }}
    buildTypes {{ release {{ isMinifyEnabled = false }} }}
    compileOptions {{
        sourceCompatibility = JavaVersion.VERSION_1_8
        targetCompatibility = JavaVersion.VERSION_1_8
    }}
    kotlinOptions {{ jvmTarget = "1.8" }}
    packaging {{ jniLibs {{ keepDebugSymbols.add("**/*.so") }} }}
}}
''', encoding="utf-8")
    # JNI accesses configuration fields reflectively; preserve their names in consuming apps.
    (module / "consumer-rules.pro").write_text(JNI_KEEP_RULE + "\n", encoding="utf-8")
    main = module / "src/main"
    main.mkdir(parents=True)
    (main / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android" />\n', encoding="utf-8")
    for filename in KOTLIN_FILES:
        copy_file(source / "sherpa-onnx/kotlin-api" / filename,
                  main / "java/com/k2fsa/sherpa/onnx" / filename)
    for abi in abis:
        copy_file(native_libraries / abi / "libsherpa-onnx-jni.so",
                  main / "jniLibs" / abi / "libsherpa-onnx-jni.so")
    notices = main / "assets/sherpa-onnx-lite"
    for filename in sorted((output / "licenses").rglob("*")):
        if filename.is_file():
            copy_file(filename, notices / "licenses" / filename.relative_to(output / "licenses"))
    copy_file(ROOT / "dependencies.json", notices / "dependencies.json")
    (notices / "profile.json").write_text(json.dumps({
        "sourceCommit": metadata["sourceCommit"], "profile": metadata["profile"],
        "abis": list(abis), "runtimeValidation": "NOT RUN",
        "onnxruntimeOperatorPruning": False,
    }, indent=2) + "\n", encoding="utf-8")
    return project


def verify_android_aar(archive, native_libraries, abis):
    abis = select_android_abis(abis)
    expected_libraries = {f"jni/{abi}/libsherpa-onnx-jni.so" for abi in abis}
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        if len(names) != len(bundle.namelist()):
            raise ValueError("AAR contains duplicate ZIP entries")
        required = {"AndroidManifest.xml", "classes.jar", "proguard.txt"}
        if not required <= names:
            raise ValueError(f"AAR is missing required entries: {sorted(required - names)}")
        packaged_libraries = {name for name in names if name.endswith(".so")}
        if packaged_libraries != expected_libraries:
            raise ValueError(f"AAR ABI/library mismatch: {sorted(packaged_libraries)}")
        for abi in abis:
            original = native_libraries / abi / "libsherpa-onnx-jni.so"
            packed_digest = hashlib.sha256(bundle.read(f"jni/{abi}/{original.name}")).hexdigest()
            if packed_digest != sha256_file(original):
                raise ValueError(f"AAR changed the verified native library for {abi}")
        manifest = ElementTree.fromstring(bundle.read("AndroidManifest.xml"))
        minimum = manifest.find("uses-sdk")
        if minimum is None or minimum.get("{http://schemas.android.com/apk/res/android}minSdkVersion") != str(LOCK["android"]["api"]):
            raise ValueError("AAR minimum SDK differs from the native build")
        if JNI_KEEP_RULE not in bundle.read("proguard.txt").decode("utf-8"):
            raise ValueError("AAR is missing the JNI consumer keep rule")
        if not any(name.startswith("assets/sherpa-onnx-lite/licenses/") for name in names):
            raise ValueError("AAR is missing third-party licenses")
        with zipfile.ZipFile(io.BytesIO(bundle.read("classes.jar"))) as classes:
            prefix = "com/k2fsa/sherpa/onnx/"
            class_names = set(classes.namelist())
            for class_name in REQUIRED_CLASSES:
                entry = f"{prefix}{class_name}.class"
                if entry not in class_names or not classes.read(entry).startswith(b"\xca\xfe\xba\xbe"):
                    raise ValueError(f"AAR is missing compiled Kotlin API: {class_name}")
            for class_name in EXCLUDED_CLASSES:
                if f"{prefix}{class_name}.class" in class_names:
                    raise ValueError(f"AAR contains excluded API: {class_name}")
    return {"abis": list(abis), "bytes": archive.stat().st_size,
            "sha256": sha256_file(archive), "structureVerification": "PASSED"}


def build_android_aar(source, work, native_libraries, output, abis, environment, metadata):
    project = prepare_aar_project(source, work / "aar-project", native_libraries, output, abis, metadata)
    run("bash", project / "gradlew", "--no-daemon", "--console=plain", ":sherpa_onnx:assembleRelease",
        cwd=project, environment=environment)
    built = project / "sherpa_onnx/build/outputs/aar/sherpa_onnx-release.aar"
    result = verify_android_aar(built, native_libraries, abis)
    filename = f"sherpa-onnx-lite-android-{LOCK['source']['version']}-{'-'.join(abis)}.aar"
    destination = output / "app-android/libs" / filename
    copy_file(built, destination)
    verify_android_aar(destination, native_libraries, abis)
    metadata["androidAar"] = dict(result, path=destination.relative_to(output).as_posix())
    metadata["androidAarToolchain"] = LOCK["android"]["aar"]
    metadata["kotlinCompilation"] = "PASSED"
    return destination

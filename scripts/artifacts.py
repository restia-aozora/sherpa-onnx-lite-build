"""Verify native dependency boundaries and produce an auditable size report."""

import json
import re
import struct
import zipfile

from build_support import copy_file, run, sha256_file

ANDROID_SYSTEM = {"libc.so", "libm.so", "libdl.so", "liblog.so", "libandroid.so", "libz.so"}
HARMONY_SYSTEM = {"libc.so", "libm.so", "libdl.so", "libace_napi.z.so", "libhilog_ndk.z.so", "librawfile.z.so"}


def inspect_elf_header(filename, require_16k=False):
    with filename.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01":
            raise ValueError(f"Not a little-endian ELF64 library: {filename}")
        if struct.unpack_from("<H", header, 18)[0] != 183:
            raise ValueError(f"Not AArch64: {filename}")
        offset = struct.unpack_from("<Q", header, 32)[0]
        entry_size, entry_count = struct.unpack_from("<HH", header, 54)
        if entry_size < 56:
            raise ValueError(f"Invalid program header size: {filename}")
        load_count = 0
        for index in range(entry_count):
            stream.seek(offset + index * entry_size)
            segment = stream.read(entry_size)
            if struct.unpack_from("<I", segment)[0] != 1:
                continue
            load_count += 1
            alignment = struct.unpack_from("<Q", segment, 48)[0]
            if require_16k and alignment < 16384:
                raise ValueError(f"Android LOAD alignment below 16 KB: {filename}")
        if load_count == 0:
            raise ValueError(f"ELF contains no loadable segments: {filename}")


def elf_needed(filename, readelf):
    output = run(readelf, "--dynamic", filename, capture=True)
    return re.findall(r"\(NEEDED\).*?\[([^]]+)\]", output)


def verify_elf_package(libraries, platform, readelf, nm, c_symbols):
    available = {filename.name for filename in libraries.glob("*.so")}
    system = ANDROID_SYSTEM if platform == "android" else HARMONY_SYSTEM
    report = {}
    for filename in libraries.glob("*.so"):
        inspect_elf_header(filename, require_16k=platform == "android")
        dependencies = elf_needed(filename, readelf)
        unresolved = set(dependencies) - available - system
        if unresolved:
            raise ValueError(f"Unpackaged dependencies for {filename.name}: {sorted(unresolved)}")
        report[filename.name] = dependencies
    primary = libraries / ("libsherpa-onnx-jni.so" if platform == "android" else "libsherpa-onnx-c-api.so")
    exported = run(nm, "--dynamic", "--defined-only", primary, capture=True)
    if platform == "android":
        for class_name in ("OfflineRecognizer", "OnlineRecognizer", "OfflineTts", "Vad"):
            if f"Java_com_k2fsa_sherpa_onnx_{class_name}_" not in exported:
                raise ValueError(f"Missing JNI class exports: {class_name}")
        for class_name in ("KeywordSpotter", "AudioTagging", "OfflineSpeakerDiarization"):
            if f"Java_com_k2fsa_sherpa_onnx_{class_name}_" in exported:
                raise ValueError(f"Unexpected JNI exports: {class_name}")
    else:
        names = {line.split()[-1] for line in exported.splitlines() if line.split()}
        if not set(c_symbols) <= names:
            raise ValueError(f"Missing C exports: {sorted(set(c_symbols) - names)}")
        extra = {name for name in names if name.startswith("SherpaOnnx")} - set(c_symbols)
        if extra:
            raise ValueError(f"Unexpected C exports: {sorted(extra)}")
        undefined = run(nm, "--dynamic", "--undefined-only", libraries / "libsherpa_onnx.so", capture=True)
        required = set(re.findall(r"\bSherpaOnnx\w+", undefined))
        if not required <= names:
            raise ValueError(f"N-API refers to removed C exports: {sorted(required - names)}")
    return report


def collect_licenses(source, build, dependencies, output):
    legal_names = ("license", "copying", "notice", "copyright")
    count = 0
    for label, root in (("sherpa", source), ("build-dependencies", build / "_deps"), ("onnxruntime", dependencies)):
        if not root.exists():
            continue
        for filename in root.rglob("*"):
            if ".git" in filename.parts or not filename.is_file():
                continue
            if filename.name.lower().startswith(legal_names) and filename.stat().st_size < 2 * 1024 * 1024:
                copy_file(filename, output / "licenses" / label / filename.relative_to(root))
                count += 1
    if count == 0:
        raise ValueError("No upstream license files collected")


def package_output(output, platform, metadata, max_package_mib):
    records = [{"path": filename.relative_to(output).as_posix(), "bytes": filename.stat().st_size,
                "sha256": sha256_file(filename)}
               for filename in sorted(output.rglob("*")) if filename.is_file()]
    metadata.update({"platform": platform, "architecture": "arm64", "files": records,
                     "uncompressedBytes": sum(record["bytes"] for record in records),
                     "modelInference": "NOT RUN", "utsCompilation": "NOT RUN",
                     "physicalDevice": "NOT RUN", "onnxruntimeOperatorPruning": False})
    (output / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    archive = output.parent / f"sherpa-onnx-lite-{platform}-arm64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for filename in sorted(output.rglob("*")):
            if filename.is_file():
                bundle.write(filename, filename.relative_to(output))
    summary = {"archive": archive.name, "archiveBytes": archive.stat().st_size,
               "archiveSha256": sha256_file(archive), "uncompressedBytes": metadata["uncompressedBytes"],
               "nativeLinking": "PASSED", "runtimeValidation": "NOT RUN",
               "maximumPackageMiB": max_package_mib}
    summary["withinBudget"] = summary["archiveBytes"] <= max_package_mib * 1024 * 1024
    (output.parent / f"{platform}-size-report.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if not summary["withinBudget"]:
        raise ValueError("Library archive exceeds configured budget; this budget is NOT the whole App upload size")

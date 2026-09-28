"""Download verification and isolated build filesystem helpers."""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "dependencies.json").read_text(encoding="utf-8"))


def run(*arguments, cwd=None, capture=False, environment=None):
    command = [str(argument) for argument in arguments]
    print("+ " + " ".join(command), flush=True)
    result = subprocess.run(command, cwd=cwd, env=environment, check=True,
                            text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else None


def sha256_file(filename):
    with Path(filename).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download_verified(url, expected_digest, destination):
    if urlparse(url).scheme != "https" or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ValueError("An HTTPS URL and an explicit lowercase SHA-256 are required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(destination) != expected_digest:
            raise ValueError(f"Cached archive checksum mismatch: {destination}")
        return destination
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        run("curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
            "--retry", "3", "--connect-timeout", "30", "--max-time", "1800",
            "--output", temporary, url)
        if sha256_file(temporary) != expected_digest:
            raise ValueError(f"Downloaded archive checksum mismatch: {destination.name}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def extract_zip(archive, destination, member_prefix=None):
    prefix = PurePosixPath(member_prefix) if member_prefix is not None else PurePosixPath(".")
    if prefix.is_absolute() or ".." in prefix.parts or "\\" in str(prefix) or ":" in str(prefix):
        raise ValueError(f"Unsafe ZIP prefix: {prefix}")
    if destination.exists():
        raise ValueError(f"Extraction directory must be new: {destination}")
    destination.mkdir(parents=True)
    destination = destination.resolve()
    extraction_boundary = destination.joinpath(*prefix.parts)
    with zipfile.ZipFile(archive) as bundle:
        links = []
        for entry in bundle.infolist():
            relative = PurePosixPath(entry.filename)
            if relative.is_absolute() or ".." in relative.parts or "\\" in entry.filename or ":" in entry.filename:
                raise ValueError(f"Unsafe ZIP path: {entry.filename}")
            if not relative.is_relative_to(prefix):
                continue
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(extraction_boundary):
                raise ValueError(f"ZIP path escapes destination: {entry.filename}")
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                link = bundle.read(entry).decode("utf-8")
                if Path(link).is_absolute() or not (target.parent / link).resolve().is_relative_to(extraction_boundary):
                    raise ValueError(f"Unsafe ZIP symlink: {entry.filename}")
                links.append((target, link))
                continue
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                if mode & 0o777:
                    target.chmod(mode & 0o777)
        # Create links last so no regular file is written through an archive symlink.
        for target, link in links:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)


def require_one(directory, pattern):
    matches = list(directory.rglob(pattern))
    if len(matches) != 1:
        raise ValueError(f"Expected one {pattern} under {directory}, found {len(matches)}")
    return matches[0]


def copy_file(source, destination):
    # Upstream Harmony headers can already link to the source file.
    if destination.exists() and source.samefile(destination):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def extract_harmony_native_sdk(archive, destination):
    # The official command-line bundle also contains Node, emulators and ArkTS tools.
    # Only the Native SDK subtree is needed by this CMake build.
    with zipfile.ZipFile(archive) as bundle:
        toolchains = [PurePosixPath(entry.filename) for entry in bundle.infolist()
                      if not entry.is_dir() and
                      PurePosixPath(entry.filename).parts[-3:] == ("build", "cmake", "ohos.toolchain.cmake")]
    if len(toolchains) != 1:
        raise ValueError(f"Expected exactly one Harmony Native SDK in the archive, found {len(toolchains)}")
    native_prefix = toolchains[0].parents[2]
    extract_zip(archive, destination, member_prefix=native_prefix)
    return destination.resolve().joinpath(*native_prefix.parts)


def prepare_harmony_sdk(work):
    configured = os.environ.get("OHOS_SDK_NATIVE_DIR")
    if configured:
        native = Path(configured).resolve()
    else:
        url = os.environ.get("HARMONY_SDK_URL", "")
        digest = os.environ.get("HARMONY_SDK_SHA256", "")
        if not url or not digest:
            raise ValueError("Harmony requires OHOS_SDK_NATIVE_DIR, or HARMONY_SDK_URL and HARMONY_SDK_SHA256. "
                             "Obtain an authorized Linux SDK archive from Huawei and configure its checksum.")
        archive = download_verified(url, digest, work / "harmony-sdk.zip")
        native = extract_harmony_native_sdk(archive, work / "sdk")
    for relative in ("build/cmake/ohos.toolchain.cmake", "llvm/bin/clang", "llvm/bin/clang++",
                     "llvm/bin/llvm-readelf", "llvm/bin/llvm-nm", "llvm/bin/llvm-strip"):
        if not (native / relative).is_file():
            raise ValueError(f"Incomplete Linux Harmony Native SDK: {native / relative}")
    if not (native / "sysroot").is_dir():
        raise ValueError(f"Incomplete Linux Harmony Native SDK: {native / 'sysroot'}")
    return native

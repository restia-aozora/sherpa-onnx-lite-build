import itertools
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/build.yml"
ABI_INPUTS = (
    ("android_arm64_v8a", "BUILD_ARM64", "arm64-v8a", "true"),
    ("android_armeabi_v7a", "BUILD_ARMV7", "armeabi-v7a", "false"),
)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def read_step(self, name):
        match = re.search(
            r"^      - name: " + re.escape(name) + r"\n((?: {8}[^\n]*\n|\n)*)",
            self.workflow, re.MULTILINE)
        self.assertIsNotNone(match, f"Missing workflow step: {name}")
        return match.group(1)

    def find_bash(self):
        if os.name != "nt":
            executable = shutil.which("bash")
        else:
            candidates = [Path("C:/Program Files/Git/bin/bash.exe"),
                          Path("C:/Program Files/Git/usr/bin/bash.exe")]
            git_executable = shutil.which("git")
            if git_executable:
                git_root = Path(git_executable).resolve().parent.parent
                candidates.extend((git_root / "bin/bash.exe", git_root / "usr/bin/bash.exe"))
            executable = next((str(candidate) for candidate in candidates if candidate.is_file()), None)
        if not executable:
            self.skipTest("Bash is required to execute workflow selection tests")
        return executable

    def test_android_checkboxes_are_optional_and_default_to_arm64_only(self):
        for input_name, _, _, default in ABI_INPUTS:
            with self.subTest(input_name=input_name):
                match = re.search(r"^      " + input_name + r":\n((?: {8}[^\n]*\n)+)",
                                  self.workflow, re.MULTILINE)
                self.assertIsNotNone(match)
                fields = match.group(1)
                self.assertIn("        type: boolean\n", fields)
                self.assertIn("        required: false\n", fields)
                self.assertIn(f"        default: {default}\n", fields)

    def test_each_checkbox_is_wired_to_validation_and_build(self):
        validation = self.read_step("Validate Android ABI selection")
        build_step = self.read_step("Build selected Android ABIs into one AAR")
        for input_name, variable, _, _ in ABI_INPUTS:
            expected = f"          {variable}: ${{{{ inputs.{input_name} }}}}\n"
            self.assertIn(expected, validation)
            self.assertIn(expected, build_step)
        self.assertLess(self.workflow.index("name: Validate Android ABI selection"),
                        self.workflow.index("name: Install native build tools"))

    def test_all_checkbox_combinations_pass_exact_abis_or_fail_before_build(self):
        bash = self.find_bash()
        validation = self.read_step("Validate Android ABI selection")
        build_step = self.read_step("Build selected Android ABIs into one AAR")
        validation_script = textwrap.dedent(validation.split("        run: |\n", 1)[1])
        build_script = textwrap.dedent(build_step.split("        run: |\n", 1)[1])
        # Capture the native build boundary without downloading SDKs or compiling.
        capture_command = "python3() { printf 'BUILD_ARGUMENT=%s\\n' \"$@\"; }\n"
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary) / "Android SDK"
            toolchain = sdk / "ndk/28.2.13676358/build/cmake/android.toolchain.cmake"
            toolchain.parent.mkdir(parents=True)
            toolchain.touch()
            for flags in itertools.product((False, True), repeat=len(ABI_INPUTS)):
                selected = [entry[2] for entry, enabled in zip(ABI_INPUTS, flags) if enabled]
                with self.subTest(selected=selected):
                    environment = os.environ.copy()
                    environment["ANDROID_HOME"] = sdk.as_posix()
                    for entry, enabled in zip(ABI_INPUTS, flags):
                        environment[entry[1]] = str(enabled).lower()
                    result = subprocess.run(
                        [bash, "--noprofile", "--norc", "-e", "-c",
                         capture_command + validation_script + build_script],
                        env=environment, capture_output=True, text=True, timeout=15)
                    arguments = [line.removeprefix("BUILD_ARGUMENT=")
                                 for line in result.stdout.splitlines()
                                 if line.startswith("BUILD_ARGUMENT=")]
                    if selected:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(arguments, ["scripts/build.py", "android", "--android-abis",
                                                     *selected, "--jobs", "2", "--max-package-mib", "55"])
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("Select at least one Android ABI", result.stdout)
                        self.assertEqual(arguments, [])

    def test_platform_routes_remain_independent_and_non_android_ignores_abis(self):
        job_sections = re.split(r"^  (build-[a-z]+):\n", self.workflow, flags=re.MULTILINE)
        self.assertEqual(set(job_sections[1::2]), {"build-android", "build-ios", "build-harmony"})
        for name, body in zip(job_sections[1::2], job_sections[2::2]):
            platform = name.removeprefix("build-")
            self.assertIn(f"    if: inputs.platform == 'all' || inputs.platform == '{platform}'\n", body)
            self.assertNotIn("    needs:", body)
            if platform != "android":
                self.assertNotIn("--android-abis", body)
                self.assertNotIn("inputs.android_", body)


if __name__ == "__main__":
    unittest.main()

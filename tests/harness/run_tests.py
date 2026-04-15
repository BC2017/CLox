#!/usr/bin/env python3
"""CLox test harness.

Builds a debug-trace-free `clox-test` binary (without DEBUG_TRACE_EXECUTION
defined, so stdout isn't polluted by the VM trace), discovers .lox test
files under tests/cases/, and runs each one. Expected output is described
inline in each test file using comment markers:

    // expect: <stdout line>
    // expect runtime error: <substring of stderr>
    // expect compile error:  <substring of stderr>

Multiple `// expect:` lines are matched in order against stdout lines.
The error markers may omit the `: <message>` portion to match any error
of that kind. A test with no expectation comments is skipped with a warning.

Usage:
    python tests/harness/run_tests.py             # run everything
    python tests/harness/run_tests.py tests/cases/arithmetic
    python tests/harness/run_tests.py tests/cases/strings/concatenation.lox
    python tests/harness/run_tests.py --no-build  # skip rebuild check

Requires Python 3.9+ and gcc on PATH.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
CASES_DIR = SCRIPT_DIR.parent / "cases"
BIN_DIR = PROJECT_ROOT / "bin"
TEST_BINARY = BIN_DIR / ("clox-test.exe" if platform.system() == "Windows" else "clox-test")
TIMEOUT_SECONDS = 10

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s


def green(s: str) -> str: return _c("32", s)
def red(s: str) -> str: return _c("31", s)
def yellow(s: str) -> str: return _c("33", s)
def gray(s: str) -> str: return _c("90", s)
def bold(s: str) -> str: return _c("1", s)


EXPECT_OUTPUT_RE = re.compile(r"//\s*expect:\s*(.*)$")
EXPECT_RUNTIME_ERROR_RE = re.compile(r"//\s*expect runtime error\s*(?::\s*(.*))?$")
EXPECT_COMPILE_ERROR_RE = re.compile(r"//\s*expect compile error\s*(?::\s*(.*))?$")


@dataclass
class TestCase:
    path: Path
    expected_output: list = field(default_factory=list)
    expected_compile_error: object = None  # None | str ("" means accept any message)
    expected_runtime_error: object = None

    @property
    def display_path(self) -> str:
        try:
            return str(self.path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        except ValueError:
            return str(self.path)

    @property
    def has_expectations(self) -> bool:
        return (
            bool(self.expected_output)
            or self.expected_compile_error is not None
            or self.expected_runtime_error is not None
        )


@dataclass
class TestResult:
    test: TestCase
    passed: bool
    failure_reason: str = ""


def parse_test(lox_path: Path) -> TestCase:
    test = TestCase(path=lox_path)
    text = lox_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        m = EXPECT_OUTPUT_RE.search(line)
        if m:
            test.expected_output.append(m.group(1).rstrip())
            continue
        m = EXPECT_RUNTIME_ERROR_RE.search(line)
        if m:
            test.expected_runtime_error = m.group(1) or ""
            continue
        m = EXPECT_COMPILE_ERROR_RE.search(line)
        if m:
            test.expected_compile_error = m.group(1) or ""
            continue
    return test


def needs_rebuild(binary: Path) -> bool:
    if not binary.exists():
        return True
    binary_mtime = binary.stat().st_mtime
    sources = list(SRC_DIR.glob("*.c")) + list(SRC_DIR.glob("*.h"))
    return any(s.stat().st_mtime > binary_mtime for s in sources)


DEBUG_DEFINE_RE = re.compile(
    r"^([ \t]*#[ \t]*define[ \t]+)(DEBUG_TRACE_EXECUTION|DEBUG_PRINT_CODE)\b.*$",
    re.MULTILINE,
)


def generate_common_override() -> Path:
    """Read src/common.h and write a copy with debug-trace defines stripped.

    Used with gcc's -include flag so it's processed before any source file.
    Once it executes, common.h's include guard (clox_common_h) is set, so
    when source files later #include "common.h" the original body is skipped
    and DEBUG_TRACE_EXECUTION is never defined.
    """
    common_h = SRC_DIR / "common.h"
    text = common_h.read_text(encoding="utf-8")
    stripped = DEBUG_DEFINE_RE.sub(r"// [test override] \1\2 disabled", text)
    override = BIN_DIR / "_test_common_override.h"
    override.write_text(stripped, encoding="utf-8")
    return override


def build_test_binary() -> bool:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    sources = sorted(SRC_DIR.glob("*.c"))
    if not sources:
        print(red(f"No .c files found in {SRC_DIR}"))
        return False
    override_h = generate_common_override()
    cmd = [
        "gcc", "-std=c99", "-Wall", "-Wextra",
        f"-I{SRC_DIR}",
        "-include", str(override_h),
        "-o", str(TEST_BINARY),
        *map(str, sources),
    ]
    print(gray(f"  building {TEST_BINARY.name} (DEBUG_TRACE_EXECUTION disabled via -include)..."))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print(red("  gcc not found on PATH — install gcc or set up your build environment"))
        return False
    if result.returncode != 0:
        print(red("  failed to build test binary:"))
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        return False
    return True


def run_test(test: TestCase) -> TestResult:
    try:
        proc = subprocess.run(
            [str(TEST_BINARY), str(test.path)],
            capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, errors="replace",
        )
    except subprocess.TimeoutExpired:
        return TestResult(test, False, f"timed out after {TIMEOUT_SECONDS}s")
    except FileNotFoundError:
        return TestResult(test, False, f"test binary not found: {TEST_BINARY}")

    stdout = proc.stdout.replace("\r\n", "\n").replace("\r", "\n")
    stderr = proc.stderr.replace("\r\n", "\n").replace("\r", "\n")
    stdout_lines = stdout.splitlines()

    if test.expected_compile_error is not None:
        if proc.returncode != 65:
            return TestResult(test, False,
                f"expected compile error (exit 65), got exit {proc.returncode}\n"
                f"        stdout: {stdout!r}\n"
                f"        stderr: {stderr.strip()!r}")
        if test.expected_compile_error and test.expected_compile_error not in stderr:
            return TestResult(test, False,
                f"compile error message mismatch\n"
                f"        expected substring: {test.expected_compile_error!r}\n"
                f"        actual stderr:      {stderr.strip()!r}")
        return TestResult(test, True)

    if test.expected_runtime_error is not None:
        if proc.returncode != 70:
            return TestResult(test, False,
                f"expected runtime error (exit 70), got exit {proc.returncode}\n"
                f"        stdout: {stdout!r}\n"
                f"        stderr: {stderr.strip()!r}")
        if test.expected_runtime_error and test.expected_runtime_error not in stderr:
            return TestResult(test, False,
                f"runtime error message mismatch\n"
                f"        expected substring: {test.expected_runtime_error!r}\n"
                f"        actual stderr:      {stderr.strip()!r}")
        return TestResult(test, True)

    if proc.returncode != 0:
        return TestResult(test, False,
            f"expected success (exit 0), got exit {proc.returncode}\n"
            f"        stderr: {stderr.strip()!r}")
    if stdout_lines != test.expected_output:
        return TestResult(test, False,
            f"output mismatch\n"
            f"        expected: {test.expected_output}\n"
            f"        actual:   {stdout_lines}")
    return TestResult(test, True)


def discover_tests(filter_path):
    if filter_path:
        target = Path(filter_path)
        if not target.is_absolute():
            target = (PROJECT_ROOT / filter_path).resolve()
        if target.is_file():
            return [target]
        if target.is_dir():
            return sorted(target.rglob("*.lox"))
        print(red(f"filter path not found: {filter_path}"))
        return []
    return sorted(CASES_DIR.rglob("*.lox"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run CLox tests")
    parser.add_argument("filter", nargs="?",
                        help="Optional test file or directory to limit the run")
    parser.add_argument("--no-build", action="store_true",
                        help="Skip rebuilding the test binary")
    args = parser.parse_args()

    if not args.no_build:
        if needs_rebuild(TEST_BINARY) and not build_test_binary():
            return 1
    if not TEST_BINARY.exists():
        print(red(f"test binary not found: {TEST_BINARY} (run without --no-build)"))
        return 1

    test_files = discover_tests(args.filter)
    if not test_files:
        print(red(f"no .lox test files found under {CASES_DIR}"))
        return 1

    print(f"running {len(test_files)} test(s) with {TEST_BINARY.name}\n")

    passed = failed = skipped = 0
    for path in test_files:
        test = parse_test(path)
        if not test.has_expectations:
            print(f"  {yellow('SKIP')}  {test.display_path}  (no // expect: comments)")
            skipped += 1
            continue
        result = run_test(test)
        if result.passed:
            passed += 1
            print(f"  {green('PASS')}  {test.display_path}")
        else:
            failed += 1
            print(f"  {red('FAIL')}  {test.display_path}")
            print(gray(f"        {result.failure_reason}"))

    print()
    summary = f"{passed} passed, {failed} failed"
    if skipped:
        summary += f", {skipped} skipped"
    if failed == 0:
        print(green(bold(summary)))
        return 0
    print(red(bold(summary)))
    return 1


if __name__ == "__main__":
    sys.exit(main())

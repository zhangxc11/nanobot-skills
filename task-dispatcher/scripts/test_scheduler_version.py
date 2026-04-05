#!/usr/bin/env python3
"""Test that SCHEDULER_VERSION constant exists and has correct format in scheduler.py."""

import re
import sys
from pathlib import Path

def test_version_constant():
    """Verify SCHEDULER_VERSION exists in scheduler.py source and has semver format."""
    scheduler_path = Path(__file__).resolve().parent / "scheduler.py"
    source = scheduler_path.read_text()

    # 1. Check the constant is defined
    match = re.search(r"^SCHEDULER_VERSION\s*=\s*['\"](.+?)['\"]", source, re.MULTILINE)
    assert match, "SCHEDULER_VERSION constant not found in scheduler.py"

    version = match.group(1)
    print(f"Found SCHEDULER_VERSION = '{version}'")

    # 2. Check value is '1.1.0'
    assert version == '1.1.0', f"Expected '1.1.0', got '{version}'"

    # 3. Check semver format (X.Y.Z)
    assert re.match(r'^\d+\.\d+\.\d+$', version), f"Version '{version}' is not valid semver"

    # 4. Check it appears before imports (i.e., near the top of the file after docstring)
    docstring_end = source.find('"""', source.find('"""') + 3) + 3
    version_pos = source.find("SCHEDULER_VERSION")
    first_import = source.find("\nimport ")
    assert version_pos < first_import, "SCHEDULER_VERSION should appear before imports"
    assert version_pos > docstring_end, "SCHEDULER_VERSION should appear after docstring"

    print("All checks passed!")
    return True

if __name__ == "__main__":
    try:
        test_version_constant()
        sys.exit(0)
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)

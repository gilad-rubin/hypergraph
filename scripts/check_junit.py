"""Fail unless a pytest JUnit report shows zero skipped tests and zero warnings, as CI requires.

Usage: python3 scripts/check_junit.py pytest.xml
"""

import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
suite = root if root.tag == "testsuite" else root.find("testsuite")
if suite is None:
    raise SystemExit(f"Could not find testsuite in {sys.argv[1]}")
skipped = int(suite.attrib.get("skipped", "0"))
warnings = int(suite.attrib.get("warnings", "0"))
if skipped:
    raise SystemExit(f"CI requires zero skipped tests, found {skipped}")
if warnings:
    raise SystemExit(f"CI requires zero warnings, found {warnings}")
print("Pytest reported zero skips and zero warnings.")

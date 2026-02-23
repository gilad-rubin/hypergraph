#!/usr/bin/env python3
"""Verify code examples in getting-started.md run correctly."""

import re
import subprocess
import sys
from pathlib import Path

# Examples that require external dependencies (marked, not tested)
EXTERNAL_DEP_MARKERS = [
    "model.embed",
    "db.search",
    "requests.get",
    "httpx",
    "openai",
]

# Examples that intentionally raise errors (test differently)
ERROR_EXAMPLES = [
    "process.with_inputs(y=",  # RenameError
    'renamed.with_inputs(x="different")',  # RenameError
]


def extract_code_blocks(md_content: str) -> list[tuple[int, str]]:
    """Extract Python code blocks with their line numbers."""
    pattern = r"```python\n(.*?)```"
    blocks = []

    for match in re.finditer(pattern, md_content, re.DOTALL):
        # Find line number
        start_pos = match.start()
        line_num = md_content[:start_pos].count("\n") + 1
        code = match.group(1).strip()
        blocks.append((line_num, code))

    return blocks


def needs_external_deps(code: str) -> bool:
    """Check if code requires external dependencies."""
    return any(marker in code for marker in EXTERNAL_DEP_MARKERS)


def is_error_example(code: str) -> bool:
    """Check if code is meant to demonstrate an error."""
    return any(marker in code for marker in ERROR_EXAMPLES)


def test_code_block(code: str, line_num: int) -> tuple[bool, str]:
    """Test a code block. Returns (success, message)."""

    if needs_external_deps(code):
        return True, f"Line {line_num}: SKIP (external deps)"

    if is_error_example(code):
        return True, f"Line {line_num}: SKIP (error example)"

    # Add import at top if not present
    if "from hypergraph import" not in code and "@node" in code or "from hypergraph import" not in code:
        code = "from hypergraph import node, FunctionNode, Graph\n" + code

    # Remove comment-only lines that show expected output
    lines = code.split("\n")
    exec_lines = []
    for line in lines:
        # Keep lines that aren't pure "# output" comments
        if not line.strip().startswith("# ") or "=" in line or "import" in line.lower():
            exec_lines.append(line)
        elif line.strip().startswith("# Warning:") or line.strip().startswith("# RenameError:"):
            # Skip expected output comments
            pass
        elif line.strip().startswith("# "):
            # Keep other comments
            exec_lines.append(line)

    code = "\n".join(exec_lines)

    try:
        result = subprocess.run(
            ["uv", "run", "python", "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=Path(__file__).parent.parent,
        )

        if result.returncode == 0:
            return True, f"Line {line_num}: PASS"
        else:
            return False, f"Line {line_num}: FAIL\n  Code:\n{indent(code)}\n  Error: {result.stderr[:500]}"

    except subprocess.TimeoutExpired:
        return False, f"Line {line_num}: TIMEOUT"
    except Exception as e:
        return False, f"Line {line_num}: ERROR - {e}"


def indent(text: str, prefix: str = "    ") -> str:
    """Indent text."""
    return "\n".join(prefix + line for line in text.split("\n"))


def main():
    docs_path = Path(__file__).parent.parent / "docs" / "getting-started.md"

    if not docs_path.exists():
        print(f"ERROR: {docs_path} not found")
        sys.exit(1)

    content = docs_path.read_text()
    blocks = extract_code_blocks(content)

    print(f"Found {len(blocks)} code blocks in getting-started.md\n")

    passed = 0
    failed = 0
    skipped = 0

    for line_num, code in blocks:
        success, message = test_code_block(code, line_num)
        print(message)

        if "SKIP" in message:
            skipped += 1
        elif success:
            passed += 1
        else:
            failed += 1

    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

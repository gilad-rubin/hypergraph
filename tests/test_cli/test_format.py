"""Tests for CLI formatting utilities."""

from hypergraph.cli._format import (
    describe_value,
    format_duration,
    format_status,
    json_envelope,
    print_table,
    truncate_value,
)


class TestFormatDuration:
    def test_none(self):
        assert format_duration(None) == "—"

    def test_zero(self):
        assert format_duration(0) == "—"

    def test_milliseconds(self):
        assert format_duration(42) == "42ms"
        assert format_duration(999) == "999ms"

    def test_seconds(self):
        assert format_duration(1500) == "1.5s"
        assert format_duration(59999) == "60.0s"

    def test_minutes(self):
        assert format_duration(130000) == "2m10.0s"


class TestFormatStatus:
    def test_failed_uppercase(self):
        assert format_status("failed") == "FAILED"

    def test_completed_lowercase(self):
        assert format_status("completed") == "completed"

    def test_active_lowercase(self):
        assert format_status("active") == "active"


class TestDescribeValue:
    def test_none(self):
        assert describe_value(None) == ("—", "—")

    def test_list(self):
        assert describe_value([1, 2, 3]) == ("list", "3 items")

    def test_dict(self):
        assert describe_value({"a": 1}) == ("dict", "1 keys")

    def test_str(self):
        t, s = describe_value("hello")
        assert t == "str"
        assert "5B" in s

    def test_int(self):
        assert describe_value(42) == ("int", "42")


class TestTruncateValue:
    def test_short_value(self):
        assert truncate_value("hello") == "hello"

    def test_long_value(self):
        long = "x" * 300
        result = truncate_value(long, max_chars=50)
        assert len(result) == 51  # 50 chars + "…"
        assert result.endswith("…")


class TestJsonEnvelope:
    def test_structure(self):
        env = json_envelope("test.cmd", {"key": "value"})
        assert env["schema_version"] == 2
        assert env["command"] == "test.cmd"
        assert "generated_at" in env
        assert env["data"] == {"key": "value"}


class TestPrintTable:
    def test_basic_table(self):
        headers = ["Name", "Status"]
        rows = [["wf-1", "completed"], ["wf-2", "active"]]
        lines = print_table(headers, rows)
        assert len(lines) == 4  # header + separator + 2 rows
        assert "Name" in lines[0]
        assert "───" in lines[1]
        assert "wf-1" in lines[2]

    def test_empty_rows(self):
        lines = print_table(["A", "B"], [])
        assert lines == []

"""Tests for checkpointer serializers."""

import pytest

from hypergraph.checkpointers import JsonSerializer, PickleSerializer


class TestJsonSerializer:
    def test_roundtrip_dict(self):
        s = JsonSerializer()
        data = {"key": "value", "nested": [1, 2, 3]}
        assert s.deserialize(s.serialize(data)) == data

    def test_roundtrip_none(self):
        s = JsonSerializer()
        assert s.deserialize(s.serialize(None)) is None

    def test_non_serializable_raises_by_default(self):
        """Non-JSON types raise TypeError by default (strict mode)."""
        s = JsonSerializer()
        from datetime import datetime, timezone

        data = {"ts": datetime(2024, 1, 1, tzinfo=timezone.utc)}
        with pytest.raises(TypeError):
            s.serialize(data)

    def test_bytes_round_trip_as_bytes(self):
        """A step may return bytes (a blob column, a source file): JSON has none, so they are written as one tagged
        object and read back as the same bytes, at any depth."""
        s = JsonSerializer()
        data = {"pdf": b"%PDF-1.7\x00\xff", "pages": [bytearray(b"\x89PNG"), {"raw": memoryview(b"ab")}]}
        assert s.deserialize(s.serialize(data)) == {"pdf": b"%PDF-1.7\x00\xff", "pages": [b"\x89PNG", {"raw": b"ab"}]}

    def test_a_dict_that_only_resembles_the_bytes_form_stays_a_dict(self):
        s = JsonSerializer()
        data = {"__hypergraph_bytes__": "aGk=", "other": 1}
        assert s.deserialize(s.serialize(data)) == data

    def test_lossy_mode_uses_str(self):
        """With lossy=True, non-JSON types fall back to str()."""
        s = JsonSerializer(lossy=True)
        from datetime import datetime, timezone

        data = {"ts": datetime(2024, 1, 1, tzinfo=timezone.utc)}
        result = s.deserialize(s.serialize(data))
        assert isinstance(result["ts"], str)


class TestPickleSerializer:
    def test_requires_explicit_opt_in(self):
        with pytest.raises(ValueError, match="allow_pickle=True"):
            PickleSerializer()

    def test_roundtrip(self):
        s = PickleSerializer(allow_pickle=True)
        data = {"key": [1, 2, 3], "set": {4, 5}}
        result = s.deserialize(s.serialize(data))
        assert result == data

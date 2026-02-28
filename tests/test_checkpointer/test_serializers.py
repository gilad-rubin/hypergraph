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

    def test_non_serializable_uses_str(self):
        """Non-JSON types fall back to str() via default=str."""
        s = JsonSerializer()
        # datetime would normally fail JSON serialization
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

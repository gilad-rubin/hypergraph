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


class TestBlobSerializer:
    def _serializer(self, tmp_path):
        from hypergraph.checkpointers import BlobSerializer, FileBlobStore

        return BlobSerializer(FileBlobStore(tmp_path / "blobs"))

    def test_bytes_roundtrip_and_leave_the_json(self, tmp_path):
        s = self._serializer(tmp_path)
        data = {"pdf": b"%PDF-1.4\x00\x01", "n": 1, "nested": [b"x", {"y": b"y"}]}
        stored = s.serialize(data)
        assert b"PDF" not in stored and b"$bytes" in stored
        assert s.deserialize(stored) == data

    def test_same_bytes_stored_once_by_content(self, tmp_path):
        s = self._serializer(tmp_path)
        s.serialize({"a": b"same", "b": b"same"})
        files = [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
        assert len(files) == 1

    def test_bytes_inside_models_and_dataclasses(self, tmp_path):
        from dataclasses import dataclass

        from pydantic import BaseModel

        class Doc(BaseModel):
            name: str
            data: bytes

        @dataclass(frozen=True)
        class Pair:
            left: bytes
            right: int

        s = self._serializer(tmp_path)
        back = s.deserialize(s.serialize({"doc": Doc(name="a", data=b"\xff\x00"), "pair": Pair(b"\x01", 2)}))
        assert back == {"doc": {"name": "a", "data": b"\xff\x00"}, "pair": {"left": b"\x01", "right": 2}}

    def test_plain_values_serialize_like_json(self, tmp_path):
        from hypergraph.checkpointers import JsonSerializer

        s = self._serializer(tmp_path)
        data = {"key": "value", "nested": [1, 2, 3], "none": None}
        assert s.serialize(data) == JsonSerializer().serialize(data)

    def test_missing_blob_is_a_clear_error(self, tmp_path):
        s = self._serializer(tmp_path)
        with pytest.raises(KeyError, match="no blob"):
            s.deserialize(b'{"pdf": {"$bytes": "' + b"0" * 64 + b'"}}')

    def test_reference_shape_is_not_mistaken_for_user_data(self, tmp_path):
        s = self._serializer(tmp_path)
        data = {"$bytes": 5, "other": "x"}
        assert s.deserialize(s.serialize(data)) == data

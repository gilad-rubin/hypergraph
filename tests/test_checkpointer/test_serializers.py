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
        for data in ({"$bytes": 5, "other": "x"}, {"$bytes": "a" * 64}, {"$$bytes": "x"}, {"$literal": {"$bytes": "y"}}):
            assert s.deserialize(s.serialize(data)) == data

    def test_models_serialize_in_json_mode_like_json_serializer(self, tmp_path):
        from datetime import datetime, timezone
        from uuid import UUID

        from pydantic import BaseModel

        from hypergraph.checkpointers import JsonSerializer

        class Event(BaseModel):
            at: datetime
            id: UUID
            tags: list[str]

        e = Event(at=datetime(2024, 1, 1, tzinfo=timezone.utc), id=UUID(int=7), tags=["a"])
        s = self._serializer(tmp_path)
        assert s.serialize({"e": e}) == JsonSerializer().serialize({"e": e})

    def test_dataclass_class_vars_and_init_vars_are_not_data(self, tmp_path):
        from dataclasses import InitVar, dataclass, field
        from typing import ClassVar

        @dataclass
        class Row:
            kind: ClassVar[str] = "row"
            seed: InitVar[int] = 0
            name: str = ""
            blob: bytes = b""
            extra: list[int] = field(default_factory=list)

            def __post_init__(self, seed: int) -> None:
                self.extra = [seed]

        s = self._serializer(tmp_path)
        assert s.deserialize(s.serialize(Row(seed=3, name="r", blob=b"\x00"))) == {"name": "r", "blob": b"\x00", "extra": [3]}

    def test_put_syncs_the_blob_before_naming_it(self, tmp_path, monkeypatch):
        import os

        from hypergraph.checkpointers import FileBlobStore

        events: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            events.append("fsync")
            real_fsync(fd)

        def replace(src, dst):
            events.append("replace")
            real_replace(src, dst)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", replace)
        FileBlobStore(tmp_path).put(b"pdf")
        assert events[:2] == ["fsync", "replace"]  # the bytes, then the name
        assert events.count("fsync") >= 2  # ...then the folder entry that holds the name

    def test_corrupt_blob_is_refused(self, tmp_path):
        from hypergraph.checkpointers import BlobCorruptError

        s = self._serializer(tmp_path)
        stored = s.serialize({"pdf": b"original"})
        blob = next(p for p in (tmp_path / "blobs").rglob("*") if p.is_file())
        blob.write_bytes(b"tampered")
        with pytest.raises(BlobCorruptError):
            s.deserialize(stored)
        # put() heals a corrupt file rather than trusting it
        s.serialize({"pdf": b"original"})
        assert blob.read_bytes() == b"original"

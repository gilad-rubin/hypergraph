"""Serializers for checkpointer value storage."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class Serializer(ABC):
    """Base class for value serialization.

    Checkpointers use serializers to convert node output values
    to bytes for storage and back.
    """

    @abstractmethod
    def serialize(self, value: Any) -> bytes:
        """Convert value to bytes for storage."""
        ...

    @abstractmethod
    def deserialize(self, data: bytes) -> Any:
        """Convert bytes back to value."""
        ...


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class JsonSerializer(Serializer):
    """JSON serializer (default). Safe, human-readable, inspectable.

    By default, handles Pydantic models and dataclasses automatically.
    Pass ``lossy=True`` to also fall back to ``str()`` for other unsupported types.
    """

    def __init__(self, *, lossy: bool = False):
        self._lossy = lossy

    def _default(self, obj: Any) -> Any:
        try:
            return _json_default(obj)
        except TypeError:
            if self._lossy:
                return str(obj)
            raise

    def serialize(self, value: Any) -> bytes:
        return json.dumps(value, default=self._default).encode("utf-8")

    def deserialize(self, data: bytes) -> Any:
        return json.loads(data.decode("utf-8"))


class PickleSerializer(Serializer):
    """Pickle serializer for complex Python objects.

    WARNING: Pickle can execute arbitrary code on deserialization.
    Requires explicit ``allow_pickle=True`` to construct.
    """

    def __init__(self, *, allow_pickle: bool = False):
        if not allow_pickle:
            raise ValueError(
                "PickleSerializer requires explicit allow_pickle=True. "
                "Pickle can execute arbitrary code on deserialization. "
                "Only use with trusted data sources."
            )

    def serialize(self, value: Any) -> bytes:
        import pickle

        return pickle.dumps(value)

    def deserialize(self, data: bytes) -> Any:
        import pickle

        return pickle.loads(data)  # noqa: S301


@runtime_checkable
class BlobStore(Protocol):
    """Where a ``BlobSerializer`` keeps ``bytes`` values: content-addressed, write-once.

    ``put`` stores the bytes and returns their reference (the hex SHA-256 of the content); storing the same bytes twice
    returns the same reference. ``get`` returns the bytes for a reference or raises ``KeyError``.
    """

    def put(self, data: bytes) -> str: ...

    def get(self, ref: str) -> bytes: ...


class FileBlobStore:
    """A ``BlobStore`` over one folder: ``<root>/<first two hex>/<sha256>``, written atomically, never overwritten."""

    def __init__(self, root: str | Path):
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, ref: str) -> Path:
        if len(ref) != 64 or any(c not in "0123456789abcdef" for c in ref):
            raise KeyError(f"not a blob reference: {ref!r}")
        return self._root / ref[:2] / ref

    def put(self, data: bytes) -> str:
        ref = hashlib.sha256(data).hexdigest()
        path = self._path(ref)
        if path.exists():
            return ref
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return ref

    def get(self, ref: str) -> bytes:
        path = self._path(ref)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise KeyError(f"no blob {ref!r} under {self._root}") from None


_BYTES_KEY = "$bytes"


class BlobSerializer(Serializer):
    """JSON, with every ``bytes`` value kept in a ``BlobStore`` and a ``{"$bytes": "<sha256>"}`` reference in its place.

    Lets a node take or return raw bytes (a PDF, an image) across a checkpointed boundary without the bytes living in
    the checkpoint row: the store holds them once, by content, and the JSON stays small and inspectable. Everything
    else serializes exactly as ``JsonSerializer`` does (Pydantic models and dataclasses included; ``lossy=True`` falls
    back to ``str()`` for the rest). Bytes inside a model's fields are found too: models are walked before encoding.
    """

    def __init__(self, store: BlobStore, *, lossy: bool = False):
        self._store = store
        self._json = JsonSerializer(lossy=lossy)

    @property
    def store(self) -> BlobStore:
        return self._store

    def _stash(self, value: Any) -> Any:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {_BYTES_KEY: self._store.put(bytes(value))}
        if isinstance(value, Mapping):
            return {k: self._stash(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._stash(v) for v in value]
        if hasattr(value, "model_dump"):
            return self._stash(value.model_dump(mode="python"))
        if hasattr(value, "__dataclass_fields__") and not isinstance(value, type):
            return self._stash({f: getattr(value, f) for f in value.__dataclass_fields__})
        return value

    def _fetch(self, obj: dict[str, Any]) -> Any:
        if len(obj) == 1 and _BYTES_KEY in obj and isinstance(obj[_BYTES_KEY], str):
            return self._store.get(obj[_BYTES_KEY])
        return obj

    def serialize(self, value: Any) -> bytes:
        return self._json.serialize(self._stash(value))

    def deserialize(self, data: bytes) -> Any:
        return json.loads(data.decode("utf-8"), object_hook=self._fetch)

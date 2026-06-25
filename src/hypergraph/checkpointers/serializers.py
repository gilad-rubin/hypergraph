"""Serializers for checkpointer value storage."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any


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

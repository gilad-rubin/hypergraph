"""Base classes for all node types."""

from __future__ import annotations

import copy
from abc import ABC
from typing import TypeVar

from hypergraph.nodes._rename import RenameEntry, RenameError

# TypeVar for self-referential return types (Python 3.10 compatible)
_T = TypeVar("_T", bound="HyperNode")


class HyperNode(ABC):
    """Abstract base class for all node types with shared rename functionality.

    Defines the minimal interface that all nodes share:
    - name: Public node name
    - inputs: Input parameter names
    - outputs: Output value names
    - _rename_history: Tracks renames for error messages

    All with_* methods return new instances (immutable pattern).

    Subclasses must set these attributes in __init__:
    - name: str
    - inputs: tuple[str, ...]
    - outputs: tuple[str, ...]
    - _rename_history: list[RenameEntry]  (typically starts as [])
    """

    # Type annotations for IDE support (set by subclass __init__)
    name: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    _rename_history: list[RenameEntry]

    def __new__(cls, *args, **kwargs):
        """Prevent direct instantiation of HyperNode."""
        if cls is HyperNode:
            raise TypeError("HyperNode cannot be instantiated directly")
        return super().__new__(cls)

    # === Public API ===

    def with_name(self: _T, name: str) -> _T:
        """Return new node with different name.

        Args:
            name: New node name

        Returns:
            New node instance with updated name
        """
        return self._with_renamed("name", {self.name: name})

    def with_inputs(
        self: _T,
        mapping: dict[str, str] | None = None,
        /,
        **kwargs: str,
    ) -> _T:
        """Return new node with renamed inputs.

        Args:
            mapping: Optional dict {old_name: new_name}
            **kwargs: Additional renames as keyword args

        Returns:
            New node instance with updated inputs

        Raises:
            RenameError: If any old name not found in current inputs

        Note:
            The `/` makes mapping positional-only, allowing kwargs
            like `mapping="foo"` if your node has an input named "mapping".
        """
        combined = {**(mapping or {}), **kwargs}
        if not combined:
            return self._copy()
        return self._with_renamed("inputs", combined)

    def with_outputs(
        self: _T,
        mapping: dict[str, str] | None = None,
        /,
        **kwargs: str,
    ) -> _T:
        """Return new node with renamed outputs.

        Args:
            mapping: Optional dict {old_name: new_name}
            **kwargs: Additional renames as keyword args

        Returns:
            New node instance with updated outputs

        Raises:
            RenameError: If any old name not found in current outputs
        """
        combined = {**(mapping or {}), **kwargs}
        if not combined:
            return self._copy()
        return self._with_renamed("outputs", combined)

    # === Internal Helpers ===

    def _copy(self: _T) -> _T:
        """Create shallow copy with independent history list.

        Only _rename_history needs deep copy (mutable list).
        All other attributes are immutable (str, tuple, bool).
        """
        clone = copy.copy(self)
        clone._rename_history = list(self._rename_history)
        return clone

    def _with_renamed(self: _T, attr: str, mapping: dict[str, str]) -> _T:
        """Rename entries in an attribute (name, inputs, or outputs).

        Args:
            attr: Attribute name to modify
            mapping: {old: new} rename mapping

        Returns:
            New node with renamed attribute

        Raises:
            RenameError: If old name not found in current attribute value
        """
        clone = self._copy()
        current = getattr(clone, attr)

        if isinstance(current, str):
            # Single value (name)
            old, new = current, mapping.get(current, current)
            if old != new:
                clone._rename_history.append(RenameEntry(attr, old, new))  # type: ignore[arg-type]
                setattr(clone, attr, new)
        else:
            # Tuple (inputs/outputs)
            for old, new in mapping.items():
                if old not in current:
                    raise clone._make_rename_error(old, attr)
                clone._rename_history.append(RenameEntry(attr, old, new))  # type: ignore[arg-type]
            setattr(clone, attr, tuple(mapping.get(v, v) for v in current))

        return clone

    def _make_rename_error(self, name: str, attr: str) -> RenameError:
        """Build helpful error message using history.

        Checks if `name` was previously renamed and includes that
        context in the error message.
        """
        current = getattr(self, attr)
        for entry in self._rename_history:
            if entry.kind == attr and entry.old == name:
                return RenameError(
                    f"'{name}' was renamed to '{entry.new}'. "
                    f"Current {attr}: {current}"
                )
        return RenameError(f"'{name}' not found. Current {attr}: {current}")

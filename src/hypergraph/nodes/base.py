"""Base classes for all node types."""

from __future__ import annotations

import copy
import hashlib
from abc import ABC
from typing import Any, TypeVar

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

    Universal capabilities (with sensible defaults):
    - definition_hash: Structural hash for caching/change detection
    - is_async: Whether async execution is required (default: False)
    - is_generator: Whether node yields multiple values (default: False)
    - cache: Whether results should be cached (default: False)
    - has_default_for(param): Check if input has a fallback value
    - get_default_for(param): Get fallback value for input
    - get_input_type(param): Get expected type for input
    - get_output_type(output): Get type of output

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

    # === Universal Capabilities ===

    @property
    def definition_hash(self) -> str:
        """Structural hash for caching/change detection.

        Default implementation hashes class name + name + inputs + outputs.
        Subclasses may override for specialized hashing (e.g., function source).
        """
        content = f"{self.__class__.__name__}:{self.name}:{self.inputs}:{self.outputs}"
        return hashlib.sha256(content.encode()).hexdigest()

    @property
    def is_async(self) -> bool:
        """Does this node require async execution?

        Default: False. Override in subclasses that support async.
        """
        return False

    @property
    def is_generator(self) -> bool:
        """Does this node yield multiple values?

        Default: False. Override in subclasses that support generators.
        """
        return False

    @property
    def cache(self) -> bool:
        """Should results be cached?

        Default: False. Override in subclasses that support caching.
        """
        return False

    def has_default_for(self, param: str) -> bool:
        """Does this node have a fallback value for this input?

        Args:
            param: Input parameter name

        Returns:
            True if a default exists, False otherwise.
            Default implementation returns False.
        """
        return False

    def get_default_for(self, param: str) -> Any:
        """Get fallback value for an input parameter.

        Args:
            param: Input parameter name

        Returns:
            The default value.

        Raises:
            KeyError: If no default exists for this parameter.
            Default implementation always raises KeyError.
        """
        raise KeyError(f"No default for '{param}'")

    def get_input_type(self, param: str) -> type | None:
        """Get expected type for an input parameter.

        Args:
            param: Input parameter name

        Returns:
            The type annotation, or None if untyped.
            Default implementation returns None.
        """
        return None

    def get_output_type(self, output: str) -> type | None:
        """Get type of an output value.

        Args:
            output: Output value name

        Returns:
            The type annotation, or None if untyped.
            Default implementation returns None.
        """
        return None

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
        context in the error message. Shows the full rename chain
        if multiple renames occurred (e.g., a→x→z).
        """
        current = getattr(self, attr)

        # Build the full rename chain for this name
        chain = self._get_rename_chain(name, attr)
        if chain:
            chain_str = "→".join(chain)
            return RenameError(
                f"'{name}' was renamed: {chain_str}. "
                f"Current {attr}: {current}"
            )
        return RenameError(f"'{name}' not found. Current {attr}: {current}")

    def _get_rename_chain(self, name: str, attr: str) -> list[str]:
        """Get the full rename chain starting from a name.

        For a->x->z, _get_rename_chain("a", "inputs") returns ["a", "x", "z"].
        Returns empty list if name was never renamed.
        """
        chain: list[str] = []
        current_name = name

        # Follow the rename chain forward
        for entry in self._rename_history:
            if entry.kind == attr and entry.old == current_name:
                if not chain:
                    chain.append(current_name)
                chain.append(entry.new)
                current_name = entry.new

        return chain

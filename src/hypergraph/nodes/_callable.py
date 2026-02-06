"""Mixin for nodes that wrap a Python callable with rename support."""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, get_type_hints

from hypergraph.nodes._rename import build_reverse_rename_map


def _build_forward_rename_map(rename_history: list) -> dict[str, str]:
    """Build a forward rename map: original_param -> current_name.

    Handles chained renames correctly:
    - Sequential calls: a->x then x->z → {a: z}
    - Parallel renames (same batch): x->y, y->z → {x: y, y: z}

    Args:
        rename_history: List of RenameEntry objects

    Returns:
        Dict mapping original names to their final current names
    """
    input_entries = [e for e in rename_history if e.kind == "inputs"]
    if not input_entries:
        return {}

    # Group entries by batch_id
    batches: dict[int | None, list] = {}
    for entry in input_entries:
        batches.setdefault(entry.batch_id, []).append(entry)

    rename_map: dict[str, str] = {}

    # Process batches in order (by first occurrence in history)
    for batch_id in dict.fromkeys(e.batch_id for e in input_entries):
        batch_entries = batches[batch_id]
        # For parallel renames (same batch), compute using map state BEFORE this batch
        batch_updates = {}
        for entry in batch_entries:
            # Find original: look for existing mapping where value == entry.old
            original = next(
                (k for k, v in rename_map.items() if v == entry.old),
                entry.old,
            )
            batch_updates[original] = entry.new
        # Apply all updates from this batch at once
        rename_map.update(batch_updates)

    return rename_map


class CallableMixin:
    """Mixin for nodes that wrap a callable with rename support.

    Provides shared implementations of:
    - defaults (cached_property)
    - parameter_annotations (cached_property)
    - definition_hash (property)
    - has_default_for / get_default_for
    - get_input_type
    - map_inputs_to_params

    Requires the host class to set:
    - func: Callable
    - _rename_history: list[RenameEntry]
    - _definition_hash: str
    - inputs: tuple[str, ...]
    """

    func: Callable
    _rename_history: list
    _definition_hash: str
    inputs: tuple[str, ...]

    @property
    def definition_hash(self) -> str:
        """SHA256 hash of function source (cached at creation)."""
        return self._definition_hash

    @functools.cached_property
    def defaults(self) -> dict[str, Any]:
        """Default values for input parameters (using current/renamed names).

        Returns dict mapping current input names to their default values.
        If inputs have been renamed, uses the renamed names as keys.
        """
        sig = inspect.signature(self.func)
        rename_map = _build_forward_rename_map(self._rename_history)

        return {
            rename_map.get(name, name): param.default
            for name, param in sig.parameters.items()
            if param.default is not inspect.Parameter.empty
        }

    @functools.cached_property
    def parameter_annotations(self) -> dict[str, Any]:
        """Type annotations for input parameters.

        Returns:
            dict mapping parameter names (using current/renamed input names) to their
            type annotations. Only includes parameters that have annotations.
            Returns empty dict if get_type_hints fails (e.g., forward references).
        """
        try:
            hints = get_type_hints(self.func)
        except Exception:
            return {}

        sig = inspect.signature(self.func)
        original_params = list(sig.parameters.keys())
        rename_map = _build_forward_rename_map(self._rename_history)

        result: dict[str, Any] = {}
        for orig_param in original_params:
            if orig_param in hints:
                final_name = rename_map.get(orig_param, orig_param)
                result[final_name] = hints[orig_param]

        return result

    def has_default_for(self, param: str) -> bool:
        """Check if this parameter has a default value."""
        return param in self.defaults

    def get_default_for(self, param: str) -> Any:
        """Get default value for a parameter.

        Raises:
            KeyError: If parameter has no default.
        """
        defaults = self.defaults
        if param not in defaults:
            raise KeyError(f"No default for '{param}'")
        return defaults[param]

    def get_input_type(self, param: str) -> type | None:
        """Get type annotation for an input parameter."""
        return self.parameter_annotations.get(param)

    def map_inputs_to_params(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Map renamed input names back to original function parameter names."""
        reverse_map = build_reverse_rename_map(self._rename_history, "inputs")
        if not reverse_map:
            return inputs
        return {reverse_map.get(key, key): value for key, value in inputs.items()}

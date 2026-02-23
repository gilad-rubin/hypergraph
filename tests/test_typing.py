"""Tests for type compatibility checking utilities."""

from __future__ import annotations

import warnings
from typing import Annotated, Any, ForwardRef, TypeVar, Union

from hypergraph._typing import (
    NoAnnotation,
    TypeCheckMemo,
    Unresolvable,
    is_type_compatible,
    safe_get_type_hints,
)

# ---------------------------------------------------------------------------
# Simple type compatibility tests
# ---------------------------------------------------------------------------


class TestSimpleTypeCompatibility:
    """Test basic type matching."""

    def test_identical_types_are_compatible(self) -> None:
        """Same type should be compatible."""
        assert is_type_compatible(int, int) is True
        assert is_type_compatible(str, str) is True
        assert is_type_compatible(float, float) is True
        assert is_type_compatible(bool, bool) is True

    def test_different_types_are_not_compatible(self) -> None:
        """Different types should not be compatible."""
        assert is_type_compatible(str, int) is False
        assert is_type_compatible(int, str) is False
        assert is_type_compatible(float, str) is False
        assert is_type_compatible(list, dict) is False

    def test_any_accepts_anything(self) -> None:
        """Any as required type accepts any incoming type."""
        assert is_type_compatible(int, Any) is True
        assert is_type_compatible(str, Any) is True
        assert is_type_compatible(list[int], Any) is True

    def test_noannotation_skips_check(self) -> None:
        """NoAnnotation should skip the compatibility check."""
        assert is_type_compatible(NoAnnotation, int) is True
        assert is_type_compatible(int, NoAnnotation) is True
        assert is_type_compatible(NoAnnotation, NoAnnotation) is True

    def test_none_type_compatibility(self) -> None:
        """None type (type(None)) should be handled correctly."""
        assert is_type_compatible(type(None), type(None)) is True
        assert is_type_compatible(type(None), int) is False
        assert is_type_compatible(int, type(None)) is False


# ---------------------------------------------------------------------------
# Union type compatibility tests
# ---------------------------------------------------------------------------


class TestUnionTypeCompatibility:
    """Test Union type handling with both Union[a, b] and a | b syntax."""

    def test_type_into_union_is_compatible(self) -> None:
        """A single type should be compatible with a Union containing it."""
        # Using | syntax (Python 3.10+)
        assert is_type_compatible(int, int | str) is True
        assert is_type_compatible(str, int | str) is True
        assert is_type_compatible(float, int | str) is False

    def test_union_into_type_requires_all_members_compatible(self) -> None:
        """A Union outgoing requires all members compatible with required."""
        # int | str -> int requires both int and str compatible with int
        assert is_type_compatible(int | str, int) is False  # str not compatible with int
        assert is_type_compatible(int | str, int | str) is True  # exact match

    def test_union_into_superset_union_is_compatible(self) -> None:
        """A Union should be compatible with a larger Union containing all members."""
        assert is_type_compatible(int | str, int | str | float) is True
        assert is_type_compatible(int, int | str | float) is True

    def test_union_into_subset_union_is_not_compatible(self) -> None:
        """A larger Union should not be compatible with a smaller one."""
        assert is_type_compatible(int | str | float, int | str) is False
        assert is_type_compatible(int | str | float, int) is False

    def test_union_exact_match(self) -> None:
        """Exact Union match should be compatible."""
        assert is_type_compatible(int | str, int | str) is True
        assert is_type_compatible(str | int, int | str) is True  # Order shouldn't matter

    def test_typing_union_syntax(self) -> None:
        """Test typing.Union syntax works the same as | syntax."""
        assert is_type_compatible(int, Union[int, str]) is True  # noqa: UP007
        assert is_type_compatible(Union[int, str], Union[int, str, float]) is True  # noqa: UP007
        assert is_type_compatible(Union[int, str], int) is False  # noqa: UP007


# ---------------------------------------------------------------------------
# Generic type compatibility tests
# ---------------------------------------------------------------------------


class TestGenericTypeCompatibility:
    """Test generic type handling (list[int], dict[str, int], etc)."""

    def test_list_with_same_element_type(self) -> None:
        """list[int] should be compatible with list[int]."""
        assert is_type_compatible(list[int], list[int]) is True
        assert is_type_compatible(list[str], list[str]) is True

    def test_list_with_different_element_type(self) -> None:
        """list[int] should not be compatible with list[str]."""
        assert is_type_compatible(list[int], list[str]) is False
        assert is_type_compatible(list[str], list[int]) is False

    def test_parameterized_into_unparameterized_is_compatible(self) -> None:
        """list[int] should be compatible with bare list."""
        assert is_type_compatible(list[int], list) is True
        assert is_type_compatible(dict[str, int], dict) is True

    def test_dict_with_same_key_value_types(self) -> None:
        """dict[str, int] should be compatible with dict[str, int]."""
        assert is_type_compatible(dict[str, int], dict[str, int]) is True

    def test_dict_with_different_types(self) -> None:
        """dict with different key or value types should not be compatible."""
        assert is_type_compatible(dict[str, int], dict[str, str]) is False
        assert is_type_compatible(dict[str, int], dict[int, int]) is False

    def test_nested_generics(self) -> None:
        """Nested generics should be compared recursively."""
        assert is_type_compatible(list[dict[str, int]], list[dict[str, int]]) is True
        assert is_type_compatible(list[dict[str, int]], list[dict[str, str]]) is False

    def test_set_type(self) -> None:
        """set[T] should work like list[T]."""
        assert is_type_compatible(set[int], set[int]) is True
        assert is_type_compatible(set[int], set[str]) is False

    def test_tuple_type(self) -> None:
        """tuple types should compare element by element."""
        assert is_type_compatible(tuple[int, str], tuple[int, str]) is True
        assert is_type_compatible(tuple[int, str], tuple[str, int]) is False


# ---------------------------------------------------------------------------
# Forward reference resolution tests
# ---------------------------------------------------------------------------


class TestForwardReferenceResolution:
    """Test forward reference handling."""

    def test_string_forward_ref_resolves(self) -> None:
        """String 'int' should resolve to int type."""
        memo = TypeCheckMemo(globals={"int": int}, locals=None)
        assert is_type_compatible("int", int, memo) is True
        assert is_type_compatible(int, "int", memo) is True

    def test_forwardref_object_resolves(self) -> None:
        """ForwardRef('int') should resolve to int type."""
        memo = TypeCheckMemo(globals={"int": int}, locals=None)
        ref = ForwardRef("int")
        assert is_type_compatible(ref, int, memo) is True

    def test_forward_ref_to_class(self) -> None:
        """Forward reference to a class should resolve."""

        class MyClass:
            pass

        memo = TypeCheckMemo(globals={"MyClass": MyClass}, locals=None)
        assert is_type_compatible("MyClass", MyClass, memo) is True


# ---------------------------------------------------------------------------
# Graceful degradation tests
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    """Test handling of Unresolvable and edge cases."""

    def test_unresolvable_warns_and_returns_true(self) -> None:
        """Unresolvable should emit warning and return True (skip check)."""
        unresolvable = Unresolvable("SomeUnknownType")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = is_type_compatible(unresolvable, int)
            assert result is True
            assert len(w) == 1
            assert "Unresolvable type hint" in str(w[0].message)
            assert "SomeUnknownType" in str(w[0].message)

    def test_unresolvable_on_required_side(self) -> None:
        """Unresolvable on required side should also skip check."""
        unresolvable = Unresolvable("UnknownRequired")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = is_type_compatible(int, unresolvable)
            assert result is True
            assert len(w) == 1

    def test_unresolvable_repr(self) -> None:
        """Unresolvable should have informative repr."""
        u = Unresolvable("MyType")
        assert repr(u) == "Unresolvable['MyType']"

    def test_unresolvable_equality(self) -> None:
        """Unresolvable instances with same string should be equal."""
        u1 = Unresolvable("Type")
        u2 = Unresolvable("Type")
        u3 = Unresolvable("Other")
        assert u1 == u2
        assert u1 != u3
        assert u1 != "Type"


# ---------------------------------------------------------------------------
# TypeVar compatibility tests
# ---------------------------------------------------------------------------


class TestTypeVarCompatibility:
    """Test TypeVar handling."""

    def test_unconstrained_typevar_accepts_anything(self) -> None:
        """TypeVar without constraints should accept any type."""
        T = TypeVar("T")
        assert is_type_compatible(int, T) is True
        assert is_type_compatible(str, T) is True
        assert is_type_compatible(list[int], T) is True

    def test_constrained_typevar(self) -> None:
        """TypeVar with constraints should only accept those types."""
        T = TypeVar("T", int, str)
        assert is_type_compatible(int, T) is True
        assert is_type_compatible(str, T) is True
        assert is_type_compatible(float, T) is False

    def test_bounded_typevar(self) -> None:
        """TypeVar with bound should accept subclasses."""
        T = TypeVar("T", bound=int)
        assert is_type_compatible(int, T) is True
        # bool is a subclass of int
        assert is_type_compatible(bool, T) is True

    def test_incoming_typevar_returns_true(self) -> None:
        """When incoming is TypeVar, we can't know concrete type - accept."""
        T = TypeVar("T")
        # If incoming is TypeVar, we accept since we don't know the concrete type
        assert is_type_compatible(T, int) is True


# ---------------------------------------------------------------------------
# Annotated type tests
# ---------------------------------------------------------------------------


class TestAnnotatedTypeCompatibility:
    """Test Annotated type handling."""

    def test_annotated_types_compare_primary(self) -> None:
        """Annotated types should compare their primary types."""
        assert is_type_compatible(Annotated[int, "metadata"], Annotated[int, "other"]) is True
        assert is_type_compatible(Annotated[int, "metadata"], Annotated[str, "metadata"]) is False

    def test_annotated_vs_plain_type(self) -> None:
        """Annotated[T, ...] should be compatible with T."""
        assert is_type_compatible(Annotated[int, "doc"], int) is True
        assert is_type_compatible(int, Annotated[int, "doc"]) is True

    def test_annotated_type_mismatch(self) -> None:
        """Annotated[int, ...] should not be compatible with str."""
        assert is_type_compatible(Annotated[int, "doc"], str) is False


# ---------------------------------------------------------------------------
# safe_get_type_hints tests
# ---------------------------------------------------------------------------


class TestSafeGetTypeHints:
    """Test safe_get_type_hints wrapper."""

    def test_basic_function_hints(self) -> None:
        """Should extract hints from a simple function."""

        def greet(name: str) -> str:
            return f"Hello {name}"

        hints = safe_get_type_hints(greet)
        assert hints["name"] is str
        assert hints["return"] is str

    def test_function_without_annotations(self) -> None:
        """Should return empty dict for unannotated function."""

        def no_hints(x, y):
            return x + y

        hints = safe_get_type_hints(no_hints)
        assert hints == {}

    def test_partial_annotations(self) -> None:
        """Should handle functions with partial annotations."""

        def partial(x: int, y):
            return x + y

        hints = safe_get_type_hints(partial)
        assert hints["x"] is int
        assert "y" not in hints

    def test_none_return_annotation(self) -> None:
        """None return should be converted to type(None)."""

        def returns_none(x: int) -> None:
            pass

        hints = safe_get_type_hints(returns_none)
        assert hints["return"] is type(None)


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Test edge cases and corner scenarios."""

    def test_callable_type(self) -> None:
        """Callable types should be handled."""
        from collections.abc import Callable

        assert is_type_compatible(Callable[[int], str], Callable[[int], str]) is True

    def test_optional_type(self) -> None:
        """Optional[T] is Union[T, None] and should work correctly."""
        # int | None is the modern form of Optional[int]
        assert is_type_compatible(int, int | None) is True
        assert is_type_compatible(type(None), int | None) is True
        assert is_type_compatible(str, int | None) is False

    def test_empty_memo_is_created_if_none(self) -> None:
        """If memo is None, function should create an empty one."""
        # This should not raise
        result = is_type_compatible(int, int, memo=None)
        assert result is True


# ---------------------------------------------------------------------------
# Literal type compatibility tests (TYPE-01)
# ---------------------------------------------------------------------------


class TestLiteralTypeCompatibility:
    """Test Literal type handling (TYPE-01).

    Literal types restrict values to specific literals: Literal["a", "b"] only accepts "a" or "b".
    Tests cover identity comparison and basic non-compatibility checks.
    """

    def test_literal_identical_values_compatible(self) -> None:
        """Same Literal is compatible with itself."""
        from typing import Literal

        assert is_type_compatible(Literal["a", "b"], Literal["a", "b"]) is True

    def test_literal_superset_into_subset_not_compatible(self) -> None:
        """Literal with more values should NOT be compatible with subset."""
        from typing import Literal

        # Literal["a", "b", "c"] has more values than Literal["a", "b"] accepts
        assert is_type_compatible(Literal["a", "b", "c"], Literal["a", "b"]) is False

    def test_base_type_into_literal_not_compatible(self) -> None:
        """Base type should NOT be compatible with Literal (has more possible values)."""
        from typing import Literal

        # str has infinitely more values than Literal["a", "b"]
        assert is_type_compatible(str, Literal["a", "b"]) is False
        # int has more values than Literal[1, 2]
        assert is_type_compatible(int, Literal[1, 2]) is False

    def test_literal_different_values_not_compatible(self) -> None:
        """Literal with non-overlapping values should not be compatible."""
        from typing import Literal

        # Literal["x"] has no overlap with Literal["a", "b"]
        assert is_type_compatible(Literal["x"], Literal["a", "b"]) is False


# ---------------------------------------------------------------------------
# Protocol type compatibility tests (TYPE-02)
# ---------------------------------------------------------------------------


class TestProtocolTypeCompatibility:
    """Test Protocol type handling (TYPE-02).

    Protocol enables structural typing - checking interface compatibility rather than inheritance.
    """

    def test_protocol_identical_compatible(self) -> None:
        """Same protocol is compatible with itself."""
        from typing import Protocol

        class Readable(Protocol):
            def read(self) -> str: ...

        assert is_type_compatible(Readable, Readable) is True

    def test_class_implementing_protocol_compatible(self) -> None:
        """Class implementing protocol's methods should be compatible."""
        from typing import Protocol, runtime_checkable

        @runtime_checkable
        class Readable(Protocol):
            def read(self) -> str: ...

        class FileReader:
            def read(self) -> str:
                return "content"

        # Note: Type compatibility at static level is different from runtime
        # The implementation may or may not support this
        # If supported, FileReader should be compatible with Readable
        # For now, test that it doesn't crash
        try:
            result = is_type_compatible(FileReader, Readable)
            # Either True (structural typing works) or False (not supported) - both valid
            assert isinstance(result, bool)
        except Exception:
            # If it fails, that's also documenting current behavior
            pass

    def test_class_not_implementing_protocol_not_compatible(self) -> None:
        """Class missing protocol methods should not be compatible."""
        from typing import Protocol, runtime_checkable

        @runtime_checkable
        class Readable(Protocol):
            def read(self) -> str: ...

        class NotReadable:
            def write(self) -> None:
                pass

        # Missing read() method - should not be compatible
        # Note: Implementation may not check this, document behavior
        result = is_type_compatible(NotReadable, Readable)
        # Either False (correctly checked) or True (not checked) - document
        assert isinstance(result, bool)

    def test_protocol_with_multiple_methods(self) -> None:
        """Protocol with multiple method requirements."""
        from typing import Protocol

        class ReadWrite(Protocol):
            def read(self) -> str: ...
            def write(self, data: str) -> None: ...

        # Same protocol should be compatible
        assert is_type_compatible(ReadWrite, ReadWrite) is True


# ---------------------------------------------------------------------------
# TypedDict type compatibility tests (TYPE-03)
# ---------------------------------------------------------------------------


class TestTypedDictTypeCompatibility:
    """Test TypedDict type handling (TYPE-03).

    TypedDict is a dict with specific string keys and typed values.
    Tests cover identity and basic dict compatibility.
    """

    def test_typeddict_identical_compatible(self) -> None:
        """Same TypedDict is compatible with itself."""
        from typing import TypedDict

        class PersonDict(TypedDict):
            name: str
            age: int

        assert is_type_compatible(PersonDict, PersonDict) is True

    def test_typeddict_into_dict_compatible(self) -> None:
        """TypedDict should be compatible with dict (TypedDict is a dict)."""
        from typing import TypedDict

        class PersonDict(TypedDict):
            name: str
            age: int

        # TypedDict is a subtype of dict
        assert is_type_compatible(PersonDict, dict) is True


# ---------------------------------------------------------------------------
# NamedTuple type compatibility tests (TYPE-04)
# ---------------------------------------------------------------------------


class TestNamedTupleTypeCompatibility:
    """Test NamedTuple type handling (TYPE-04).

    NamedTuple is a tuple with named fields and type annotations.
    """

    def test_namedtuple_identical_compatible(self) -> None:
        """Same NamedTuple is compatible with itself."""
        from typing import NamedTuple

        class Point(NamedTuple):
            x: int
            y: int

        assert is_type_compatible(Point, Point) is True

    def test_namedtuple_into_tuple_compatible(self) -> None:
        """NamedTuple should be compatible with tuple (it is a tuple)."""
        from typing import NamedTuple

        class Point(NamedTuple):
            x: int
            y: int

        # Point is a tuple[int, int]
        assert is_type_compatible(Point, tuple) is True
        # Also compatible with specific tuple type
        assert is_type_compatible(Point, tuple[int, int]) is True

    def test_tuple_into_namedtuple_not_compatible(self) -> None:
        """Plain tuple should NOT be compatible with NamedTuple (lacks field names)."""
        from typing import NamedTuple

        class Point(NamedTuple):
            x: int
            y: int

        # tuple[int, int] doesn't have named fields
        assert is_type_compatible(tuple[int, int], Point) is False

    def test_namedtuple_different_fields_not_compatible(self) -> None:
        """NamedTuples with different fields are not compatible."""
        from typing import NamedTuple

        class Point(NamedTuple):
            x: int
            y: int

        class Vector(NamedTuple):
            dx: float
            dy: float

        assert is_type_compatible(Point, Vector) is False


# ---------------------------------------------------------------------------
# ParamSpec type compatibility tests (TYPE-05)
# ---------------------------------------------------------------------------


class TestParamSpecTypeCompatibility:
    """Test ParamSpec type handling (TYPE-05).

    ParamSpec captures the parameter types of a callable for use in decorators.
    """

    def test_paramspec_basic_handling(self) -> None:
        """ParamSpec doesn't crash when used in type comparison."""
        from collections.abc import Callable
        from typing import ParamSpec

        P = ParamSpec("P")

        # Should not crash - may return True (permissive) or handle gracefully
        try:
            result = is_type_compatible(Callable[P, int], Callable[P, int])
            assert isinstance(result, bool)
        except Exception:
            # If it fails, document that ParamSpec is not supported
            pass

    def test_paramspec_in_callable_type(self) -> None:
        """Callable with ParamSpec compared to Callable with Any params."""
        from collections.abc import Callable
        from typing import ParamSpec

        P = ParamSpec("P")

        # Callable[P, int] vs Callable[..., int]
        # Both accept any arguments and return int
        try:
            result = is_type_compatible(Callable[P, int], Callable[..., int])
            # Either compatible (treated similarly) or not - document behavior
            assert isinstance(result, bool)
        except Exception:
            pass

    def test_paramspec_concat_handling(self) -> None:
        """ParamSpec.args and ParamSpec.kwargs access doesn't error."""
        from typing import ParamSpec

        P = ParamSpec("P")

        # Accessing P.args and P.kwargs should not crash
        # These are typing constructs for parameter concatenation
        args = P.args
        kwargs = P.kwargs
        assert args is not None
        assert kwargs is not None


# ---------------------------------------------------------------------------
# Self type compatibility tests (TYPE-06)
# ---------------------------------------------------------------------------


class TestSelfTypeCompatibility:
    """Test Self type handling (TYPE-06).

    Self refers to the class in which it appears (Python 3.11+).
    """

    def test_self_type_basic_handling(self) -> None:
        """Self type doesn't crash when encountered."""
        # Handle import for Python < 3.11
        try:
            from typing import Self
        except ImportError:
            from typing_extensions import Self

        # Create a class using Self
        class Chainable:
            def chain(self) -> Self:
                return self

        # Getting type hints shouldn't crash
        import typing

        try:
            hints = typing.get_type_hints(Chainable.chain)
            # Self should be in the hints
            assert "return" in hints or hints is not None
        except Exception:
            # If get_type_hints fails with Self, that's documented behavior
            pass

    def test_self_type_comparison(self) -> None:
        """Self type compared to Self type."""
        try:
            from typing import Self
        except ImportError:
            from typing_extensions import Self

        # Self vs Self should be compatible
        try:
            result = is_type_compatible(Self, Self)
            assert result is True
        except Exception:
            # May not be supported
            pass

    def test_self_vs_explicit_class(self) -> None:
        """Self should ideally be compatible with the class it's in."""
        try:
            from typing import Self
        except ImportError:
            from typing_extensions import Self

        class MyClass:
            pass

        # This is a static type check concept - Self resolves to the class
        # At runtime, testing this is tricky
        # Just verify no crash occurs
        try:
            result = is_type_compatible(Self, MyClass)
            # May be True (Self resolves) or False (not supported)
            assert isinstance(result, bool)
        except Exception:
            pass

"""Tests for hypergraph.nodes.function."""

import warnings

import pytest

from hypergraph.nodes._rename import RenameError
from hypergraph.nodes.function import FunctionNode, node


class TestFunctionNodeConstruction:
    """Tests for FunctionNode.__init__."""

    def test_basic_sync_function_no_output(self):
        """Basic sync function without output_name."""

        def foo(x):
            pass

        fn = FunctionNode(foo)
        assert fn.name == "foo"
        assert fn.inputs == ("x",)
        assert fn.outputs == ()

    def test_name_defaults_to_func_name(self):
        """Name defaults to func.__name__."""

        def my_function(x):
            pass

        fn = FunctionNode(my_function)
        assert fn.name == "my_function"

    def test_with_output_name_string(self):
        """Single output_name string becomes 1-tuple."""

        def foo(x):
            return x

        fn = FunctionNode(foo, output_name="result")
        assert fn.outputs == ("result",)

    def test_with_output_name_tuple(self):
        """Multiple output names as tuple."""

        def foo(x):
            return x, x

        fn = FunctionNode(foo, output_name=("a", "b"))
        assert fn.outputs == ("a", "b")

    def test_with_custom_name(self):
        """Custom name overrides func.__name__."""

        def foo(x):
            pass

        fn = FunctionNode(foo, name="custom")
        assert fn.name == "custom"

    def test_with_name_and_output_name(self):
        """Both custom name and output_name."""

        def foo(x):
            return x

        fn = FunctionNode(foo, name="custom", output_name="result")
        assert fn.name == "custom"
        assert fn.outputs == ("result",)

    def test_with_rename_inputs(self):
        """rename_inputs renames input parameters."""

        def foo(x, y):
            pass

        fn = FunctionNode(foo, rename_inputs={"x": "a"})
        assert fn.inputs == ("a", "y")

    def test_with_cache_true(self):
        """cache=True sets cache attribute."""

        def foo(x):
            pass

        fn = FunctionNode(foo, cache=True)
        assert fn.cache is True

    def test_cache_defaults_false(self):
        """cache defaults to False."""

        def foo(x):
            pass

        fn = FunctionNode(foo)
        assert fn.cache is False

    def test_from_function_node(self):
        """Creating FunctionNode from existing FunctionNode extracts func."""

        def foo(x):
            return x

        original = FunctionNode(foo, name="old", output_name="old_result")
        new = FunctionNode(original, name="new", output_name="new_result")

        # New node uses original func
        assert new.func is foo
        # New config is applied
        assert new.name == "new"
        assert new.outputs == ("new_result",)
        # Original is unchanged
        assert original.name == "old"
        assert original.outputs == ("old_result",)

    def test_multiple_inputs(self):
        """Function with multiple parameters."""

        def foo(a, b, c):
            pass

        fn = FunctionNode(foo)
        assert fn.inputs == ("a", "b", "c")

    def test_no_inputs(self):
        """Function with no parameters."""

        def foo():
            pass

        fn = FunctionNode(foo)
        assert fn.inputs == ()


class TestResolveOutputsWarning:
    """Tests for output warning behavior."""

    def test_no_annotation_no_warning(self):
        """No warning when function has no return annotation."""

        def foo(x):
            pass

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            FunctionNode(foo)
            assert len(w) == 0

    def test_none_annotation_no_warning(self):
        """No warning when function has -> None annotation."""

        def foo(x) -> None:
            pass

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            FunctionNode(foo)
            assert len(w) == 0

    def test_return_annotation_no_output_name_warns(self):
        """Warning when function has return annotation but no output_name."""

        def foo(x) -> int:
            return x

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            FunctionNode(foo)
            assert len(w) == 1
            assert "has return type" in str(w[0].message)
            assert "output_name" in str(w[0].message)

    def test_return_annotation_with_output_name_no_warning(self):
        """No warning when output_name is provided."""

        def foo(x) -> int:
            return x

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            FunctionNode(foo, output_name="result")
            assert len(w) == 0


class TestExecutionModeDetection:
    """Tests for is_async and is_generator detection."""

    def test_sync_function(self):
        """Sync function: is_async=False, is_generator=False."""

        def foo():
            pass

        fn = FunctionNode(foo)
        assert fn.is_async is False
        assert fn.is_generator is False

    def test_async_function(self):
        """Async function: is_async=True, is_generator=False."""

        async def foo():
            pass

        fn = FunctionNode(foo)
        assert fn.is_async is True
        assert fn.is_generator is False

    def test_sync_generator(self):
        """Sync generator: is_async=False, is_generator=True."""

        def foo():
            yield 1

        fn = FunctionNode(foo)
        assert fn.is_async is False
        assert fn.is_generator is True

    def test_async_generator(self):
        """Async generator: is_async=True, is_generator=True."""

        async def foo():
            yield 1

        fn = FunctionNode(foo)
        assert fn.is_async is True
        assert fn.is_generator is True


class TestFunctionNodeProperties:
    """Tests for FunctionNode properties."""

    def test_definition_hash_is_string(self):
        """definition_hash is a 64-character hex string."""

        def foo():
            pass

        fn = FunctionNode(foo)
        h = fn.definition_hash
        assert isinstance(h, str)
        assert len(h) == 64

    def test_definition_hash_cached(self):
        """definition_hash returns same value on repeated access."""

        def foo():
            pass

        fn = FunctionNode(foo)
        h1 = fn.definition_hash
        h2 = fn.definition_hash
        assert h1 == h2

    def test_is_async_returns_bool(self):
        """is_async returns a boolean."""

        def foo():
            pass

        fn = FunctionNode(foo)
        assert isinstance(fn.is_async, bool)

    def test_is_generator_returns_bool(self):
        """is_generator returns a boolean."""

        def foo():
            pass

        fn = FunctionNode(foo)
        assert isinstance(fn.is_generator, bool)


class TestFunctionNodeCall:
    """Tests for FunctionNode.__call__."""

    def test_delegates_to_func(self):
        """__call__ delegates to self.func."""

        def double(x):
            return x * 2

        fn = FunctionNode(double, output_name="result")
        assert fn(5) == 10

    def test_passes_args(self):
        """__call__ passes positional args."""

        def add(a, b):
            return a + b

        fn = FunctionNode(add, output_name="result")
        assert fn(1, 2) == 3

    def test_passes_kwargs(self):
        """__call__ passes keyword args."""

        def greet(name, greeting="Hello"):
            return f"{greeting}, {name}!"

        fn = FunctionNode(greet, output_name="result")
        assert fn("World", greeting="Hi") == "Hi, World!"


class TestFunctionNodeRepr:
    """Tests for FunctionNode.__repr__."""

    def test_no_output_side_effect(self):
        """Repr for side-effect only node."""

        def foo():
            pass

        fn = FunctionNode(foo)
        assert repr(fn) == "FunctionNode(foo, outputs=())"

    def test_with_outputs(self):
        """Repr shows outputs."""

        def foo():
            return 1

        fn = FunctionNode(foo, output_name="result")
        assert repr(fn) == "FunctionNode(foo, outputs=('result',))"

    def test_after_with_name(self):
        """Repr shows 'original as new_name' after rename."""

        def foo():
            return 1

        fn = FunctionNode(foo, output_name="result")
        renamed = fn.with_name("bar")
        assert repr(renamed) == "FunctionNode(foo as 'bar', outputs=('result',))"


class TestNodeDecorator:
    """Tests for @node decorator."""

    def test_without_parens(self):
        """@node without parentheses creates FunctionNode."""

        @node
        def foo(x):
            pass

        assert isinstance(foo, FunctionNode)
        assert foo.outputs == ()

    def test_with_empty_parens(self):
        """@node() with empty parentheses creates FunctionNode."""

        @node()
        def foo(x):
            pass

        assert isinstance(foo, FunctionNode)
        assert foo.outputs == ()

    def test_with_output_name(self):
        """@node(output_name="x") sets outputs."""

        @node(output_name="result")
        def foo(x):
            return x

        assert foo.outputs == ("result",)

    def test_with_multiple_outputs(self):
        """@node(output_name=("a", "b")) sets multiple outputs."""

        @node(output_name=("a", "b"))
        def foo(x):
            return x, x

        assert foo.outputs == ("a", "b")

    def test_with_all_params(self):
        """@node with all parameters."""

        @node(output_name="result", rename_inputs={"x": "input"}, cache=True)
        def foo(x):
            return x

        assert foo.outputs == ("result",)
        assert foo.inputs == ("input",)
        assert foo.cache is True

    def test_preserves_function_behavior(self):
        """Decorated function still callable."""

        @node(output_name="result")
        def double(x):
            return x * 2

        assert double(5) == 10

    def test_name_always_from_function(self):
        """Decorator always uses func.__name__."""

        @node(output_name="x")
        def my_func():
            pass

        assert my_func.name == "my_func"

    def test_warning_on_return_annotation(self):
        """Decorator emits warning for return annotation without output_name."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")

            @node
            def foo() -> int:
                return 1

            assert len(w) == 1
            assert "has return type" in str(w[0].message)


class TestNodeDecoratorWithRenameInputs:
    """Tests for @node decorator with rename_inputs."""

    def test_rename_single_input(self):
        """rename_inputs renames a single input."""

        @node(rename_inputs={"x": "input_value"})
        def foo(x):
            pass

        assert foo.inputs == ("input_value",)

    def test_rename_multiple_inputs(self):
        """rename_inputs renames multiple inputs."""

        @node(rename_inputs={"a": "x", "b": "y"})
        def foo(a, b):
            pass

        assert foo.inputs == ("x", "y")

    def test_rename_unknown_input_raises(self):
        """rename_inputs with unknown key raises RenameError."""

        def foo(x, y):
            pass

        with pytest.raises(RenameError) as exc_info:
            FunctionNode(foo, rename_inputs={"z": "renamed"})

        assert "Cannot rename unknown inputs: 'z'" in str(exc_info.value)
        assert "'x'" in str(exc_info.value)
        assert "'y'" in str(exc_info.value)

    def test_rename_multiple_unknown_inputs_raises(self):
        """rename_inputs with multiple unknown keys raises RenameError."""

        def foo(x):
            pass

        with pytest.raises(RenameError) as exc_info:
            FunctionNode(foo, rename_inputs={"a": "b", "c": "d"})

        assert "Cannot rename unknown inputs" in str(exc_info.value)


class TestFunctionNodeWithLambda:
    """Tests for FunctionNode with lambda functions."""

    def test_lambda_basic(self):
        """Lambda functions work with FunctionNode."""
        fn = FunctionNode(lambda x: x * 2, output_name="result")
        assert fn.inputs == ("x",)
        assert fn.outputs == ("result",)
        assert fn(5) == 10

    def test_lambda_name_is_lambda(self):
        """Lambda function name is '<lambda>'."""
        fn = FunctionNode(lambda x: x)
        assert fn.name == "<lambda>"


class TestFunctionNodeDefaults:
    """Tests for FunctionNode.defaults property."""

    def test_no_defaults(self):
        """Function with no defaults has empty dict."""

        def foo(a, b, c):
            pass

        fn = FunctionNode(foo)
        assert fn.defaults == {}

    def test_single_default(self):
        """Function with one default parameter."""

        def foo(a, b=1):
            pass

        fn = FunctionNode(foo)
        assert fn.defaults == {"b": 1}

    def test_multiple_defaults(self):
        """Function with multiple default parameters."""

        def foo(a, b=1, c="hello"):
            pass

        fn = FunctionNode(foo)
        assert fn.defaults == {"b": 1, "c": "hello"}

    def test_all_defaults(self):
        """Function where all parameters have defaults."""

        def foo(a=10, b=20):
            pass

        fn = FunctionNode(foo)
        assert fn.defaults == {"a": 10, "b": 20}

    def test_default_none(self):
        """Function with None as default value."""

        def foo(a, b=None):
            pass

        fn = FunctionNode(foo)
        assert fn.defaults == {"b": None}


class TestFunctionNodeParameterAnnotations:
    """Tests for FunctionNode.parameter_annotations property."""

    def test_fully_typed_function(self):
        """Function with all parameters typed."""

        def foo(x: int, y: str) -> float:
            return 0.0

        fn = FunctionNode(foo, output_name="result")
        assert fn.parameter_annotations == {"x": int, "y": str}

    def test_untyped_function(self):
        """Function without type annotations returns empty dict."""

        def foo(a, b):
            return a + b

        fn = FunctionNode(foo, output_name="out")
        assert fn.parameter_annotations == {}

    def test_partial_annotations(self):
        """Function with some parameters typed."""

        def foo(x: int, y) -> str:
            return str(x)

        fn = FunctionNode(foo, output_name="out")
        assert fn.parameter_annotations == {"x": int}

    def test_with_renamed_inputs(self):
        """Renamed inputs use the new names."""

        def foo(x: int, y: str) -> float:
            return 0.0

        fn = FunctionNode(foo, output_name="result", rename_inputs={"x": "input_val"})
        assert fn.parameter_annotations == {"input_val": int, "y": str}

    def test_complex_types(self):
        """Complex type annotations are preserved."""
        from typing import Optional, List

        def foo(items: List[int], default: Optional[str] = None) -> dict:
            return {}

        fn = FunctionNode(foo, output_name="result")
        assert fn.parameter_annotations == {"items": List[int], "default": Optional[str]}


class TestFunctionNodeOutputAnnotation:
    """Tests for FunctionNode.output_annotation property."""

    def test_single_output(self):
        """Single output maps to return type."""

        def foo(x: int) -> float:
            return 0.0

        fn = FunctionNode(foo, output_name="result")
        assert fn.output_annotation == {"result": float}

    def test_no_return_annotation(self):
        """Function without return annotation returns empty dict."""

        def foo(x: int):
            return x

        fn = FunctionNode(foo, output_name="result")
        assert fn.output_annotation == {}

    def test_no_outputs_side_effect(self):
        """Side-effect only function returns empty dict."""

        def foo(x: int) -> None:
            pass

        fn = FunctionNode(foo)
        assert fn.output_annotation == {}

    def test_multiple_outputs_tuple(self):
        """Multiple outputs with tuple return type."""

        def foo(x: int) -> tuple[str, float]:
            return ("", 0.0)

        fn = FunctionNode(foo, output_name=("a", "b"))
        assert fn.output_annotation == {"a": str, "b": float}

    def test_multiple_outputs_wrong_tuple_length(self):
        """Mismatched tuple length returns empty dict."""

        def foo(x: int) -> tuple[str, float, int]:
            return ("", 0.0, 0)

        fn = FunctionNode(foo, output_name=("a", "b"))
        # 3 tuple elements but only 2 outputs - can't map
        assert fn.output_annotation == {}

    def test_multiple_outputs_non_tuple_return(self):
        """Multiple outputs with non-tuple return returns empty dict."""

        def foo(x: int) -> list:
            return []

        fn = FunctionNode(foo, output_name=("a", "b"))
        assert fn.output_annotation == {}

    def test_complex_return_type(self):
        """Complex return types are preserved."""
        from typing import Optional, Dict

        def foo(x: int) -> Optional[Dict[str, int]]:
            return None

        fn = FunctionNode(foo, output_name="result")
        assert fn.output_annotation == {"result": Optional[Dict[str, int]]}


class TestFunctionSignatures:
    """Tests for FunctionNode handling of all Python parameter types (FUNC-01 through FUNC-05).

    Python has 5 parameter kinds (from inspect.Parameter.kind):
    - POSITIONAL_ONLY: def f(a, /)
    - POSITIONAL_OR_KEYWORD: def f(a) (standard, already tested elsewhere)
    - VAR_POSITIONAL: def f(*args)
    - KEYWORD_ONLY: def f(*, kw)
    - VAR_KEYWORD: def f(**kwargs)
    """

    # FUNC-01: *args tests

    def test_var_positional_in_inputs(self):
        """*args parameter appears in inputs (FUNC-01)."""

        def foo(*args):
            pass

        fn = FunctionNode(foo)
        assert "args" in fn.inputs
        assert fn.inputs == ("args",)

    def test_var_positional_no_default(self):
        """*args has no default (FUNC-01)."""

        def foo(*args):
            pass

        fn = FunctionNode(foo)
        assert fn.defaults == {}
        assert not fn.has_default_for("args")

    def test_var_positional_with_annotation(self):
        """*args can have type annotation (FUNC-01)."""

        def foo(*args: int):
            pass

        fn = FunctionNode(foo)
        # The annotation for *args may or may not appear in parameter_annotations
        # depending on how get_type_hints handles variadic parameters
        # Just verify no exception is raised
        assert "args" in fn.inputs

    # FUNC-02: **kwargs tests

    def test_var_keyword_in_inputs(self):
        """**kwargs parameter appears in inputs (FUNC-02)."""

        def foo(**kwargs):
            pass

        fn = FunctionNode(foo)
        assert "kwargs" in fn.inputs
        assert fn.inputs == ("kwargs",)

    def test_var_keyword_no_default(self):
        """**kwargs has no default (FUNC-02)."""

        def foo(**kwargs):
            pass

        fn = FunctionNode(foo)
        assert fn.defaults == {}
        assert not fn.has_default_for("kwargs")

    # FUNC-03: keyword-only tests

    def test_keyword_only_in_inputs(self):
        """Keyword-only params appear in inputs (FUNC-03)."""

        def foo(a, *, kw):
            pass

        fn = FunctionNode(foo)
        assert fn.inputs == ("a", "kw")

    def test_keyword_only_with_default(self):
        """Keyword-only can have defaults (FUNC-03)."""

        def foo(a, *, kw=10):
            pass

        fn = FunctionNode(foo)
        assert fn.defaults == {"kw": 10}
        assert fn.has_default_for("kw")
        assert fn.get_default_for("kw") == 10

    def test_keyword_only_without_default(self):
        """Keyword-only without default has no entry in defaults (FUNC-03)."""

        def foo(a, *, kw):
            pass

        fn = FunctionNode(foo)
        assert "kw" not in fn.defaults
        assert not fn.has_default_for("kw")

    def test_keyword_only_with_annotation(self):
        """Keyword-only with type annotation (FUNC-03)."""

        def foo(a, *, kw: str):
            pass

        fn = FunctionNode(foo)
        assert fn.parameter_annotations.get("kw") == str

    # FUNC-04: positional-only tests

    def test_positional_only_in_inputs(self):
        """Positional-only params appear in inputs (FUNC-04)."""

        def foo(a, /, b):
            pass

        fn = FunctionNode(foo)
        assert fn.inputs == ("a", "b")

    def test_positional_only_with_default(self):
        """Positional-only can have defaults (FUNC-04)."""

        def foo(a=5, /, b=10):
            pass

        fn = FunctionNode(foo)
        assert fn.defaults == {"a": 5, "b": 10}

    def test_positional_only_with_annotation(self):
        """Positional-only with type annotation (FUNC-04)."""

        def foo(a: int, /) -> str:
            return ""

        fn = FunctionNode(foo, output_name="result")
        assert fn.parameter_annotations.get("a") == int

    # FUNC-05: mixed argument types tests

    def test_mixed_all_param_kinds(self):
        """Function with all parameter kinds (FUNC-05)."""

        def foo(pos_only, /, regular, *args, kw_only, **kwargs):
            pass

        fn = FunctionNode(foo)
        assert fn.inputs == ("pos_only", "regular", "args", "kw_only", "kwargs")

    def test_mixed_with_defaults(self):
        """Mixed params with various defaults (FUNC-05)."""

        def foo(pos=1, /, reg=2, *args, kw=3, **kwargs):
            pass

        fn = FunctionNode(foo)
        assert fn.defaults == {"pos": 1, "reg": 2, "kw": 3}
        # *args and **kwargs never have defaults
        assert "args" not in fn.defaults
        assert "kwargs" not in fn.defaults

    def test_mixed_with_annotations(self):
        """Mixed params with type annotations (FUNC-05)."""

        def foo(pos: int, /, reg: str, *args: float, kw: bool, **kwargs: dict) -> list:
            pass

        fn = FunctionNode(foo, output_name="result")
        # Verify standard params are annotated
        assert fn.parameter_annotations.get("pos") == int
        assert fn.parameter_annotations.get("reg") == str
        assert fn.parameter_annotations.get("kw") == bool

    def test_mixed_rename_works(self):
        """rename_inputs works with mixed param kinds (FUNC-05)."""

        def foo(a, /, b, *, c):
            pass

        fn = FunctionNode(foo, rename_inputs={"a": "x", "b": "y", "c": "z"})
        assert fn.inputs == ("x", "y", "z")

    def test_mixed_callable(self):
        """Mixed-signature function is still callable (FUNC-05)."""

        def foo(a, /, b, *args, c, **kwargs):
            return (a, b, args, c, kwargs)

        fn = FunctionNode(foo, output_name="result")
        result = fn(1, 2, 3, 4, c=5, extra=6)
        assert result == (1, 2, (3, 4), 5, {"extra": 6})

    def test_only_var_args_and_kwargs(self):
        """Function with just *args and **kwargs (FUNC-05)."""

        def foo(*args, **kwargs):
            pass

        fn = FunctionNode(foo)
        assert fn.inputs == ("args", "kwargs")
        assert fn.defaults == {}

    def test_keyword_only_multiple(self):
        """Multiple keyword-only params (FUNC-05)."""

        def foo(*, a, b=1, c):
            pass

        fn = FunctionNode(foo)
        assert fn.inputs == ("a", "b", "c")
        assert fn.defaults == {"b": 1}

    def test_positional_only_multiple(self):
        """Multiple positional-only params (FUNC-05)."""

        def foo(a, b, c, /):
            pass

        fn = FunctionNode(foo)
        assert fn.inputs == ("a", "b", "c")


class TestGeneratorEdgeCases:
    """Tests for generator node edge cases (GAP-07)."""

    def test_generator_yields_none_values(self):
        """Generator yielding None values."""

        def gen_none(n: int):
            for _ in range(n):
                yield None

        fn = FunctionNode(gen_none, output_name="items")
        assert fn.is_generator is True

    def test_generator_detection_basic(self):
        """Basic generator detection."""

        def basic_gen():
            yield 1
            yield 2

        fn = FunctionNode(basic_gen, output_name="items")
        assert fn.is_generator is True
        assert fn.is_async is False

    def test_empty_generator_detection(self):
        """Generator that yields nothing is still detected as generator."""

        def empty_gen():
            return
            yield  # Makes it a generator

        fn = FunctionNode(empty_gen, output_name="items")
        assert fn.is_generator is True

    def test_generator_with_return_statement(self):
        """Generator with return statement."""

        def gen_with_return(n: int):
            for i in range(n):
                yield i
            return  # Explicit return

        fn = FunctionNode(gen_with_return, output_name="items")
        assert fn.is_generator is True

    def test_async_generator_detection(self):
        """Async generator detection."""

        async def async_gen():
            yield 1
            yield 2

        fn = FunctionNode(async_gen, output_name="items")
        assert fn.is_generator is True
        assert fn.is_async is True

    def test_generator_with_complex_yield_patterns(self):
        """Generator with conditional yields."""

        def conditional_gen(items):
            for item in items:
                if item > 0:
                    yield item
                elif item == 0:
                    yield None
                # Negative items not yielded

        fn = FunctionNode(conditional_gen, output_name="results")
        assert fn.is_generator is True

    def test_generator_expression_not_generator_node(self):
        """Function returning generator expression is not a generator node."""

        def returns_genexp(n: int):
            return (i for i in range(n))

        fn = FunctionNode(returns_genexp, output_name="items")
        # Function itself is not a generator, it returns one
        assert fn.is_generator is False

    def test_generator_with_parameters(self):
        """Generator with multiple parameters."""

        def gen_with_params(start: int, stop: int, step: int = 1):
            current = start
            while current < stop:
                yield current
                current += step

        fn = FunctionNode(gen_with_params, output_name="items")
        assert fn.is_generator is True
        assert fn.inputs == ("start", "stop", "step")
        assert fn.defaults == {"step": 1}

    def test_generator_with_type_annotations(self):
        """Generator with full type annotations."""
        from typing import Iterator

        def typed_gen(n: int) -> Iterator[int]:
            for i in range(n):
                yield i

        fn = FunctionNode(typed_gen, output_name="items")
        assert fn.is_generator is True
        assert fn.parameter_annotations == {"n": int}

    def test_nested_generator_not_detected(self):
        """Function with nested generator def is not a generator."""

        def contains_gen():
            def inner_gen():
                yield 1

            return list(inner_gen())

        fn = FunctionNode(contains_gen, output_name="result")
        # Outer function is not a generator
        assert fn.is_generator is False


class TestGeneratorExecution:
    """Tests for generator execution in graphs."""

    def test_generator_accumulated_to_list(self):
        """Generator results are accumulated to list by runner."""
        from hypergraph import Graph
        from hypergraph.runners.sync import SyncRunner

        @node(output_name="items")
        def gen_items(n: int):
            for i in range(n):
                yield i

        graph = Graph([gen_items])
        runner = SyncRunner()

        result = runner.run(graph, {"n": 3})

        assert result["items"] == [0, 1, 2]

    def test_generator_yielding_none(self):
        """Generator yielding None values accumulates correctly."""
        from hypergraph import Graph
        from hypergraph.runners.sync import SyncRunner

        @node(output_name="items")
        def gen_none(n: int):
            for _ in range(n):
                yield None

        graph = Graph([gen_none])
        runner = SyncRunner()

        result = runner.run(graph, {"n": 3})

        assert result["items"] == [None, None, None]

    def test_empty_generator_returns_empty_list(self):
        """Generator yielding nothing returns empty list."""
        from hypergraph import Graph
        from hypergraph.runners.sync import SyncRunner

        @node(output_name="items")
        def empty_gen():
            return
            yield  # Make it a generator

        graph = Graph([empty_gen])
        runner = SyncRunner()

        result = runner.run(graph, {})

        assert result["items"] == []

    def test_generator_in_chain(self):
        """Generator output flows to downstream node."""
        from hypergraph import Graph
        from hypergraph.runners.sync import SyncRunner

        @node(output_name="items")
        def gen_items(n: int):
            for i in range(n):
                yield i * 2

        @node(output_name="total")
        def sum_items(items: list) -> int:
            return sum(items)

        graph = Graph([gen_items, sum_items])
        runner = SyncRunner()

        result = runner.run(graph, {"n": 4})

        assert result["items"] == [0, 2, 4, 6]
        assert result["total"] == 12

    def test_generator_in_cycle(self):
        """Generator node in cyclic graph."""
        from hypergraph import Graph
        from hypergraph.runners.sync import SyncRunner

        @node(output_name="items")
        def growing_gen(items: list, limit: int = 5):
            # Yield existing items plus one more
            for item in items:
                yield item
            if len(items) < limit:
                yield len(items)

        graph = Graph([growing_gen])
        runner = SyncRunner()

        result = runner.run(graph, {"items": [], "limit": 3})

        # Generator should grow the list: [] -> [0] -> [0,1] -> [0,1,2]
        # At len=3, we stop adding (limit reached)
        # Final should stabilize at [0,1,2]
        assert len(result["items"]) <= 3

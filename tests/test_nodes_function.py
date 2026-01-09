"""Tests for hypergraph.nodes.function."""

import warnings


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

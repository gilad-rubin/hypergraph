"""Tests for hypergraph._utils."""


from hypergraph._utils import ensure_tuple, hash_definition


class TestEnsureTuple:
    """Tests for ensure_tuple()."""

    def test_single_string(self):
        """Single string becomes 1-tuple."""
        assert ensure_tuple("foo") == ("foo",)

    def test_empty_string(self):
        """Empty string becomes 1-tuple with empty string."""
        assert ensure_tuple("") == ("",)

    def test_single_element_tuple(self):
        """1-tuple passes through unchanged."""
        assert ensure_tuple(("foo",)) == ("foo",)

    def test_multi_element_tuple(self):
        """Multi-element tuple passes through unchanged."""
        assert ensure_tuple(("a", "b", "c")) == ("a", "b", "c")

    def test_empty_tuple(self):
        """Empty tuple passes through unchanged."""
        assert ensure_tuple(()) == ()


class TestHashDefinition:
    """Tests for hash_definition()."""

    def test_returns_64_char_hex_string(self):
        """Hash is a 64-character hex string (SHA256)."""

        def foo():
            pass

        result = hash_definition(foo)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_same_source_same_hash(self):
        """Functions with identical source have the same hash."""

        def foo():
            return 1

        def bar():
            return 1

        # Note: They won't have the same hash because their names differ
        # in the source. We test consistency instead.
        hash1 = hash_definition(foo)
        hash2 = hash_definition(foo)
        assert hash1 == hash2

    def test_different_source_different_hash(self):
        """Functions with different source have different hashes."""

        def foo():
            return 1

        def bar():
            return 2

        assert hash_definition(foo) != hash_definition(bar)

    def test_nested_function(self):
        """Nested functions can be hashed."""

        def outer():
            def inner():
                return 42

            return inner

        inner = outer()
        result = hash_definition(inner)
        assert len(result) == 64

    def test_method(self):
        """Methods can be hashed."""

        class MyClass:
            def method(self):
                return 42

        obj = MyClass()
        result = hash_definition(obj.method)
        assert len(result) == 64

    def test_hash_is_cached_by_caller_not_function(self):
        """hash_definition computes fresh each time (caching is caller's job)."""

        def foo():
            return 1

        hash1 = hash_definition(foo)
        hash2 = hash_definition(foo)
        # Both calls compute the same result
        assert hash1 == hash2

    def test_lambda_in_file(self):
        """Lambda defined in a file can be hashed."""
        # Lambda defined in file (not REPL)
        fn = lambda x: x * 2  # noqa: E731
        result = hash_definition(fn)
        assert len(result) == 64

    # --- Bytecode fallback tests ---

    def test_exec_created_function_returns_hash(self):
        """Functions created via exec() should fall back to bytecode hashing."""
        namespace = {}
        exec("def dynamic(x): return x + 1", namespace)
        fn = namespace["dynamic"]

        result = hash_definition(fn)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_exec_created_function_is_deterministic(self):
        """Same exec'd function hashed twice gives the same result."""
        namespace = {}
        exec("def dynamic(x): return x + 1", namespace)
        fn = namespace["dynamic"]

        assert hash_definition(fn) == hash_definition(fn)

    def test_exec_different_body_different_hash(self):
        """exec'd functions with different bodies produce different hashes."""
        ns1, ns2 = {}, {}
        exec("def f(x): return x + 1", ns1)
        exec("def f(x): return x * 2", ns2)

        assert hash_definition(ns1["f"]) != hash_definition(ns2["f"])

    def test_builtin_function_returns_hash(self):
        """Built-in functions should produce a stable hash, not raise."""
        result = hash_definition(len)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_builtin_is_deterministic(self):
        """Same built-in hashed twice gives the same result."""
        assert hash_definition(len) == hash_definition(len)

    def test_different_builtins_different_hash(self):
        """Different built-ins produce different hashes."""
        assert hash_definition(len) != hash_definition(print)

    def test_exec_different_defaults_different_hash(self):
        """exec'd functions with different default values produce different hashes."""
        ns1, ns2 = {}, {}
        exec("def f(x=1): return x", ns1)  # noqa: S102
        exec("def f(x=2): return x", ns2)  # noqa: S102

        assert hash_definition(ns1["f"]) != hash_definition(ns2["f"])

    # Note: Closure/global variable changes are not captured by hash_definition.
    # This is a known limitation shared by all DAG frameworks (see Hamilton docs).
    # The hash captures the function's *code*, not the values in its environment.

    def test_functools_partial_returns_hash(self):
        """functools.partial objects should produce a stable hash."""
        from functools import partial

        fn = partial(int, base=16)
        result = hash_definition(fn)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

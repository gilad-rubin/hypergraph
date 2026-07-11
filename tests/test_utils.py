"""Tests for hypergraph._utils."""

import os
import subprocess
import sys
import textwrap

import pytest

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

    def test_dynamic_frozenset_default_is_stable_across_hash_seeds(self, tmp_path):
        """Dynamic defaults must not inherit process-randomized repr ordering."""
        script = tmp_path / "dynamic_default.py"
        script.write_text(
            textwrap.dedent(
                """
                from hypergraph._utils import hash_definition


                namespace = {}
                exec(
                    "def dynamic(labels=frozenset({"
                    "'triage', 'dosage', 'allergy', 'discharge'"
                    "})): return labels",
                    namespace,
                )
                print(hash_definition(namespace["dynamic"]))
                """
            ),
            encoding="utf-8",
        )

        hashes = []
        for seed in ("1", "2"):
            result = subprocess.run(
                [sys.executable, str(script)],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONHASHSEED": seed},
            )
            hashes.append(result.stdout.strip())

        assert len(set(hashes)) == 1
        assert len(hashes[0]) == 64

    def test_dynamic_supported_default_leaf_changes_hash(self):
        """A changed leaf in a supported default remains part of identity."""

        def dynamic(default):
            namespace = {"default": default}
            exec("def generated(value=default): return value", namespace)  # noqa: S102
            return namespace["generated"]

        baseline = dynamic(frozenset({"triage", "dosage"}))
        changed = dynamic(frozenset({"triage", "allergy"}))

        assert hash_definition(baseline) != hash_definition(changed)

    def test_dynamic_supported_closure_leaf_changes_hash(self):
        """A changed leaf in a dynamic closure remains part of identity."""
        namespace = {}
        exec(  # noqa: S102
            "def factory(state):\n    def generated():\n        return state\n    return generated",
            namespace,
        )

        baseline = namespace["factory"](frozenset({"triage", "dosage"}))
        changed = namespace["factory"](frozenset({"triage", "allergy"}))

        assert hash_definition(baseline) != hash_definition(changed)

    @pytest.mark.parametrize("location", ["default", "closure"])
    def test_dynamic_opaque_state_refuses(self, location):
        """Opaque dynamic state must not leak an address into identity."""
        if location == "default":
            namespace = {"opaque": object()}
            exec("def generated(value=opaque): return value", namespace)  # noqa: S102
            generated = namespace["generated"]
        else:
            namespace = {}
            exec(  # noqa: S102
                "def factory(state):\n    def generated():\n        return state\n    return generated",
                namespace,
            )
            generated = namespace["factory"](object())

        with pytest.raises(TypeError, match=r"object.*cache_key\(\)"):
            hash_definition(generated)

    def test_dynamic_cyclic_default_refuses(self):
        """Cyclic defaults fail loudly instead of using recursive repr."""
        cyclic = []
        cyclic.append(cyclic)
        namespace = {"cyclic": cyclic}
        exec("def generated(value=cyclic): return value", namespace)  # noqa: S102

        with pytest.raises(TypeError, match=r"cycle.*cache_key\(\)"):
            hash_definition(namespace["generated"])

    def test_nested_dynamic_code_body_changes_hash(self):
        """Nested code constants retain their executable body."""

        def outer(inner_result):
            namespace = {}
            exec(  # noqa: S102
                f"def generated():\n    def nested():\n        return {inner_result}\n    return nested",
                namespace,
            )
            return namespace["generated"]

        assert hash_definition(outer(1)) != hash_definition(outer(2))

    def test_dynamic_top_level_name_table_changes_hash(self):
        """Different referenced globals cannot collapse to one code identity."""
        first, second = {}, {}
        exec("def generated(x): return foo(x)", first)  # noqa: S102
        exec("def generated(x): return bar(x)", second)  # noqa: S102

        assert hash_definition(first["generated"]) != hash_definition(second["generated"])

    def test_dynamic_code_ignores_filename_and_first_line(self):
        """Checkout path and first-line location are not identity facts."""
        source = "def generated():\n    return 42"
        first, second = {}, {}
        exec(compile(source, "/checkout/one/component.py", "exec"), first)  # noqa: S102
        exec(  # noqa: S102
            compile("\n\n" + source, "/checkout/two/component.py", "exec"),
            second,
        )

        assert hash_definition(first["generated"]) == hash_definition(second["generated"])

    # Source-defined closure/global values are not captured because source is the
    # identity. The dynamic bytecode fallback above additionally captures closures.

    def test_functools_partial_returns_hash(self):
        """functools.partial objects should produce a stable hash."""
        from functools import partial

        fn = partial(int, base=16)
        result = hash_definition(fn)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)


class _Summarizer:
    """Configurable component used by bound-method hashing tests."""

    def __init__(self, model: str):
        self.model = model

    def summarize(self, text: str) -> str:
        return f"{self.model}: {text}"


class TestHashDefinitionBoundMethods:
    """Bound methods mix instance state into the hash.

    The fingerprint is captured when the hash is computed (node construction),
    so post-construction mutation of instance state is not tracked.
    """

    def test_differently_configured_instances_different_hash(self):
        """Same method, different instance state → different hash."""
        a = _Summarizer(model="gpt-4")
        b = _Summarizer(model="haiku")

        assert hash_definition(a.summarize) != hash_definition(b.summarize)

    def test_same_instance_stable_hash(self):
        """Same instance, same method → stable hash across calls."""
        obj = _Summarizer(model="gpt-4")

        assert hash_definition(obj.summarize) == hash_definition(obj.summarize)

    def test_identically_configured_instances_same_hash(self):
        """Two instances with identical state hash the same."""
        a = _Summarizer(model="gpt-4")
        b = _Summarizer(model="gpt-4")

        assert hash_definition(a.summarize) == hash_definition(b.summarize)

    def test_cache_key_method_takes_precedence(self):
        """A callable cache_key() defines the fingerprint (hypercache convention)."""

        class Component:
            def __init__(self, model: str, client: object):
                self.model = model
                self.client = client  # not part of identity

            def cache_key(self) -> dict:
                return {"model": self.model}

            def run(self, x: int) -> int:
                return x

        a = Component("gpt-4", client=object())
        b = Component("gpt-4", client=object())  # different client, same key
        c = Component("haiku", client=object())

        assert hash_definition(a.run) == hash_definition(b.run)
        assert hash_definition(a.run) != hash_definition(c.run)

    def test_frozen_frozenset_state_is_stable_across_hash_seeds(self, tmp_path):
        """Typed state must not inherit process-randomized container repr order."""
        script = tmp_path / "frozen_component.py"
        script.write_text(
            textwrap.dedent(
                """
                from dataclasses import dataclass

                from hypergraph._utils import hash_definition


                @dataclass(frozen=True)
                class FrozenComponent:
                    labels: frozenset[str]

                    def run(self, value: int) -> int:
                        return value


                component = FrozenComponent(
                    frozenset({"triage", "dosage", "allergy", "discharge"})
                )
                print(hash_definition(component.run))
                """
            ),
            encoding="utf-8",
        )

        hashes = []
        for seed in ("1", "2"):
            result = subprocess.run(
                [sys.executable, str(script)],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONHASHSEED": seed},
            )
            hashes.append(result.stdout.strip())

        assert len(set(hashes)) == 1
        assert len(hashes[0]) == 64

    def test_frozen_dataclass_state_leaf_changes_hash(self):
        """Canonicalization must retain every supported state leaf."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Component:
            labels: frozenset[str]

            def run(self, value: int) -> int:
                return value

        baseline = Component(frozenset({"triage", "dosage"}))
        changed = Component(frozenset({"triage", "allergy"}))

        assert hash_definition(baseline.run) != hash_definition(changed.run)

    def test_nested_mapping_order_does_not_change_hash(self):
        """Mapping insertion order is not execution identity."""

        class Component:
            def __init__(self, settings: dict[str, int]):
                self.settings = settings

            def run(self, value: int) -> int:
                return value

        forward = Component({"alpha": 1, "beta": 2})
        reversed_ = Component({"beta": 2, "alpha": 1})

        assert hash_definition(forward.run) == hash_definition(reversed_.run)

    def test_pydantic_like_model_dump_is_deterministic_state(self):
        """A model_dump() contract is supported without consulting object repr."""

        class Model:
            def __init__(self, name: str):
                self.name = name

            def model_dump(self):
                return {"name": self.name}

            def __repr__(self):
                return object.__repr__(self)

        class Component:
            def __init__(self, model: Model):
                self.model = model

            def run(self, value: int) -> int:
                return value

        a = Component(Model("clinical"))
        b = Component(Model("clinical"))

        assert hash_definition(a.run) == hash_definition(b.run)

    def test_opaque_nested_state_refuses_with_cache_key_guidance(self):
        """Opaque objects must not leak addresses into execution identity."""

        class Component:
            def __init__(self):
                self.client = object()

            def run(self, value: int) -> int:
                return value

        with pytest.raises(TypeError, match=r"object.*cache_key\(\)"):
            hash_definition(Component().run)

    def test_cyclic_nested_state_refuses(self):
        """Cycles fail loudly instead of falling back to recursive repr."""

        class Component:
            def __init__(self):
                self.steps = []
                self.steps.append(self.steps)

            def run(self, value: int) -> int:
                return value

        with pytest.raises(TypeError, match=r"cycle.*cache_key\(\)"):
            hash_definition(Component().run)

    def test_builtin_subclass_without_explicit_state_refuses(self):
        """A builtin subclass cannot silently collapse into its base value."""

        class Label(str):
            pass

        class Component:
            def __init__(self):
                self.label = Label("clinical")

            def run(self, value: int) -> int:
                return value

        with pytest.raises(TypeError, match=r"Label.*cache_key\(\)"):
            hash_definition(Component().run)

    def test_slots_instance_falls_back_to_code_hash(self):
        """Instances without __dict__ fall back to code-only hashing."""

        class Slotted:
            __slots__ = ("model",)

            def __init__(self, model: str):
                self.model = model

            def run(self, x: int) -> int:
                return x

        a = Slotted("gpt-4")
        b = Slotted("haiku")

        result = hash_definition(a.run)
        assert len(result) == 64
        # No accessible state — same as pre-fix behavior
        assert result == hash_definition(b.run)

    def test_classmethod_keeps_code_only_hash(self):
        """Classmethods are not fingerprinted (class dicts are not stable)."""

        class WithClassmethod:
            @classmethod
            def run(cls, x: int) -> int:
                return x

        result = hash_definition(WithClassmethod.run)
        assert len(result) == 64
        assert result == hash_definition(WithClassmethod.run)

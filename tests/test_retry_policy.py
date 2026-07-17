"""Contract tests for the public RetryPolicy / RetryAfterError surface (#230).

Assertion map (validation contract, wave A2):
    S6  validation errors before execution   TestRetryPolicyValidation, TestRetryAfterErrorValidation
    S8  direct calls stay raw                test_direct_function_call_is_single_shot,
                                             test_node_call_is_single_shot
    —   node-owned declaration + clones      TestNodeRetryParam
    —   fingerprint canonicality             TestPolicyFingerprint
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from hypergraph import FunctionNode, RetryAfterError, RetryPolicy, node


class TestRetryPolicyValidation:
    """S6 — every invalid policy is rejected at construction, before any execution."""

    def test_retry_on_is_required(self):
        with pytest.raises(TypeError):
            RetryPolicy(max_attempts=3)  # type: ignore[call-arg]

    def test_empty_retry_on_rejected(self):
        with pytest.raises(ValueError, match="retry_on"):
            RetryPolicy(max_attempts=3, retry_on=())

    @pytest.mark.parametrize(
        "bad_entry",
        [KeyboardInterrupt, SystemExit, BaseException, asyncio.CancelledError, GeneratorExit],
    )
    def test_base_exception_family_rejected(self, bad_entry):
        with pytest.raises(ValueError, match="BaseException"):
            RetryPolicy(max_attempts=3, retry_on=(ValueError, bad_entry))

    @pytest.mark.parametrize("bad_entry", ["timeout", ValueError("instance"), 42])
    def test_non_class_entries_rejected(self, bad_entry):
        with pytest.raises((TypeError, ValueError), match="retry_on"):
            RetryPolicy(max_attempts=3, retry_on=(bad_entry,))

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_max_attempts_rejected(self, bad):
        with pytest.raises(ValueError, match="max_attempts"):
            RetryPolicy(max_attempts=bad, retry_on=(ValueError,))

    def test_bogus_jitter_rejected(self):
        with pytest.raises(ValueError, match="jitter"):
            RetryPolicy(max_attempts=3, retry_on=(ValueError,), jitter="bogus")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("initial_delay", 0.0),
            ("initial_delay", -1.0),
            ("backoff_multiplier", 0.0),
            ("backoff_multiplier", -2.0),
            ("max_delay", 0.0),
            ("max_delay", -5.0),
            ("retry_window", 0.0),
            ("retry_window", -10.0),
            ("initial_delay", float("nan")),
            ("max_delay", float("inf")),
        ],
    )
    def test_non_positive_or_non_finite_timing_rejected(self, field, value):
        kwargs = {"max_attempts": 3, "retry_on": (ValueError,), field: value}
        with pytest.raises(ValueError, match=field):
            RetryPolicy(**kwargs)

    def test_policy_is_frozen(self):
        policy = RetryPolicy(max_attempts=3, retry_on=(ValueError,))
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.max_attempts = 5  # type: ignore[misc]

    def test_single_exception_type_normalized_to_tuple(self):
        policy = RetryPolicy(max_attempts=3, retry_on=ValueError)
        assert policy.retry_on == (ValueError,)

    def test_defaults_match_locked_contract(self):
        policy = RetryPolicy(max_attempts=3, retry_on=(ValueError,))
        assert policy.retry_window is None
        assert policy.initial_delay == 1.0
        assert policy.backoff_multiplier == 2.0
        assert policy.max_delay == 60.0
        assert policy.jitter == "full"

    def test_exception_base_is_allowed(self):
        # Only the BaseException family is rejected; a broad Exception entry is legal.
        policy = RetryPolicy(max_attempts=2, retry_on=(Exception,))
        assert policy.retry_on == (Exception,)


class TestRetryAfterErrorValidation:
    def test_carries_error_and_delay(self):
        underlying = ConnectionError("rate limited")
        carrier = RetryAfterError(underlying, retry_after=30)
        assert carrier.error is underlying
        assert carrier.retry_after == 30.0

    @pytest.mark.parametrize("bad", [-1, float("inf"), float("nan")])
    def test_invalid_delay_rejected(self, bad):
        with pytest.raises(ValueError, match="retry_after"):
            RetryAfterError(ConnectionError("x"), retry_after=bad)

    def test_zero_delay_allowed(self):
        assert RetryAfterError(ConnectionError("x"), retry_after=0).retry_after == 0.0

    @pytest.mark.parametrize("bad", [KeyboardInterrupt(), ValueError, "boom"])
    def test_error_must_be_exception_instance(self, bad):
        with pytest.raises(TypeError, match="error"):
            RetryAfterError(bad, retry_after=1)

    def test_nested_carrier_rejected(self):
        inner = RetryAfterError(ConnectionError("x"), retry_after=1)
        with pytest.raises(TypeError, match="RetryAfterError"):
            RetryAfterError(inner, retry_after=2)


class TestNodeRetryParam:
    def test_node_decorator_accepts_policy(self):
        policy = RetryPolicy(max_attempts=3, retry_on=(ConnectionError,))

        @node(output_name="out", retry=policy)
        def fetch(x: int) -> int:
            return x

        assert fetch.retry is policy

    def test_default_is_no_retry(self):
        @node(output_name="out")
        def fetch(x: int) -> int:
            return x

        assert fetch.retry is None

    def test_retry_true_shorthand_rejected(self):
        with pytest.raises(TypeError, match="RetryPolicy"):

            @node(output_name="out", retry=True)
            def fetch(x: int) -> int:
                return x

    def test_function_node_constructor_accepts_policy(self):
        policy = RetryPolicy(max_attempts=2, retry_on=(ValueError,))

        def fetch(x: int) -> int:
            return x

        fn = FunctionNode(fetch, output_name="out", retry=policy)
        assert fn.retry is policy

    def test_clones_carry_the_declaration(self):
        policy = RetryPolicy(max_attempts=3, retry_on=(ConnectionError,))

        @node(output_name="out", retry=policy)
        def fetch(x: int) -> int:
            return x

        assert fetch.with_name("fetch2").retry is policy
        assert fetch.rename_inputs({"x": "y"}).retry is policy
        assert fetch.rename_outputs({"out": "value"}).retry is policy

    def test_retry_does_not_change_cache_identity(self):
        def fetch(x: int) -> int:
            return x

        plain = FunctionNode(fetch, output_name="out")
        with_retry = FunctionNode(fetch, output_name="out", retry=RetryPolicy(max_attempts=5, retry_on=(ValueError,)))
        assert plain.definition_hash == with_retry.definition_hash


class TestPolicyFingerprint:
    def test_equal_policies_share_a_fingerprint(self):
        a = RetryPolicy(max_attempts=3, retry_on=(ConnectionError, TimeoutError))
        b = RetryPolicy(max_attempts=3, retry_on=(ConnectionError, TimeoutError))
        assert a.fingerprint == b.fingerprint

    def test_retry_on_order_is_canonical(self):
        a = RetryPolicy(max_attempts=3, retry_on=(ConnectionError, TimeoutError))
        b = RetryPolicy(max_attempts=3, retry_on=(TimeoutError, ConnectionError))
        assert a.fingerprint == b.fingerprint

    @pytest.mark.parametrize(
        "changed",
        [
            {"max_attempts": 4},
            {"retry_on": (ValueError,)},
            {"retry_window": 30.0},
            {"initial_delay": 0.5},
            {"backoff_multiplier": 3.0},
            {"max_delay": 10.0},
            {"jitter": "none"},
        ],
    )
    def test_every_field_feeds_the_fingerprint(self, changed):
        base_kwargs = {"max_attempts": 3, "retry_on": (ConnectionError,)}
        base = RetryPolicy(**base_kwargs)
        other = RetryPolicy(**{**base_kwargs, **changed})
        assert base.fingerprint != other.fingerprint


# === S8: direct calls stay raw ===


def test_direct_function_call_is_single_shot():
    calls: list[int] = []

    @node(
        output_name="out",
        retry=RetryPolicy(max_attempts=5, retry_on=(ConnectionError,), initial_delay=0.001),
    )
    def fetch(x: int) -> int:
        calls.append(x)
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        fetch.func(1)
    assert calls == [1]


def test_node_call_is_single_shot():
    calls: list[int] = []

    @node(
        output_name="out",
        retry=RetryPolicy(max_attempts=5, retry_on=(ConnectionError,), initial_delay=0.001),
    )
    def fetch(x: int) -> int:
        calls.append(x)
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        fetch(1)
    assert calls == [1]

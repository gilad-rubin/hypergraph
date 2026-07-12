"""Public export surface checks for the hypergraph root package."""

import hypergraph


class TestExceptionExports:
    """All runtime exceptions are importable from the hypergraph root."""

    def test_execution_error_exported(self):
        """ExecutionError is importable from the root and listed in __all__ (D10)."""
        from hypergraph import ExecutionError

        assert ExecutionError is hypergraph.exceptions.ExecutionError
        assert "ExecutionError" in hypergraph.__all__

    def test_failure_evidence_api_exported(self):
        """Structured failure evidence and its accessor are root exports."""
        from hypergraph import FailureEvidence, get_failure_evidence
        from hypergraph.runners._shared.results import FailureEvidence as CanonicalFailureEvidence

        assert FailureEvidence is CanonicalFailureEvidence
        assert get_failure_evidence is hypergraph.exceptions.get_failure_evidence
        assert "FailureEvidence" in hypergraph.__all__
        assert "get_failure_evidence" in hypergraph.__all__

    def test_all_names_resolve(self):
        """Every name in __all__ is an attribute of the package."""
        for name in hypergraph.__all__:
            assert hasattr(hypergraph, name), f"__all__ lists missing attribute: {name}"

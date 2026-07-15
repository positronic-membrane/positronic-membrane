"""Behavioral evaluation harness (issue #112).

Layer 1 (mechanical parity) names the E2E suite (#61) and conformance suite
(#101) as the parity bar. Layer 2 (behavioral benchmark) is this package: a
fixed, versioned suite of conversation probes plus a bounded autonomous-week
sandbox run, scored by an LLM judge and compared against a recorded baseline.

Usage: python -m benchmarks.run --target <label>
"""

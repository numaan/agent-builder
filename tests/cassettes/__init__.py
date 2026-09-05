"""Recorded model responses for the phase-3 tests, and the generator that writes them.

``python -m tests.cassettes.build_cassettes`` rewrites every ``*.json`` here from the scenarios
in :mod:`tests.cassettes.scenarios`, offline against a scripted stand-in; ``--live`` records the
same scenarios against the real API when ``ANTHROPIC_API_KEY`` is set. Tests never mention a
hash, so a regeneration changes no test file.
"""

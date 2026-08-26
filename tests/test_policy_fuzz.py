"""F2: fuzz parse_well_known() — the one parser in this codebase that
ingests attacker-reachable input (a site's own /.well-known/considerate.json
response). It must never raise anything other than PolicyError, and never
hang, no matter how malformed or adversarially-shaped the input is.
"""

import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from considerate.exceptions import PolicyError
from considerate.policy import parse_well_known

_json_value = st.recursive(
    st.none() | st.booleans() | st.floats(allow_nan=False, allow_infinity=False) | st.text(max_size=50),
    lambda children: (
        st.lists(children, max_size=6) | st.dictionaries(st.text(max_size=20), children, max_size=6)
    ),
    max_leaves=40,
)


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(_json_value)
def test_arbitrary_json_values_never_crash_unexpectedly(value):
    raw = json.dumps(value)
    try:
        parse_well_known(raw)
    except PolicyError:
        pass  # the one acceptable outcome for malformed/unexpected shapes


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(st.text(max_size=200))
def test_arbitrary_text_never_crashes_unexpectedly(text):
    try:
        parse_well_known(text)
    except PolicyError:
        pass


# A handful of specifically adversarial shapes worth pinning down by name,
# not just relying on the fuzzer to eventually generate them.
_ADVERSARIAL_DOCS = [
    "{}",
    "[]",
    "null",
    "true",
    "42",
    '"just a string"',
    '{"agents": "not an object"}',
    '{"agents": {"Bot": "not an object either"}}',
    '{"agents": {"Bot": null}}',
    '{"default": null}',
    '{"default": []}',
    '{"crawl_windows": "not a list"}',
    '{"crawl_windows": [null, 42, "x", {"multiplier": "not a number"}]}',
    '{"disallow_paths": "not a list"}',
    '{"disallow_paths": [1, 2, null, "/ok"]}',
    '{"version": 123}',  # non-string version — coerced, not fatal
    '{"contact": 456}',  # non-string contact — dropped, not fatal
    '{"' + "a" * 100_000 + '": 1}',  # pathologically long key
]


def test_specific_adversarial_documents_do_not_raise_unexpected_errors():
    for doc in _ADVERSARIAL_DOCS:
        try:
            parse_well_known(doc)
        except PolicyError:
            pass
        except Exception as exc:  # pragma: no cover - the point is that this never happens
            raise AssertionError(f"unexpected {type(exc).__name__} for input {doc!r}: {exc}") from exc

"""Validate schema/considerate.schema.json itself: the shipped example must
pass, and a battery of realistically-broken policy files must fail — this
is what backs `considerate policy validate` (see test_cli.py) and what any
site operator's own tooling (a JSON-Schema-aware editor, a CI check) would
run against.
"""

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = Path(__file__).parent.parent / "schema" / "considerate.schema.json"
EXAMPLE_PATH = Path(__file__).parent.parent / "examples" / "site_setup" / "considerate.json"

SCHEMA = json.loads(SCHEMA_PATH.read_text())


def _validator():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    return jsonschema.Draft202012Validator(SCHEMA)


def test_schema_is_itself_well_formed():
    _validator()  # raises if the schema document itself is invalid


def test_shipped_example_validates():
    doc = json.loads(EXAMPLE_PATH.read_text())
    errors = list(_validator().iter_errors(doc))
    assert errors == []


def test_minimal_empty_policy_validates():
    assert list(_validator().iter_errors({})) == []


@pytest.mark.parametrize(
    "bad_doc",
    [
        {"default": {"requests_per_second": -1}},  # must be > 0
        {"default": {"requests_per_second": "fast"}},  # wrong type
        {"default": {"tier": "extremely-fragile"}},  # not one of the enum
        {"disallow_paths": ["admin"]},  # must start with '/'
        {"crawl_windows": [{"hours": "25:00-06:00", "multiplier": 1}]},  # invalid hour
        {"crawl_windows": [{"days": ["someday"], "multiplier": 1}]},  # not a real day
        {"crawl_windows": [{"multiplier": 0}]},  # must be > 0
        {"crawl_windows": [{"unexpected_field": True}]},  # crawlWindow is additionalProperties: false
        {"agents": {"Bot": {"max_concurrent": 0}}},  # must be >= 1
    ],
)
def test_realistic_bad_documents_fail(bad_doc):
    errors = list(_validator().iter_errors(bad_doc))
    assert errors, f"expected schema validation errors for {bad_doc!r}"

"""ConsiderateConfig.from_yaml() had zero test coverage before this file —
found while wiring up coverage reporting (F5). It's a real, documented
feature (README shows it directly), not just plumbing.
"""

from pathlib import Path

import pytest

from considerate import ConsiderateConfig

EXAMPLE_YAML = Path(__file__).parent.parent / "considerate.yaml.example"


def test_from_yaml_loads_the_shipped_example():
    config = ConsiderateConfig.from_yaml(EXAMPLE_YAML)
    assert config.default_tier == "standard"
    assert config.overrides["small-business-site.com"] == "fragile"
    assert config.respect_robots_txt is True
    assert config.fetch_well_known is True
    assert config.max_concurrent_per_domain == 2
    assert config.breaker.error_rate_threshold == 0.2
    assert config.breaker.consecutive_failures == 3
    assert config.breaker.cooldown_seconds == 60


def test_from_yaml_with_minimal_file(tmp_path):
    yaml_path = tmp_path / "minimal.yaml"
    yaml_path.write_text("default_tier: robust\n")
    config = ConsiderateConfig.from_yaml(yaml_path)
    assert config.default_tier == "robust"
    # Everything else falls back to dataclass defaults.
    assert config.max_concurrent_per_domain == 2
    assert config.respect_robots_txt is True


def test_from_yaml_with_empty_file_uses_all_defaults(tmp_path):
    yaml_path = tmp_path / "empty.yaml"
    yaml_path.write_text("")
    config = ConsiderateConfig.from_yaml(yaml_path)
    assert config == ConsiderateConfig()


def test_from_yaml_overrides_partial_circuit_breaker_fields(tmp_path):
    yaml_path = tmp_path / "partial.yaml"
    yaml_path.write_text("circuit_breaker:\n  cooldown_seconds: 5\n")
    config = ConsiderateConfig.from_yaml(yaml_path)
    assert config.breaker.cooldown_seconds == 5
    # Untouched breaker fields keep their dataclass defaults.
    assert config.breaker.consecutive_failures == 3


def test_from_yaml_missing_pyyaml_gives_a_clear_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("simulated: pyyaml not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match=r"pip install considerate\[yaml\]"):
        ConsiderateConfig.from_yaml(EXAMPLE_YAML)

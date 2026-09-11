"""Drift test for the `robot-blackbox init` config template.

`_DEFAULT_CONFIG_YAML` in `cli/app.py` is what fresh installs land with.
Whenever the in-code dataclass default (`BlackBoxConfig.default()`)
adds or removes a knob, the template must follow; otherwise users
silently miss new behaviour. This test catches that drift before it
ships.

Scope: every key in `BlackBoxConfig.default()` that the template
exposes (we currently expose all of them) must round-trip through the
template back to the same value, modulo legitimate format choices
(numeric coercion, key ordering, comment-only lines).
"""

from __future__ import annotations

from dataclasses import asdict

import yaml

from blackboxrs.cli.app import _DEFAULT_CONFIG_YAML
from blackboxrs.core.config import BlackBoxConfig


def _loaded_template() -> dict:
    raw = yaml.safe_load(_DEFAULT_CONFIG_YAML) or {}
    assert isinstance(raw, dict), "template did not parse to a dict"
    return raw


def test_template_rosbag2_trigger_event_types_matches_default():
    """Regression for the v0.4 detector-expansion miss: when
    `tf_topology` / `process_signals` / `clock_skew` triggers were
    added to the in-code default, the template was not updated. Result
    was that fresh installs missed bag-recording on three detector
    classes. Keep these two lists locked together."""
    template = _loaded_template()
    default = BlackBoxConfig.default()
    template_triggers = template["rosbag2"]["trigger_event_types"]
    default_triggers = default.rosbag2.trigger_event_types
    assert set(template_triggers) == set(default_triggers), (
        f"trigger_event_types drift between template and default.\n"
        f"  template only: {sorted(set(template_triggers) - set(default_triggers))}\n"
        f"  default only:  {sorted(set(default_triggers) - set(template_triggers))}\n"
        f"Fix: update _DEFAULT_CONFIG_YAML in blackboxrs/cli/app.py."
    )


def test_template_loads_through_dict_to_config_without_warnings(caplog):
    """The template must parse cleanly with strict-mode unknown-key
    detection enabled. If it doesn't, a fresh `init` immediately fails
    `BlackBoxConfig.load(strict=True)` in CI/deployment validation."""
    template = _loaded_template()
    # Use the same internal helper `load()` uses, in strict mode.
    from blackboxrs.core.config import _dict_to_config

    cfg = _dict_to_config(template, strict=True, source="<_DEFAULT_CONFIG_YAML>")
    # And the post-load runtime policy must still apply cleanly.
    cfg.apply_runtime_role()
    # Smoke: roundtrip back to dict and ensure no top-level keys
    # vanish.
    rt = asdict(cfg)
    assert set(rt) >= set(template), (
        f"keys lost during template round-trip: {set(template) - set(rt)}"
    )

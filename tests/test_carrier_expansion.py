"""`agedum-provider/v2` — schema gating, the v1 intent diagnostic, and (later
sections) `carrierMeta` validation + per-carrier expansion.

Synthetic mechanism fixtures only: no fleet prose, no agentsconf content.
"""

import json

import pytest

from agedum.provider import (
    PROVIDER_SCHEMA_VERSION,
    PROVIDER_SCHEMA_VERSION_2,
    ProviderSchemaError,
    expand_carrier_refs,
    load_config_with_format,
    load_merged_config_with_format,
)


def _write_yaml(root, rel, text):
    """Write a YAML config at ``root/rel`` (creating parents); return its path."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --- schema matrix: both versions load; JSON is v1 forever ---


def test_yaml_v1_and_v2_both_load(tmp_path):
    v1 = _write_yaml(tmp_path, "v1.yaml", f"schema: {PROVIDER_SCHEMA_VERSION}\nharness: claude\n")
    v2 = _write_yaml(tmp_path, "v2.yaml", f"schema: {PROVIDER_SCHEMA_VERSION_2}\nharness: claude\n")
    assert load_config_with_format(v1).schema == PROVIDER_SCHEMA_VERSION
    assert load_config_with_format(v2).schema == PROVIDER_SCHEMA_VERSION_2
    # The schema key is consumed either way — the envelope stays JSON-shaped.
    assert "schema" not in load_config_with_format(v1).config
    assert "schema" not in load_config_with_format(v2).config


def test_json_documents_are_schema_less_v1_forever(tmp_path):
    path = tmp_path / "j.json"
    path.write_text(json.dumps({"harness": "claude"}))
    loaded = load_config_with_format(path)
    assert loaded.format == "json"
    assert loaded.schema == PROVIDER_SCHEMA_VERSION


def test_unsupported_schema_keeps_the_v1_engine_error_string(tmp_path):
    # Regression pin for the 0.60 behaviour: an old (v1-only) engine refuses a v2
    # document loudly with exactly this error, naming the expected value. The
    # message template must not drift — it is the rollout warning's contract
    # (fleet minimum ≥ 0.61: on older engines the launcher vanishes from
    # `agedum --providers` with this error).
    path = _write_yaml(tmp_path, "v9.yaml", "schema: agedum-provider/v9\nharness: claude\n")
    with pytest.raises(ProviderSchemaError) as excinfo:
        load_config_with_format(path)
    assert str(excinfo.value) == (
        f"{path}: YAML provider config must declare `schema: "
        f"{PROVIDER_SCHEMA_VERSION}` (found 'agedum-provider/v9')"
    )


# --- the root document's declared schema gates expansion ---


def _ds_intent_yaml(schema):
    return (
        f"schema: {schema}\n"
        "harness: opencode\n"
        "config:\n"
        "  opencodeConfig:\n"
        "    agent:\n"
        "      worker:\n"
        "        mode: subagent\n"
        "        model: ds-flash@high\n"
    )


def test_root_governs_merge_v1_base_v2_root_expands(tmp_path):
    # The v1 base carries no intent of its own; the v2 root's schema governs.
    _write_yaml(tmp_path, "base.yaml", "schema: agedum-provider/v1\nharness: opencode\n")
    root = _write_yaml(
        tmp_path, "child.yaml", _ds_intent_yaml(PROVIDER_SCHEMA_VERSION_2) + "extends: base.yaml\n"
    )
    merged = load_merged_config_with_format(root, tmp_path)
    assert merged.schema == PROVIDER_SCHEMA_VERSION_2
    assert "schema" not in merged.config


def test_root_governs_merge_v2_base_v1_root_does_not_expand(tmp_path):
    # The misplaced intent — `@`-refs under a v1 root — gets the named diagnostic
    # even though a base declared v2: expansion follows the root, and intent
    # without the root's license is exactly what the diagnostic exists for.
    _write_yaml(tmp_path, "base.yaml", f"schema: {PROVIDER_SCHEMA_VERSION_2}\nharness: opencode\n")
    root = _write_yaml(
        tmp_path, "child.yaml", _ds_intent_yaml(PROVIDER_SCHEMA_VERSION) + "extends: base.yaml\n"
    )
    merged = load_merged_config_with_format(root, tmp_path)
    assert merged.schema == PROVIDER_SCHEMA_VERSION
    with pytest.raises(ProviderSchemaError, match="agedum-provider/v2"):
        expand_carrier_refs(merged.config, root_schema=merged.schema)


# --- the v1 diagnostic: intent markers in a v1 document are a named load error ---


@pytest.mark.parametrize(
    "extra",
    [
        # an opencode agent model @-ref
        "config:\n  opencodeConfig:\n    agent:\n      w:\n        model: ds-flash@high\n",
        # a config.model @-ref
        "config:\n  model: ds-flash@high\n",
        # the universe meta key
        "expansionModels:\n  - ds-flash@high\n",
    ],
)
def test_v1_document_with_intent_gets_the_named_diagnostic(tmp_path, extra):
    path = _write_yaml(
        tmp_path,
        "v1-intent.yaml",
        f"schema: {PROVIDER_SCHEMA_VERSION}\nharness: opencode\n" + extra,
    )
    merged = load_merged_config_with_format(path, tmp_path)
    with pytest.raises(ProviderSchemaError) as excinfo:
        expand_carrier_refs(merged.config, root_schema=merged.schema)
    message = str(excinfo.value)
    assert "looks like expansion intent" in message
    assert f"declare `schema: {PROVIDER_SCHEMA_VERSION_2}`" in message


def test_v1_diagnostic_scopes_at_ref_slots_not_any_at_sign(tmp_path):
    # An `@` inside a prompt/description is prose, not intent; a non-opencode
    # `model` value with an `@` is that harness's own string. Neither is flagged.
    path = _write_yaml(
        tmp_path,
        "v1-at-prose.yaml",
        f"schema: {PROVIDER_SCHEMA_VERSION}\n"
        "harness: opencode\n"
        "config:\n"
        "  model: deepseek/deepseek-flash\n"
        "  opencodeConfig:\n"
        "    agent:\n"
        "      w:\n"
        "        model: deepseek/deepseek-flash\n"
        "        prompt: review @alice's notes tomorrow\n",
    )
    merged = load_merged_config_with_format(path, tmp_path)
    assert expand_carrier_refs(merged.config, root_schema=merged.schema) == merged.config


def test_clean_v1_config_passes_the_seam_unchanged(tmp_path):
    # Zero behaviour change: no markers, no expansion — the config is returned
    # untouched and nothing is read from disk.
    config = {
        "harness": "opencode",
        "config": {"model": "deepseek/deepseek-flash", "effortLevel": "high"},
    }
    assert expand_carrier_refs(config, root_schema=PROVIDER_SCHEMA_VERSION) is config


# --- v2 with no intent markers is a documented no-op ---


def test_v2_without_intent_is_a_no_op_schema_alone_is_not_intent(tmp_path):
    # A v2 schema with zero `@`-refs and no `expansionModels` expands nothing —
    # declaring v2 alone is not intent (so a v2 config on a non-opencode harness
    # with no markers never errors).
    config = {
        "harness": "opencode",
        "config": {"model": "deepseek/deepseek-flash"},
    }
    assert expand_carrier_refs(config, root_schema=PROVIDER_SCHEMA_VERSION_2) == config


def test_v2_strips_a_present_expansion_models_key():
    # `expansionModels` is consumed like `modelsCatalog` — never reaches the launch.
    config = {"harness": "opencode", "expansionModels": []}
    expanded = expand_carrier_refs(config, root_schema=PROVIDER_SCHEMA_VERSION_2)
    assert "expansionModels" not in expanded

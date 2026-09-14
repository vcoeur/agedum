"""`agedum-provider/v2` — schema gating, the v1 intent diagnostic, `carrierMeta`
validation, and per-carrier expansion.

Synthetic mechanism fixtures only: no fleet prose, no agentsconf content.
"""

import json

import pytest

from agedum.provider import (
    PROVIDER_SCHEMA_VERSION,
    PROVIDER_SCHEMA_VERSION_2,
    ModelCatalogSchemaError,
    ProviderSchemaError,
    expand_carrier_refs,
    expand_model_refs,
    load_config_with_format,
    load_merged_config_with_format,
    load_model_catalog,
    load_model_catalog_with_carrier_meta,
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


# --- carrierMeta: the catalogue's expansion-facts section ---
# One synthetic model per effort-carrier family: deepseek/glm carry
# options.reasoningEffort, gpt carries variant, kimi rides model aliases.

SYNTH_CATALOGUE = """\
schema: agedum-models/v1
models:
  ds-flash:
    name: DS Flash
    limit: {context: 1000000, output: 65536}
  glm-x:
    name: GLM X
    limit: {context: 200000, output: 32768}
  sol:
    name: GPT-5.6 Sol
    attachment: true
  k3:
    name: Kimi K3
    limit: {context: 262144, output: 32768}
carrierMeta:
  ds-flash:
    provider: ds
    family: deepseek
    efforts: [high, low]
    display: DS Flash
  glm-x:
    provider: glm-p
    family: glm
    efforts: [high, low]
    display: GLM X
  sol:
    provider: openai
    family: gpt
    efforts: [high, low]
    display: GPT-5.6 Sol
  k3:
    provider: kimi-coding
    family: kimi
    efforts: [high, low]
    display: Kimi K3
    aliases: {high: k3, low: k3-low}
    alias_model_id: k3
"""


def _write_catalogue(root, text=SYNTH_CATALOGUE, rel="models.yaml"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_synth_catalogue_loads_with_carrier_meta(tmp_path):
    _write_catalogue(tmp_path)
    catalog = load_model_catalog_with_carrier_meta(tmp_path / "models.yaml")
    assert set(catalog.models) == {"ds-flash", "glm-x", "sol", "k3"}
    assert catalog.carrier_meta["k3"] == {
        "provider": "kimi-coding",
        "family": "kimi",
        "efforts": ["high", "low"],
        "display": "Kimi K3",
        "aliases": {"high": "k3", "low": "k3-low"},
        "alias_model_id": "k3",
    }


def test_load_model_catalog_still_returns_the_models_map(tmp_path):
    # The v1-shaped entry point is unchanged — models only (carrierMeta is
    # validated on load but not returned there).
    _write_catalogue(tmp_path)
    models = load_model_catalog(tmp_path / "models.yaml")
    assert models["ds-flash"] == {
        "name": "DS Flash",
        "limit": {"context": 1000000, "output": 65536},
    }


def test_carrier_meta_absent_is_an_empty_facts_map(tmp_path):
    _write_catalogue(tmp_path, "schema: agedum-models/v1\nmodels:\n  m1:\n    name: M1\n")
    catalog = load_model_catalog_with_carrier_meta(tmp_path / "models.yaml")
    assert catalog.carrier_meta == {}


def test_carrier_meta_present_v1_filing_is_byte_identical(tmp_path):
    # v1 `modelRef` filing reads only `models`: a catalogue carrying carrierMeta
    # files the same verbatim fragment a 0.60 engine would (which ignores the
    # section entirely).
    _write_catalogue(tmp_path)
    config = {
        "harness": "opencode",
        "config": {
            "providerDef": {
                "id": "ds",
                "npm": "@ai-sdk/openai-compatible",
                "modelRef": "ds-flash",
            }
        },
    }
    expanded = expand_model_refs(config, tmp_path)
    assert expanded["config"]["opencodeConfig"]["provider"]["ds"]["models"]["ds-flash"] == {
        "name": "DS Flash",
        "limit": {"context": 1000000, "output": 65536},
    }


def test_unknown_carrier_meta_keys_are_ignored(tmp_path):
    # Data-file philosophy: future facts (phase 3 adds `vision`) land here
    # without a catalogue change.
    text = SYNTH_CATALOGUE.replace(
        "    display: DS Flash\n",
        "    display: DS Flash\n    vision: true\n    someday: maybe\n",
    )
    _write_catalogue(tmp_path, text)
    assert (
        load_model_catalog_with_carrier_meta(tmp_path / "models.yaml").carrier_meta["ds-flash"][
            "vision"
        ]
        is True
    )


# Base facts for the bad-entry matrix: the ds entry omits exactly the key under
# test; the k3 entry is the model-alias shape with a non-alphabet effort to trip
# the aliases checks.
_DS_PROVIDER = "    provider: ds\n"
_DS_FACTS = _DS_PROVIDER + "    family: deepseek\n    display: D\n"
_DS_FACTS_NO_FAMILY = _DS_PROVIDER + "    display: D\n"
_DS_FACTS_NO_DISPLAY = _DS_PROVIDER + "    family: deepseek\n"
_K3_FACTS = "    provider: kimi-coding\n    family: kimi\n    display: K\n    efforts: [high]\n"


@pytest.mark.parametrize(
    ("meta_block", "match"),
    [
        ("  ds-flash: nope\n", "carrierMeta entry for model 'ds-flash' must be a mapping"),
        ("  ds-flash:\n    family: deepseek\n", "key `provider`"),
        ('  ds-flash:\n    provider: ""\n', "key `provider`"),
        ("  ds-flash:\n    provider: 5\n", "key `provider`"),
        ("  ds-flash:\n" + _DS_FACTS_NO_FAMILY, "key `family`"),
        ("  ds-flash:\n" + _DS_FACTS_NO_DISPLAY, "key `display`"),
        ("  ds-flash:\n" + _DS_FACTS, "key `efforts`"),
        ("  ds-flash:\n" + _DS_FACTS + "    efforts: []\n", "key `efforts`"),
        ("  ds-flash:\n" + _DS_FACTS + "    efforts: [high, turbo]\n", "key `efforts`"),
        ("  ds-flash:\n" + _DS_FACTS + "    efforts: high\n", "key `efforts`"),
        (
            "  ds-flash:\n" + _DS_FACTS + "    efforts: [high]\n    alias_model_id: ds-flash\n",
            "key `alias_model_id`",
        ),
        ("  k3:\n" + _K3_FACTS + "    aliases: {high: k3, turbo: k3-t}\n", "key `aliases`"),
        ("  k3:\n" + _K3_FACTS + '    aliases: {high: ""}\n', "key `aliases`"),
        ("  k3:\n" + _K3_FACTS + "    aliases: high\n", "key `aliases`"),
        ("  k3:\n" + _K3_FACTS + "    aliases: {high: k3}\n", "key `alias_model_id`"),
        (
            "  k3:\n" + _K3_FACTS + '    aliases: {high: k3}\n    alias_model_id: ""\n',
            "key `alias_model_id`",
        ),
    ],
)
def test_bad_carrier_meta_entries_error_naming_model_and_key(tmp_path, meta_block, match):
    text = (
        "schema: agedum-models/v1\nmodels:\n  ds-flash:\n    name: D\ncarrierMeta:\n" + meta_block
    )
    _write_catalogue(tmp_path, text)
    with pytest.raises(ModelCatalogSchemaError, match=match):
        load_model_catalog(tmp_path / "models.yaml")


def test_carrier_meta_section_itself_must_be_a_mapping(tmp_path):
    _write_catalogue(
        tmp_path, "schema: agedum-models/v1\nmodels:\n  m1:\n    name: M1\ncarrierMeta: [a]\n"
    )
    with pytest.raises(ModelCatalogSchemaError, match="`carrierMeta` must be a mapping"):
        load_model_catalog(tmp_path / "models.yaml")

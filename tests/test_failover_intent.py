"""`failoverIntent` — run-time failover-rung resolution from a v2 config's own
agents (phase 3, stage c): the collision matrix, the builder's four filtering
rules, roster mapping, per-carrier translation + `rungOptions` + `vision`,
universe subsumption, determinism, and zero behaviour change.

Synthetic mechanism fixtures only: no fleet prose, no agentsconf content.
"""

import pytest

from agedum.provider import (
    PROVIDER_SCHEMA_VERSION,
    PROVIDER_SCHEMA_VERSION_2,
    ExpansionError,
    ProviderSchemaError,
    _derive_failover_block,
    _ModelRef,
    expand_carrier_refs,
)

# One synthetic model per effort-carrier family, every entry carrying the
# `vision` fact the failover vision map is derived from (ds-flash pins a
# `false`).
FAILOVER_CATALOGUE = """\
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
    vision: false
  glm-x:
    provider: glm-p
    family: glm
    efforts: [high, low]
    display: GLM X
    vision: true
  sol:
    provider: openai
    family: gpt
    efforts: [high, low]
    display: GPT-5.6 Sol
    vision: true
  k3:
    provider: kimi-coding
    family: kimi
    efforts: [high, low]
    display: Kimi K3
    aliases: {high: k3, low: k3-low}
    alias_model_id: k3
    vision: true
"""


def _write_catalogue(root, text=FAILOVER_CATALOGUE):
    path = root / "models.yaml"
    path.write_text(text)
    return path


_DETECT = {"status": [429, 402], "messages": ["usage limit", "quota"]}


def _intent(chains, max_walk=3):
    return {"detect": _DETECT, "maxWalk": max_walk, "chains": chains}


def _config(agents, model=None, expansion_models=None, failover_intent=None, failover=None):
    """A minimal merged v2 config around an opencodeConfig agent map."""
    block = {"opencodeConfig": {"agent": agents}}
    if model is not None:
        block["model"] = model
    config = {"harness": "opencode", "secretEnv": "K", "config": block}
    if expansion_models is not None:
        config["expansionModels"] = expansion_models
    if failover_intent is not None:
        config["failoverIntent"] = failover_intent
    if failover is not None:
        config["failover"] = failover
    return config


def _expand(config, tmp_path):
    return expand_carrier_refs(config, root_schema=PROVIDER_SCHEMA_VERSION_2, base_dir=tmp_path)


def _catalogue_without_glm_vision(root):
    """The fixture catalogue minus glm-x's `vision` fact (missing-fact paths)."""
    without = FAILOVER_CATALOGUE.replace(
        "    display: GLM X\n    vision: true\n", "    display: GLM X\n"
    )
    _write_catalogue(root, without)


# --- the collision matrix (Decision 3, all six rows) ---


def test_v1_with_precomputed_failover_passes_through_byte_unchanged():
    # Row 1 — today's behaviour, unchanged: the seam returns the config
    # untouched (identity, not a copy) and reads nothing.
    block = {
        "detect": {"status": [429], "messages": ["rate"]},
        "maxWalk": 3,
        "vision": {"ds/ds-flash": True},
        "chains": {"ds/ds-v4-pro@high": ["ds/ds-flash@high"]},
        "rungOptions": {"ds/ds-flash@high": {"reasoning_effort": "high"}},
    }
    config = {"harness": "opencode", "failover": block, "config": {"model": "ds/ds-v4-pro"}}
    assert expand_carrier_refs(config, root_schema=PROVIDER_SCHEMA_VERSION) is config


def test_v1_with_failover_intent_is_the_named_load_error():
    # Row 2 — `failoverIntent` joined `_intent_markers`, so the existing
    # "this looks like expansion intent; declare v2" diagnostic fires.
    config = {"harness": "opencode", "failoverIntent": _intent({})}
    with pytest.raises(ProviderSchemaError) as excinfo:
        expand_carrier_refs(config, root_schema=PROVIDER_SCHEMA_VERSION)
    message = str(excinfo.value)
    assert "top-level `failoverIntent`" in message
    assert f"declare `schema: {PROVIDER_SCHEMA_VERSION_2}`" in message


def test_v2_with_intent_only_expands_and_strips(tmp_path):
    # Row 3 — intent expands into the effective `failover` block; the intent
    # key is consumed like `expansionModels`.
    _write_catalogue(tmp_path)
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        failover_intent=_intent({"sol@high": ["glm-x@high"]}),
    )
    expanded = _expand(config, tmp_path)
    assert "failoverIntent" not in expanded
    assert expanded["failover"]["chains"] == {"openai/sol@high": ["glm-p/glm-x@high"]}


def test_v2_with_both_keys_is_a_named_error():
    # Row 4 — two declarations of the same block; the error names both keys.
    config = _config(
        {"m": {"mode": "subagent", "model": "ds-flash@high"}},
        failover_intent=_intent({"ds-flash@high": ["glm-x@high"]}),
        failover={},
    )
    with pytest.raises(ExpansionError, match="`failoverIntent`.*precomputed `failover`"):
        expand_carrier_refs(config, root_schema=PROVIDER_SCHEMA_VERSION_2)


def test_v2_with_precomputed_failover_only_passes_through_untouched(tmp_path):
    # Row 5 — the 0.61 contract preserved verbatim: the block is the *same
    # object* while the agent refs around it still expand.
    _write_catalogue(tmp_path)
    block = {"detect": _DETECT, "maxWalk": 3, "chains": {"ds/ds-flash@high": ["ds/ds-flash@low"]}}
    config = _config(
        {"m": {"mode": "subagent", "model": "ds-flash@high"}},
        failover=block,
    )
    expanded = _expand(config, tmp_path)
    assert expanded["failover"] is block
    assert expanded["config"]["opencodeConfig"]["agent"]["m"]["model"] == "ds/ds-flash"


def test_with_neither_key_there_is_no_failover_output(tmp_path):
    # Row 6 — the omission rule: no keys in, no block out.
    _write_catalogue(tmp_path)
    expanded = _expand(_config({"m": {"mode": "subagent", "model": "ds-flash@high"}}), tmp_path)
    assert "failover" not in expanded


def test_failover_intent_on_a_non_opencode_harness_is_refused():
    # `failoverIntent` is intent — the modelRef opencode-first rule applies.
    config = {"harness": "claude", "failoverIntent": _intent({})}
    with pytest.raises(ExpansionError, match="only implemented for the opencode harness"):
        expand_carrier_refs(config, root_schema=PROVIDER_SCHEMA_VERSION_2)


# --- the intent's own shape errors ---


def test_malformed_failover_intent_shapes_are_named_errors(tmp_path):
    _write_catalogue(tmp_path)
    with pytest.raises(ExpansionError, match="`failoverIntent` must be a JSON object"):
        _expand(_config({}, failover_intent="nope"), tmp_path)
    with pytest.raises(ExpansionError, match="`failoverIntent.chains` must be a JSON object"):
        _expand(_config({}, failover_intent=_intent("nope")), tmp_path)
    with pytest.raises(ExpansionError, match="entry 'sol@high' must be a list of rung refs"):
        _expand(_config({}, failover_intent=_intent({"sol@high": "glm-x@high"})), tmp_path)


# --- the four filtering rules (Decision 2, step 3) ---


def test_chain_source_outside_the_roster_is_dropped(tmp_path):
    # Rule 1 — a source that is no primary/subagent pair drops, even though
    # its rung would resolve fine.
    _write_catalogue(tmp_path)
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        failover_intent=_intent(
            {
                "sol@high": ["glm-x@high"],
                "k3@low": ["glm-x@low"],  # k3 declared by no agent → dropped
            }
        ),
    )
    failover = _expand(config, tmp_path)["failover"]
    assert list(failover["chains"]) == ["openai/sol@high"]


def test_dropped_chains_contribute_no_filing(tmp_path):
    # The ds / gpt-ds-flash snapshot evidence, as a synthetic: a dropped
    # chain's rungs name models that must not file catalog entries and must
    # not appear in the vision map.
    _write_catalogue(tmp_path)
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        failover_intent=_intent(
            {
                "sol@high": ["glm-x@high"],
                "k3@low": ["ds-flash@high", "glm-x@low"],  # dropped
            }
        ),
    )
    expanded = _expand(config, tmp_path)
    providers = expanded["config"]["opencodeConfig"]["provider"]
    assert list(providers) == ["openai", "glm-p"]  # no ds, no kimi-coding
    assert "ds/ds-flash" not in expanded["failover"]["vision"]
    # The dropped chain's rung effort adds nothing to the survivor's plan.
    assert list(expanded["failover"]["rungOptions"]) == ["glm-p/glm-x@high"]


def test_chain_authored_without_rungs_is_dropped(tmp_path):
    # Rule 3 — a chain left without rungs drops. (Rule 2 is structurally
    # retained but live-void under the union semantics — every resolved rung
    # of a surviving chain joins the universe before translation — so an
    # authored-empty rung list is the only way a chain arrives empty.)
    _write_catalogue(tmp_path)
    config = _config(
        {
            "m": {"mode": "primary", "model": "sol@high"},
            "w": {"mode": "subagent", "model": "sol@low"},
        },
        failover_intent=_intent({"sol@high": [], "sol@low": ["glm-x@high"]}),
    )
    failover = _expand(config, tmp_path)["failover"]
    assert list(failover["chains"]) == ["openai/sol@low"]


def test_zero_surviving_chains_omits_the_whole_block(tmp_path):
    # Rule 4 — absence means ignore, never an error: no `failover` key, and
    # the consumed intent is stripped all the same.
    _write_catalogue(tmp_path)
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        failover_intent=_intent({"k3@high": ["glm-x@high"]}),
    )
    expanded = _expand(config, tmp_path)
    assert "failover" not in expanded
    assert "failoverIntent" not in expanded


def test_filter_is_idempotent_on_a_survivor_shape_intent(tmp_path):
    # A converted-shape intent (every chain a survivor) re-filters to itself:
    # the derived chains are the authored chains translated 1:1 — nothing
    # dropped, nothing reordered, nothing added.
    _write_catalogue(tmp_path)
    config = _config(
        {
            "m": {"mode": "primary", "model": "sol@high"},
            "w": {"mode": "subagent", "model": "k3@high"},
        },
        failover_intent=_intent(
            {"sol@high": ["glm-x@high", "glm-x@low"], "k3@high": ["glm-x@high"]}
        ),
    )
    failover = _expand(config, tmp_path)["failover"]
    assert list(failover["chains"]) == ["openai/sol@high", "kimi-coding/k3"]
    assert failover["chains"]["openai/sol@high"] == ["glm-p/glm-x@high", "glm-p/glm-x@low"]
    assert failover["chains"]["kimi-coding/k3"] == ["glm-p/glm-x@high"]


def test_post_translation_source_collision_is_a_named_error():
    # The builder's defensive collision check, carried over. Unreachable
    # through authored intent (explicit-only refs translate injectively), so
    # the derivation step is exercised directly with two hand-built sources
    # that share a runtime ref.
    meta = {"provider": "openai", "family": "gpt", "efforts": ["high"], "vision": True}
    ref = _ModelRef("sol", "high", "variant", meta)
    surviving = [("a@high", ref, [ref]), ("b@high", ref, [])]
    with pytest.raises(ExpansionError, match="source collision after runtime translation"):
        _derive_failover_block({}, surviving, {}, [])


# --- roster mapping (Decision 2) ---


def test_roster_modes_map_to_mains_and_workers(tmp_path):
    # primary → mains, subagent → workers, any other or absent mode → neither.
    _write_catalogue(tmp_path)
    config = _config(
        {
            "m": {"mode": "primary", "model": "sol@high"},
            "w": {"mode": "subagent", "model": "ds-flash@low"},
            "other": {"mode": "command", "model": "glm-x@high"},
            "nomode": {"model": "k3@high"},
        },
        failover_intent=_intent(
            {
                "sol@high": ["glm-x@low"],  # main source → survives
                "ds-flash@low": ["glm-x@low"],  # worker source → survives
                "glm-x@high": ["glm-x@low"],  # other-mode source → dropped
                "k3@high": ["glm-x@low"],  # absent-mode source → dropped
            }
        ),
    )
    failover = _expand(config, tmp_path)["failover"]
    assert list(failover["chains"]) == ["openai/sol@high", "ds/ds-flash@low"]
    # The other-mode and no-mode agents are not roster members, but their
    # @-refs are still universe members (translated, filed, vision-mapped).
    assert "kimi-coding/k3" in failover["vision"]
    assert "glm-p/glm-x" in failover["vision"]


def test_plain_model_agents_contribute_no_roster_pair(tmp_path):
    # The pair, not the agent id, is the roster unit: a subagent on a plain
    # `provider/model` string cannot anchor a chain source.
    _write_catalogue(tmp_path)
    config = _config(
        {"w": {"mode": "subagent", "model": "ds/ds-flash"}},
        failover_intent=_intent({"ds-flash@low": ["glm-x@high"]}),
    )
    expanded = _expand(config, tmp_path)
    assert "failover" not in expanded  # the only chain dropped → omission
    assert "failoverIntent" not in expanded


def test_main_and_worker_pair_is_counted_once(tmp_path):
    # role_pairs is mains ∪ workers — a pair present as both anchors a source.
    _write_catalogue(tmp_path)
    config = _config(
        {
            "m": {"mode": "primary", "model": "ds-flash@high"},
            "w": {"mode": "subagent", "model": "ds-flash@high"},
        },
        failover_intent=_intent({"ds-flash@high": ["glm-x@high"]}),
    )
    failover = _expand(config, tmp_path)["failover"]
    assert failover["chains"] == {"ds/ds-flash@high": ["glm-p/glm-x@high"]}


# --- per-carrier translation, rungOptions, vision (Decision 2, steps 5-7) ---


def test_per_carrier_translation_rung_options_and_vision(tmp_path):
    _write_catalogue(tmp_path)
    config = _config(
        {
            "m": {"mode": "primary", "model": "sol@high"},
            "w": {"mode": "subagent", "model": "k3@high"},
        },
        failover_intent=_intent({"sol@high": ["glm-x@high", "ds-flash@low", "k3@low"]}),
    )
    failover = _expand(config, tmp_path)["failover"]
    # variant/reasoningEffort keep `provider/key@effort`; kimi translates to
    # the bare effort alias.
    assert failover["chains"] == {
        "openai/sol@high": ["glm-p/glm-x@high", "ds/ds-flash@low", "kimi-coding/k3-low"]
    }
    # rungOptions only for used variant/reasoningEffort rungs — the source
    # (sol@high) and the kimi alias never appear; the value key is snake_case.
    assert failover["rungOptions"] == {
        "glm-p/glm-x@high": {"reasoning_effort": "high"},
        "ds/ds-flash@low": {"reasoning_effort": "low"},
    }
    # vision: provider-major walk over the universe in first-appearance order;
    # kimi gets one entry per declared effort (the alias pair); `false` is
    # preserved, never coerced.
    assert list(failover["vision"]) == [
        "openai/sol",
        "kimi-coding/k3",
        "kimi-coding/k3-low",
        "glm-p/glm-x",
        "ds/ds-flash",
    ]
    assert failover["vision"]["ds/ds-flash"] is False
    # detect/maxWalk are the intent's authored data, verbatim.
    assert failover["detect"] == _DETECT
    assert failover["maxWalk"] == 3


def test_rung_options_canonical_order_while_chains_keep_authored_order(tmp_path):
    _write_catalogue(tmp_path)
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        failover_intent=_intent({"sol@high": ["glm-x@low", "glm-x@high"]}),
    )
    failover = _expand(config, tmp_path)["failover"]
    # Chains keep authored rung order, no per-chain dedup; rungOptions walk
    # the canonical effort order (high first), not authored order.
    assert failover["chains"]["openai/sol@high"] == ["glm-p/glm-x@low", "glm-p/glm-x@high"]
    assert list(failover["rungOptions"]) == ["glm-p/glm-x@high", "glm-p/glm-x@low"]
    assert failover["rungOptions"]["glm-p/glm-x@low"] == {"reasoning_effort": "low"}


def test_vision_walk_is_provider_major_over_the_universe(tmp_path):
    _write_catalogue(tmp_path)
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        model="k3@high",
        failover_intent=_intent({"sol@high": ["ds-flash@high"]}),
    )
    # Universe order: config.model (k3@high) → agent (sol) → surviving rung
    # (ds-flash); vision groups provider-major within it. k3 declares only
    # `high` here, so no low-alias entry — the alias pair is pinned above.
    failover = _expand(config, tmp_path)["failover"]
    assert list(failover["vision"]) == [
        "kimi-coding/k3",
        "openai/sol",
        "ds/ds-flash",
    ]


def test_universe_model_without_vision_fact_is_a_named_error(tmp_path):
    _catalogue_without_glm_vision(tmp_path)
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        failover_intent=_intent({"sol@high": ["glm-x@high"]}),
    )
    with pytest.raises(ExpansionError, match="model 'glm-x'.*no `vision` fact"):
        _expand(config, tmp_path)


def test_omitted_block_never_demands_vision_facts(tmp_path):
    # Rule 4 fires before the vision derivation: a config whose chains all
    # drop needs no `vision` facts at all (absence means ignore).
    _catalogue_without_glm_vision(tmp_path)
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        failover_intent=_intent({"k3@high": ["glm-x@high"]}),  # source not on the roster
    )
    expanded = _expand(config, tmp_path)
    assert "failover" not in expanded


def test_detect_and_max_walk_are_copied_verbatim_never_validated(tmp_path):
    # The policy line: authored data, copied as-is — shape policing belongs to
    # launch-time `failover_spec`, not to expansion.
    _write_catalogue(tmp_path)
    detect = {"status": [429], "messages": ["quota"], "extra": {"nested": True}}
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        failover_intent=_intent({"sol@high": ["glm-x@high"]}, max_walk="three"),
    )
    config["failoverIntent"]["detect"] = detect
    failover = _expand(config, tmp_path)["failover"]
    assert failover["detect"] == detect
    assert failover["maxWalk"] == "three"


# --- universe subsumption (Decision 1) ---


def test_rung_only_model_files_without_expansion_models(tmp_path):
    # The subsumption, live: a surviving rung declares its own universe
    # membership — the model files with no `expansionModels` in sight.
    _write_catalogue(tmp_path)
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        failover_intent=_intent({"sol@high": ["glm-x@high"]}),
    )
    expanded = _expand(config, tmp_path)
    assert "expansionModels" not in expanded
    assert "glm-x" in expanded["config"]["opencodeConfig"]["provider"]["glm-p"]["models"]


def test_expansion_models_and_failover_rungs_union_in_filing_order(tmp_path):
    # Both universe keys are legal together (union semantics, no referee):
    # filing walks agents → `expansionModels` → surviving rungs.
    _write_catalogue(tmp_path)
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        expansion_models=["k3@high"],
        failover_intent=_intent({"sol@high": ["glm-x@high", "k3@low"]}),
    )
    expanded = _expand(config, tmp_path)
    providers = expanded["config"]["opencodeConfig"]["provider"]
    assert list(providers) == ["openai", "kimi-coding", "glm-p"]
    assert list(providers["kimi-coding"]["models"]) == ["k3", "k3-low"]


# --- determinism ---


def test_failover_derivation_is_deterministic(tmp_path):
    _write_catalogue(tmp_path)
    config = _config(
        {
            "m": {"mode": "primary", "model": "sol@high"},
            "w": {"mode": "subagent", "model": "k3@low"},
        },
        failover_intent=_intent(
            {
                "sol@high": ["glm-x@high", "glm-x@low", "ds-flash@high"],
                "k3@low": ["glm-x@low"],
            }
        ),
    )
    first = _expand(config, tmp_path)
    second = _expand(config, tmp_path)
    assert first == second
    assert list(first["failover"]) == list(second["failover"])
    assert list(first["failover"]["chains"]) == list(second["failover"]["chains"])
    assert list(first["failover"]["vision"]) == list(second["failover"]["vision"])
    assert list(first["failover"]["rungOptions"]) == list(second["failover"]["rungOptions"])


# --- sequencing, vision walk, and presence gates (fresh review round) ---


def test_unresolvable_ref_in_a_dropped_chain_still_errors(tmp_path):
    # Step-1 sequencing: every intent ref resolves loud BEFORE any filtering.
    # The `k3@low` chain's source is outside the roster, so the chain would
    # drop — but its unknown rung must still be a named error (resolution
    # errors are authoring errors, never filter-dependent).
    _write_catalogue(tmp_path)
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        failover_intent=_intent({"sol@high": ["glm-x@high"], "k3@low": ["nosuchmodel@high"]}),
    )
    with pytest.raises(ExpansionError, match="'nosuchmodel' is not in the model catalogue"):
        _expand(config, tmp_path)


def test_non_bool_vision_fact_is_a_named_error(tmp_path):
    # A `vision` fact that is not a boolean is the same un-derivable case as a
    # missing one (launch-time `failover_spec` demands bools) — one named
    # error class covers both.
    un_bool = FAILOVER_CATALOGUE.replace(
        "    display: GLM X\n    vision: true\n", "    display: GLM X\n    vision: 'yes'\n"
    )
    _write_catalogue(tmp_path, un_bool)
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        failover_intent=_intent({"sol@high": ["glm-x@high"]}),
    )
    with pytest.raises(ExpansionError, match="model 'glm-x'.*no `vision` fact"):
        _expand(config, tmp_path)


def test_vision_walk_is_provider_major_not_plain_first_appearance(tmp_path):
    # The vision walk groups by provider (providers in first-appearance order,
    # models in first-appearance order within): two models of one provider
    # whose first appearances are separated by another provider land adjacent.
    # A plain first-appearance walk would interleave glm-p between them.
    catalogue = """\
schema: agedum-models/v1
models:
  nova:
    name: GPT Nova
    attachment: true
  glm-x:
    name: GLM X
    limit: {context: 200000, output: 32768}
  sol:
    name: GPT-5.6 Sol
    attachment: true
carrierMeta:
  nova:
    provider: openai
    family: gpt
    efforts: [high, low]
    display: GPT Nova
    vision: true
  glm-x:
    provider: glm-p
    family: glm
    efforts: [high, low]
    display: GLM X
    vision: true
  sol:
    provider: openai
    family: gpt
    efforts: [high, low]
    display: GPT-5.6 Sol
    vision: true
"""
    _write_catalogue(tmp_path, catalogue)
    config = _config(
        {"m": {"mode": "primary", "model": "glm-x@high"}},
        model="nova@high",
        failover_intent=_intent({"glm-x@high": ["sol@low"]}),
    )
    failover = _expand(config, tmp_path)["failover"]
    assert list(failover["vision"]) == ["openai/nova", "openai/sol", "glm-p/glm-x"]


def test_intent_without_detect_or_max_walk_emits_the_block_without_them(tmp_path):
    # "Copied verbatim" includes presence: a degraded intent emits a degraded
    # block — no expansion-time defaults, no expansion-time error; launch-time
    # `failover_spec` polices the emitted block.
    _write_catalogue(tmp_path)
    config = _config(
        {"m": {"mode": "primary", "model": "sol@high"}},
        failover_intent={"chains": {"sol@high": ["glm-x@high"]}},
    )
    failover = _expand(config, tmp_path)["failover"]
    assert "detect" not in failover
    assert "maxWalk" not in failover
    assert failover["chains"] == {"openai/sol@high": ["glm-p/glm-x@high"]}

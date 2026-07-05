---
title: Cline harness · agedum
description: How agedum drives Cline — wrapper-mode resolution (project AGENTS.md read natively, global AGENTS.md to the cross-tool ~/.agents/AGENTS.md, skills to ~/.cline/skills) and the provider config mapped to Cline's CLI flags with the token passed via --key.
---

# Cline

Cline, like [opencode](opencode.md), is **pure path-discovery** in wrapper mode — it reads
its instructions and skills from fixed locations and needs no flags. Cline reads `AGENTS.md`
as a cross-tool rules file, which is exactly the agent-neutral source, so the project
instructions stay in place.

## Wrapper resolution { #wrapper-resolution }

| Source | Injected at |
|---|---|
| project `AGENTS.md` | *(not injected — read natively at `./AGENTS.md`)* |
| project `.agents/skills/` | `<root>/.cline/skills/` |
| global `~/.config/agents/AGENTS.md` (+ optional `AGENTS.cline.md` overlay) | `~/.agents/AGENTS.md` |
| global `~/.config/agents/skills/` | `$CLINE_DATA_DIR/skills/` (default `~/.cline/skills/`) |

- **Project instructions** — Cline reads the project-root `AGENTS.md` as a cross-tool rules
  file. That is the agent-neutral source, already in place, so **agedum injects nothing**
  for it — and never could, since the root `AGENTS.md` is git-tracked.
- **Global instructions** — Cline reads the cross-tool global path `~/.agents/AGENTS.md`,
  so the global `AGENTS.md` is bound there — base merged with an optional `AGENTS.cline.md`
  [overlay](../source-shape.md#agentsharnessmd-per-harness-overlay-user-scope). Note this is
  **not** under `$CLINE_DATA_DIR`: Cline reads global *skills* from `~/.cline/skills/` but
  global cross-tool *instructions* from `~/.agents/AGENTS.md` — the asymmetry is Cline's.
- **Skills** — each skill is a `SKILL.md` folder (the shape Cline already expects), compiled
  with the `SKILL.cline.md` overlay and bound to `./.cline/skills/` (project) and
  `$CLINE_DATA_DIR/skills/` (global, default `~/.cline/skills/`).
- `extra_args`: **none** — Cline discovers everything from disk, like Claude and opencode.

```bash
agedum --wrapper cline -- cline task "review this change"
agedum --wrapper cline --dry-run -- cline        # show what would be injected
```

## Provider config { #provider-config }

Cline's knobs become **appended CLI flags**, like [kimi](kimi.md):

```json
{
  "harness": "cline",
  "slug": "cline-deepseek",
  "secretEnv": "DEEPSEEK_API_KEY",
  "config": {
    "provider": "deepseek",
    "model": "deepseek-v4-pro",
    "effortLevel": "xhigh"
  }
}
```

| `config` key | Appended |
|---|---|
| `model` | `--model <model>` |
| `provider` | `--provider <id>` (Cline provider id; default `cline`) |
| `effortLevel` | `--thinking <none\|low\|medium\|high\|xhigh>` |
| `plan` | `--plan` (true) |
| `secretEnv` value | `--key <token>` |

The config above launches `cline --model deepseek-v4-pro --provider deepseek --thinking
xhigh --key ***`.

**The token lands in argv.** Cline takes its API token as a per-run flag (`--key`), so —
unlike every other harness — the secret is in the launched **command**, visible in the
process list while Cline runs. That is Cline's documented mechanism, not agedum's choice;
agedum masks the token in `--dry-run` (the `command` line shows `--key ***`), and it still
rides the `requiredEnv` export so the `environment` block masks it too.

**Custom `baseUrl`.** Cline has no run-time base-URL flag, and a `--provider`/`--model` flag
set rebuilds the provider from flags (dropping any stored base URL and posting to the OpenAI
default). So for a custom OpenAI-compatible endpoint (a Kimi coding subscription, OpenCode-Go,
…) agedum **generates a single-provider `providers.json`** — the generic `openai-compatible`
provider with `lastUsedProvider` set so Cline selects it with no flag — under an isolated
`CLINE_DATA_DIR` (`~/.cache/agedum/cline/<endpoint-slug>`), and passes the key via `--key`.
Set `model` (the upstream id served there) and `secretEnv`; optional `contextWindow` /
`maxTokens` become a one-entry `models` array so Cline learns the window (its `X/N` meter +
compaction trigger) and output cap. The API key is **never** written to disk. That
`providers.json` is **seeded writable** into `CLINE_DATA_DIR` (not read-only bound), so under a
[sandbox](../provider.md#sandbox) launch Cline can rewrite it to persist its own selection
rather than hitting `EROFS` on a read-only filesystem; agedum re-seeds the correct endpoint
config on every launch. `baseUrl` and a named `provider` are mutually exclusive.

**Named provider.** To use a provider Cline already knows instead, set `provider` (no
`baseUrl`) to a built-in (e.g. `deepseek`) or one set up via `cline auth` (stored in
`~/.cline/data/settings/providers.json`). Under a sandbox launch agedum makes `~/.cline`
writable so that store (and Cline's task state) persists too.

**No subagent-model tiering.** Cline runs a single model per session, so there is no
agedum equivalent of opencode's `agentOptions[]` or reasonix's `subagentModel`, and a
tiered `cline-*-flash` (strong main model + cheap-flash subagents) config is not
expressible. Cline's subagents are fixed read-only *research* agents — their tools are
limited to `read_file`, `list_files`, `search_files`, `list_code_definition_names`,
read-only `execute_command`, and `use_skill`; they can't write, browse, reach MCP, or
nest, and they inherit the session model. `cline --help` (v3.0.15) confirms a single
`-m/--model` with no `--subagent-model`, so the only meaningful flash config is the flat
`cline-flash` (flash as the whole-session `model`).

**`--prompt`/`--run`.** Cline's prompt is a positional argument; `--tui` opens the
interactive TUI seeded with it, and a bare positional runs the task once in act mode and
exits. So `agedum cline-<name> --prompt "<text>"` maps to `cline --tui "<text>"` and
`--run "<text>"` maps to `cline "<text>"` — see the
[prompt-seeding table](../provider.md#prompt-seeding).

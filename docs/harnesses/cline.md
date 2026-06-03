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
| global `~/.agents/skills/` | `$CLINE_DATA_DIR/skills/` (default `~/.cline/skills/`) |

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

**No `baseUrl`.** Cline has no base-URL flag, so a `baseUrl` in a cline config is a
fail-loud `ProviderError`, not a silent no-op. Configure a custom endpoint once as a named
Cline provider (`cline auth`, stored in `~/.cline/data/settings/providers.json`) and select
it with `provider`. So the provider `id` in the config must match a provider Cline already
knows — a built-in (e.g. `deepseek`) or one you set up via `cline auth`.

**`--prompt`/`--run`.** Cline's prompt is a positional argument; `--tui` opens the
interactive TUI seeded with it, and a bare positional runs the task once in act mode and
exits. So `agedum cline-<name> --prompt "<text>"` maps to `cline --tui "<text>"` and
`--run "<text>"` maps to `cline "<text>"` — see the
[prompt-seeding table](../provider.md#prompt-seeding).

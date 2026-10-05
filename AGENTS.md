# agedum — operating instructions

A Python CLI that drives any agent CLI from an agent-neutral source shape
(`AGENTS.md` + `.agents/skills/`), compiling per harness and injecting it via a
private mount namespace at launch. Implemented: **Claude**, **kimi**, **opencode**,
**Cline**, **reasonix**, **aider**, **pi**, and **codex** harnesses at **project + global scope**.

Skills are discovered by walking `.agents/skills/` for every directory holding a
`SKILL.md` (`_discover_skills`), so subfolders group them: a nested `group/skill/`
compiles to the flattened name `group-skill` (and its front-matter `name` is rewritten to
match); top-level skills keep their declared name.

- **Claude** — each scope at its *own* location: project → `./CLAUDE.md` +
  `./.claude/skills/`; global (`~/.config/agents/AGENTS.md` + `~/.config/agents/skills/`) →
  `~/.claude/CLAUDE.md` + `~/.claude/skills/` (`$CLAUDE_CONFIG_DIR`-aware), never merged.
  Global scope also injects agentsconf's **Claude overlay** — `~/.config/agents/claude/settings.json`
  + `~/.config/agents/claude/scripts/` → `~/.claude/settings.json` + `~/.claude/scripts/`, each
  read-only and gated on the source existing (`_inject_claude_overlay`). agentsconf ships those to
  the writable config-agents root, never into the read-only injected target files. The
  surrounding Claude state directory is writable under a sandbox; individual overlays
  are not. agedum injects them the same way as `CLAUDE.md`/skills. `~/.claude.json` auth untouched.
- **kimi** — project `AGENTS.md` is read natively (kimi merges `AGENTS.md` from the
  project root down to the work dir into `KIMI_AGENTS_MD`), so agedum leaves it in
  place. Kimi Code reads global instructions at `~/.kimi-code/AGENTS.md`, so agedum
  binds the global source there, with no instruction flags. Skills are binds: global
  → `~/.kimi-code/skills/`, project → `./.kimi-code/skills/` (both auto-read).
  Global targets follow `KIMI_CODE_HOME`, including the isolated endpoint/model cache
  home used for custom providers. `--run` maps to `kimi --prompt "<text>"` (no
  `--print`) and drops interactive `--yolo`/`--auto`/`--plan`; agedum's interactive
  `--prompt` is unsupported and fails loudly.
- **opencode** — pure path-discovery (no flags). Project `AGENTS.md` is read natively at
  `./AGENTS.md`, so agedum leaves it in place. Global `AGENTS.md` → `<config>/AGENTS.md`;
  skills → `./.opencode/skills/` (project) + `<config>/skills/` (global), where
  `<config>` is `$XDG_CONFIG_HOME/opencode` (default `~/.config/opencode`). opencode
  searches those skills dirs before the project's raw `.agents/skills/`, so the
  overlaid (`SKILL.opencode.md`) copy wins. Matches condash's
  opencode layout; uniform with the Claude harness, no `extra_args`.
  **`failover`** (top-level config key, sibling of `requiredEnv`) starts a launch-bound
  `FailoverProxy` (`proxy.py`) and rewrites the routed providers' `options.baseURL` to
  `<proxy>/oc/<id>` in `OPENCODE_CONFIG_CONTENT`: on an admission wall (429/402/limit
  text) the proxy walks the model's configured chain — rewriting auth, model id, and
   effort options per exact runtime rung (including `rungOptions` overrides for
   `<provider>/<model>@<effort>`), translating a Responses-shaped (openai/OAuth) request
   onto its chat-completions rungs (`/chat/completions` + Responses SSE back, via the
   codex translator), and resolving a knob-less body to its model key's authored chain
   when the exact-variant and bare lookups miss (openai's Responses wire carries no
   effort knob; workers inherit their base model's chain) — so the session lands on a
   fallback instead of dying, and
   opencode never enters its same-model retry loop. A **`wait`** sub-block
   (`{maxWaitHours, probeSeconds?}`) opts into wait-for-the-limit-reset: at true chain
   exhaustion on a classified wall the proxy emits a retryable `429 + Retry-After`
   (the wall's own header when it parses and fits `maxWaitHours`, else `probeSeconds`,
   body verbatim) instead of the verbatim error, and opencode's own session retry
   bridges the window; with `wait` present `chains` becomes optional (the wait-only
   shape — an unmapped model walks its primary alone instead of transparent-forwarding,
   so a wall is waitable there too), and the first primary 200 after a wait logs
   `wait cleared`. Absent key → no proxy, byte-identical
    config (the rollback switch); without `wait` every failover behaviour is
   byte-identical too. Engines older than the release introducing `wait` silently drop
   an authored `failoverIntent.wait` key (unknown intent keys are ignored) — the
   launcher runs without wait, no error; same documented-minimum pattern as the 0.61
   window. Detail: `docs/harnesses/opencode.md#failover`.
   **Prompt templates**: an abstract included YAML fragment may define top-level
   `promptTemplates` names→strings; the launcher supplies `config.promptVars` defaults
    and an agent opts in with `prompt: {_template: name, _vars: {ID: value}}`;
    `_vars` is optional and string-only, overriding `config.promptVars`. Legacy agent
    `promptTemplate`/`promptVars` remain accepted but cannot mix with nested metadata.
    Resolve after merge/model expansion for print and launch; strip synthetic keys,
    leaving ordinary literal prompts untouched and inputs unmodified.
   Print retains `agentAppend` separately; launch folds it into the final prompt.
    Details: `docs/harnesses/opencode.md#prompt-templates`.
    **Permission templates**: an abstract included fragment may define top-level
    `permissionTemplates` names→permission objects (without `task`). An opted-in agent
     supplies `permission: {_template: name, _vars: {QUESTION: allow}, task: {...}}`;
     `_vars` is optional, string-only, and overrides `config.permissionVars`. Legacy
     agent-level `permissionTemplate`/`permissionVars` remain accepted but cannot mix with
     inline metadata. The renderer attaches only the literal task map after shared actions,
     never merges arbitrary permission fields. Print and direct launch strip synthetic keys;
     plain and top-level permissions stay untouched. Include/extends can deep-merge an
     agent's literal task map before resolution: source-local task ownership needs a separate
     authoring-side check. Details: `docs/harnesses/opencode.md#permission-templates`.
- **Cline** — pure path-discovery (no flags), same shape as opencode. Project `AGENTS.md`
  is read natively at `./AGENTS.md` (Cline reads it as a cross-tool rules file), so agedum
  leaves it in place. Global `AGENTS.md` → the cross-tool path `~/.agents/AGENTS.md` (not
  under the config dir); skills → `./.cline/skills/` (project) + `<cline-config>/skills/`
  (global), where `<cline-config>` is `$CLINE_DATA_DIR` (default `~/.cline`). Skills use
  the `SKILL.cline.md` overlay; no `extra_args`. **Provider mode** (`_cline_env`) maps the
  config to Cline CLI flags (`--model` / `--provider` / `--thinking` / `--plan`, plus
  `autoApprove` → `--auto-approve <bool>` and `compaction` → `--compaction <agentic|basic|off>`,
  where `agentic` is the LLM-summarizer strategy) and passes the token via `--key` — so the
  secret lands in argv (Cline's documented mechanism), which the dry-run masks. A **`baseUrl`**
  (custom OpenAI-compatible endpoint — Kimi coding subscription, OpenCode-Go, …) takes a
  different path: Cline has no run-time base-URL flag and a `--provider`/`--model` flag set
  rebuilds the provider from flags, silently dropping the stored base URL (posting to the
  OpenAI default). So agedum **generates a single-provider `providers.json`** (Cline's generic
  `openai-compatible` provider, `baseUrl` + `model`, `lastUsedProvider`; the key is *not*
  written — it rides `--key`) and injects it via `Launch.config_files` under an isolated
  **`CLINE_DATA_DIR`** (`~/.cache/agedum/cline/<endpoint-model-sha256>`, one per endpoint+model so
  there's no Cline account to fall back to), then launches with **no** `--provider`/`--model`
  so Cline selects the stored provider (base URL intact) via `lastUsedProvider`. That
  `providers.json` is **seeded writable** straight into `CLINE_DATA_DIR` (the 4th
  `config_files` field; **not** read-only bound), because Cline rewrites it to persist its
  provider selection and a ro-bind makes that write fail with `EROFS` — agedum atomically
  re-seeds the correct endpoint config on every launch, creating mode 0600 before any secret
  bytes and refusing unsafe symlinked parents, hardlinks or non-private seed directories.
  `baseUrl` and a named `provider` are mutually
  exclusive. On the `baseUrl` path **`contextWindow`** /
  **`maxTokens`** become a one-entry `models` array in that `providers.json` — the generic
  `openai-compatible` provider has no model catalogue, so this is how Cline learns the window
  (its `X/N` meter + the point agentic compaction fires) and output cap; omitted → Cline's
  default window. `agedum --prompt`/`--run` map to
  `cline --tui "<text>"` (interactive TUI, seeded) and `cline "<text>"` (positional, run-once
  act mode).
- **reasonix** — pure path-discovery (no flags), same shape as opencode/cline.
  [DeepSeek-Reasonix](https://github.com/esengine/DeepSeek-Reasonix) reads the project
  `AGENTS.md` natively (one of its memory docs `REASONIX.md` / `AGENTS.md` / `CLAUDE.md`),
  so agedum leaves it in place. Global `AGENTS.md` → `~/.config/reasonix/AGENTS.md` (its
  user-scope memory dir); skills → `./.reasonix/skills/` (project) + `~/.reasonix/skills/`
  (global). reasonix scans `.reasonix` / `.agents` / `.agent` / `.claude` (each `/skills`)
  under the project and home dirs, highest-priority first, and `.reasonix` leads, so the
  overlaid (`SKILL.reasonix.md`) copy wins over the raw source; no `extra_args`. **Provider
  mode** (`_reasonix_env`) maps `model` → `--model <name>` on the `chat` / `run` subcommand
  and exports the token (reasonix reads it via the provider's `api_key_env`, e.g.
  `DEEPSEEK_API_KEY`). A `baseUrl` (no native flag/env on reasonix) makes agedum **generate a
  `reasonix.toml`** `[[providers]]` block + `default_model` and inject it at the project root
  via `Launch.config_files` (the launcher writes it; the key is referenced by env-var name,
  never written); reasonix's merge replaces `[[providers]]` wholesale but keeps the user
  config's scalars + plugins, so the custom provider wins without masking other settings. Here
  `model` is the upstream model id; agedum names the provider `agedum` and runs `--model agedum`.
  The same generated-toml path also carries **two-model routing** — `subagentModel` /
  `plannerModel` / `autoPlan` → an `[agent]` section — and a **`providerDef`** list (one or more
  `{id, kind, baseUrl, model, apiKeyEnv}` → `[[providers]]` blocks, each `apiKeyEnv` auto-required);
  when every referenced model is a built-in, no `[[providers]]` is emitted so the built-ins survive.
  `agedum --run` maps to `reasonix run "<text>"`; `--prompt` is a fail-loud `ProviderError`
  (`chat` can't be pre-seeded).
- **aider** — the odd one out. aider has **no native instruction discovery** (it reads neither
  `AGENTS.md` nor `CONVENTIONS.md` itself) and **no skills mechanism**, so `compile_aider`
  injects each scope's `AGENTS.md` via aider's `--read` read-only-context flag (project then
  global), read-only bound to content-addressed `~/.cache/agedum/aider-instructions/` paths
  so context remains visible when sandbox `/tmp` is masked. It injects **no skills** (no
  `SKILL.aider.md`). There is an `AGENTS.aider.md` instruction overlay
  (user scope). **Provider mode** (`_aider_env`) maps the config to aider CLI flags — `model`
  → `--model`, `weakModel`/`editorModel` → `--weak-model`/`--editor-model`, `reasoningEffort`
  → `--reasoning-effort`, `yesAlways` → `--yes-always` — and a `baseUrl` sets `OPENAI_API_BASE`
  (OpenAI-compatible endpoint; the key reaches aider through litellm via the `requiredEnv`
  export, never argv). **Git integration is disabled by default**: agedum's namespace shares
  the real `.git` and aider auto-commits, so `_aider_env` appends `--no-git` unless `git: true`
  (then `autoCommits: false` → `--no-auto-commits`). Wrapper mode runs the literal command and
  does **not** force `--no-git` (documented caveat). `agedum --run` maps to `aider --message
  "<text>"`; `--prompt` is a fail-loud `ProviderError` (`--message` runs once and exits).
- **pi** — the earendil-works [pi](https://pi.dev) agent (`@earendil-works/pi-coding-agent`).
  Pure path-discovery like opencode/cline/reasonix: `compile_pi` leaves the project `AGENTS.md`
  in place (pi walks cwd→root for `AGENTS.md`/`CLAUDE.md`), binds the global `AGENTS.md`
  (+ optional `AGENTS.pi.md` overlay) to `~/.pi/agent/AGENTS.md` (its `getAgentDir()`,
  `$PI_CODING_AGENT_DIR`-aware), and binds skills (`SKILL.pi.md` overlay) to `./.pi/skills/`
  (project) + `~/.pi/agent/skills/` (global). No `extra_args`. **Provider mode** (`_pi_env`)
  maps `model` → `--model`, `provider` → `--provider`, `thinking` → `--thinking`; the key
  reaches pi by its conventional env-var name via the `requiredEnv` export (never argv). pi has
  **no base-URL flag**, so a `baseUrl` makes agedum generate `~/.pi/agent/models.json` (a
  provider named `agedum`, `apiKey` referenced by `$VAR`, `api` default `openai-completions`,
  model selected as `agedum/<id>`) — the reasonix.toml analog. For a **cross-provider**
  multi-agent (executor + subagents on different endpoints, e.g. Kimi executor + DeepSeek-flash
  subagents), `providerDef` (a single object or a **list** of `{id, api, baseUrl, model,
  apiKeyEnv}`) emits one `models.json` provider block each; `model`/`subagentModel` are then pi
  `provider/id` patterns passed through verbatim. `baseUrl` and `providerDef` are mutually
  exclusive; each `apiKeyEnv` is auto-required + referenced by `$VAR`. A `subagentModel` generates
  `~/.pi/agent/settings.json` `subagents.agentOverrides` routing every [pi-subagents] built-in
  agent (scout/researcher/planner/worker/reviewer/context-builder/oracle/delegate) to one model
  (the opencode-flash / reasonix-`subagentModel` analog). Both generated files are **merged**
  onto any existing ones (not masked) via the user-scope `config_files` path. **`piSettings`** is
  a generic escape hatch: a JSON object deep-merged into the generated `settings.json` (any
  settings-based extension; `subagentModel` is sugar composed into the same fragment, `piSettings`
  winning on conflict). **`piExtensionConfig`** ({relpath → object}) reaches an extension's **own
  file** under `~/.pi/agent` (e.g. pi-subagents' `parallel`/`async` in
  `extensions/subagent/config.json`) — each entry deep-merged onto that file; paths must stay
  under `~/.pi/agent` (no `..`/absolute) and the agedum-managed `settings.json`/`models.json` are
   rejected. **`config.requireExtensions`** (+ implicit `pi-subagents` when `subagentModel`/
  `piSettings.subagents` is set) warns at launch — via `Launch.warnings` — when a needed extension
   is absent from the host (`settings.json packages` / `~/.pi/agent/npm/node_modules`); `config.strict:
  true` makes it fail-loud. agedum never installs (a host action). `agedum --prompt` seeds
  `pi "<text>"` (interactive); `--run` maps to `pi --print "<text>"`.

  [pi-subagents]: https://pi.dev/packages/pi-subagents
- **codex** — the OpenAI [Codex CLI](https://github.com/openai/codex) (`@openai/codex`). Pure
  path-discovery like opencode/cline/reasonix/pi: `compile_codex` leaves the project `AGENTS.md`
  in place (codex walks work-dir→root for `AGENTS.md`), binds the global `AGENTS.md` (+ optional
  `AGENTS.codex.md` overlay) to `~/.codex/AGENTS.md` (`$CODEX_HOME`-aware, `codex_config_dir()`),
  and binds skills (`SKILL.codex.md` overlay) to `./.codex/skills/` (project) + `~/.codex/skills/`
  (global). No `extra_args`. **Provider mode** (`_codex_env`) maps `model` → `-m`; the key reaches
  codex by its conventional env-var name via the `requiredEnv` export (never argv). codex has **no
  base-URL flag**, so a `baseUrl` is passed as `-c` overrides defining a `[model_providers.agedum]`
  block (`base_url` + `env_key` = `secretEnv`; `wireApi` emitted only when set) selected with
  `-c model_provider=agedum` — codex parses each `-c` value as TOML, so no file is generated for
  the endpoint. Recent codex speaks **only the Responses API** (`wire_api = "chat"` removed Feb
  2026), so a Chat-Completions endpoint (DeepSeek etc.) sets **`chatCompletions: true`**:
  `_codex_env` emits `AGEDUM_CODEX_CHAT_UPSTREAM`, and at launch `cli.main._maybe_codex_proxy`
  interposes a `ResponsesToChatProxy` (`proxy.py`, the `FoldProxy` sibling) — codex speaks
  Responses to the proxy, which translates to/from `/chat/completions` upstream — rewriting the
  `base_url` override to the proxy address. The proxy surfaces a thinking model's streamed
  `delta.reasoning_content` (Kimi K2.7) as a Responses `reasoning` item so codex renders it.
  **`codexConfig`** is a table of arbitrary codex config keys → `-c key=<toml>` overrides (bool/int
  bare, else quoted); nested tables flatten to dotted keys (e.g.
  `sandbox_workspace_write.writable_roots`), the same shape the `mcpServers` translation emits —
  carries metadata codex can't learn from a translated endpoint, chiefly
  `model_context_window` (context-meter denominator; the `/models` probe is answered empty) and
  `model_supports_reasoning_summaries` / `model_reasoning_summary` (enable reasoning rendering).
  **`mcpServers`** — the canonical cross-harness key now works for codex: each stdio/remote
  server is emitted as `-c mcp_servers.<name>…` dotted-key TOML overrides, merged onto
  `~/.codex/config.toml`; `${VAR}` placeholders are rejected (codex is not known to expand them
  in config values).
  **`codexModelCatalog`** (`{contextWindow, displayName?, description?}`) makes codex fully
  *recognise* a custom model: agedum runs `codex debug models`, clones its first entry (for
  version-correct `base_instructions`) as this `model`, writes `~/.codex/agedum-model-catalog.json`,
  and passes `-c model_catalog_json=<path>` — silencing the "metadata not found" warning and
  lighting the context meter (which reads the window from the *catalog*, not `model_context_window`).
  Skipped gracefully if `codex debug models` can't be queried. codex custom agents are standalone TOML files the
  primary delegates to — agedum binds them three ways: `subagentModel` (sugar for one fast
  `~/.codex/agents/flash.toml`), `codexAgents: <dir>` (bind every `*.toml` in a providers-root
  dir into `~/.codex/agents/`, **personal** scope), and `codexProjectAgents: <dir>` (into
  `.codex/agents/`, **project** scope, git-tracked-target-guarded). agedum injects a default
  `sandbox_mode = "workspace-write"` when a source omits it; duplicate targets are rejected. codex
  has no global subagent-model knob ([codex#19482]), so agents are **inert unless explicitly
  invoked**. `agedum --prompt` seeds `codex "<text>"` (interactive); `--run` maps to
  `codex exec "<text>"`.

  [codex#19482]: https://github.com/openai/codex/issues/19482
- **Global instructions overlay** — the user-scope `AGENTS.md` is merged with an optional
  sibling `AGENTS.<harness>.md` (`AGENTS.claude.md` / `AGENTS.kimi.md` /
  `AGENTS.opencode.md` / `AGENTS.cline.md` / `AGENTS.reasonix.md` / `AGENTS.aider.md` /
  `AGENTS.pi.md` / `AGENTS.codex.md`) for the active harness — the instructions analogue of
  `SKILL.<harness>.md`. `AGENTS.md` has no front-matter, so the merge is a body
  concatenation (base, blank line, overlay). **User scope only** — the project `AGENTS.md`
  takes no overlay (for kimi/opencode it is read natively, never injected).

Follow-ups: `--<harness>-variant` composition.

## Stack

- Python ≥ 3.12, managed with **uv** (`uv sync`, `uv run`). Don't use raw pip/venv.
- **Manual `argv` parsing** (not Typer) so everything after `--` is opaque
  passthrough; Rich for stderr output. Entry point: `agedum.cli.main:app`
  (`[project.scripts]`). Deps: `pyyaml` (skill frontmatter merge), `rich`.
- Runtime dep: **`bwrap`** (bubblewrap) on PATH for the virtual-FS launch. Linux-only.
- Flat package layout: the `agedum/` package sits at the repo root (no `src/`).
- Version is **dynamic via hatch-vcs** — derived from the git tag `vX.Y.Z` at build
  time, never committed. A source tree with no tag resolves to a dev version;
  `agedum.__version__` falls back to `0.0.0` when the package isn't installed.

## Commands

```bash
make dev-install   # uv sync --all-groups
make test          # uv run pytest
make lint          # ruff check + ruff format --check
make format        # ruff --fix + format
make run ARGS="--version"
make docs           # build docs site (strict); docs-serve for live preview
```

Run `make format` after every change. Commit `uv.lock`; `.venv/` stays gitignored.

Docs are an MkDocs Material site under `docs/` (+ `mkdocs.yml`), published to
`agedum.vcoeur.com` via GitHub Pages. Source shape, scopes, and per-harness behaviour
are documented there — keep `docs/` in sync when the source layout or a compiler changes.

## CI / release

- `.github/workflows/ci.yml` — ruff lint + format-check + pytest on push to `main`
  and every PR.
- `.github/workflows/release.yml` — on a `v*` tag push, `uv build` then publish to
  **PyPI** via OIDC trusted publishing (no token in the repo). Tag only after merge.
  Publish is idempotent (`skip-existing: true`): re-pushing an already-released tag
  is a no-op success, not a "File already exists" failure.
- `.github/workflows/docs.yml` — on push to `main` touching `docs/**` or `mkdocs.yml`,
  build the site with `mkdocs build --strict` and deploy to GitHub Pages.
- **The `[build-system]` backends are pinned on purpose — don't float them.** `uv.lock`
  does not lock build backends, so an unpinned `hatchling` resolves to whatever is newest
  at build time, and the wheel's `Metadata-Version` can change without any source change
  (1.31.0 → 2.4, 1.32.0 → 2.5). A publish validator that predates 2.5 then rejects the
  upload *after* every other gate has passed, so the failure only ever surfaces at tag
  time. Verify with `uv build` + reading the wheel's `METADATA` before tagging.

## CLI contract

Two modes, dispatched in `cli/main.py` on the first argument:

- **provider** (primary) — `agedum <config-ref> [--env <file>] [--dry-run] [--print-config] [harness args...]`.
  Read a condash-style provider config — **JSON** (the legacy format, no version key) or
  **YAML** (a document declaring `schema: agedum-provider/v1` or `agedum-provider/v2`;
  missing or different is a `ProviderSchemaError` naming the expected value, and a correct
  key is stripped so YAML yields
  the same dict the equivalent JSON would — parse, not translate). An unquoted YAML
   `on`/`off`/`yes`/`no` in a string-valued slot (env values, `secretEnv`/`requiredEnv` entries,
   `harness`, `extends`/`include` refs, the known string keys of `config`) is a `YamlBooleanTrapError` —
  quote the value. The reference resolves **relative to the providers
  root** (`<providers_dir>/<ref>`; no recognised extension → `.json`/`.yaml`/`.yml`; an explicit
  `.json` that does not exist falls back to its `.yaml` sibling, so a converted YAML base keeps
  its old JSON referrers working; explicit `.yaml`/`.yml` as-is), or absolute when it starts with
  `/`; nested paths are allowed (`agedum claude/deepseek.json`) and not-found is an error (no CWD
   fallback). A config may **`extends`** one or more bases (a string or list, same resolution):
   bases are deep-merged left→right and the child applied last (recursive; cycles error), except
   **`requiredEnv`, which unions** down the chain — a plain list-replace would silently drop a
   base's requirement the moment a child declared its own. A config may also **`include`** one
   or more shared fragments (a string or list, same resolution) — composition, not inheritance:
   merge order for one file is most-default first, every include target (recursively resolved)
   deep-merged left→right (earlier include = more default), then the extends chain (a base's
   keys beat an included fragment's on conflict), then the file's own keys; `requiredEnv` unions
   across all three layers; cycles are detected across the combined include+extends graph (a
   file reached twice through different paths is fine); `include` is a meta key like `extends`,
   stripped from the merged result. An optional **model catalogue** kills the inline-catalog
   boilerplate: `<providers_root>/models.yaml` (fixed filename; YAML-only, declaring
   `schema: agedum-models/v1` — a sibling `ModelCatalogSchemaError` otherwise) holds verbatim
   per-model fragments in opencode's own catalog vocabulary, and inside `config.providerDef` a
   `modelRef: <model-id>` (or list) on an entry expands after include/extends merging, before
   launch building, to that entry filed under
   `config.opencodeConfig.provider.<id>.models.<model-id>` (an authored inline entry wins on
   conflict; the `modelRef` key is consumed). Opencode-only (claude/codex need none today — a
   `modelRef` elsewhere fails loudly); a config with no `modelRef` and no `modelsCatalog`
   never reads the catalogue (zero behaviour change). `modelsCatalog: <ref>` (top-level meta
   key, resolved like include, `.yaml` only) points at an alternative catalogue; the roster
   (`--providers`) skips the exact root-level `models.yaml` (a subdirectory one stays an
   ordinary candidate). Catalogue entries are type-checked minimally (name string, attachment
   boolean, limit integers-or-null, modalities string-lists) naming the model id and key; no
   boolean-trap walk on the catalogue in v1. The catalogue may also carry an **optional
   `carrierMeta` section** (schema stays `agedum-models/v1` — a permissive extension 0.60
   engines ignore correctly): per-model expansion facts — `provider`/`family`/`efforts`/
   `display`, plus `aliases` + `alias_model_id` (required iff `aliases`) for model-alias
   families — validated whenever the catalogue loads, read by nothing on the v1 path
   (`modelRef` filing stays byte-identical). A YAML config may declare
   **`schema: agedum-provider/v2`** — the effort-carrier expansion opt-in: agent entries
    and `config.model` may declare `model: <catalogue-key>@<effort>` refs (`high`,
    `medium`, `low` in canonical order, only when listed in that model's
    `carrierMeta.efforts`), and an optional
   top-level `expansionModels` list (consumed like `modelsCatalog`) declares universe
   members with no agent entry (failover-only models). The engine derives the
   carrier-specific output from `carrierMeta` — `options.reasoningEffort` (deepseek/glm),
   `variant` plus the derived OpenCode variant disable-map over the config's declared
   efforts (gpt), Kimi alias selection with `options.thinking.effort` and the low-alias
   `{id, name: "<display> (low thinking)"}` redirect (kimi) — filing derived catalog
   entries under `opencodeConfig.provider.<carrierMeta provider>.models` in
   first-appearance order of the config's refs, merged **under** what `modelRef` filed or
   the author wrote inline. The **root document's** schema gates expansion (a v1 base
   under a v2 root expands; a v2 base under a v1 root does not); JSON documents are v1
   semantics forever (v2 is YAML-only). A v1 config carrying intent markers (`@`-refs on
   opencode model slots / `expansionModels` / `failoverIntent`) is a named load error
   telling the author to declare v2; a v2 config with intent on a non-opencode harness is
   refused (modelRef's opencode-first rule), and a v2 config with no markers is a no-op
   (declaring v2 alone is not intent). A v2 config may also carry a top-level
   **`failoverIntent`** block — `detect` / `maxWalk` / `chains`, sources and rungs as
   explicit `key@effort` refs — which the engine derives, from the config's own agents,
   into the effective top-level `failover` block (the intent key is consumed like
   `expansionModels`): `mode: primary` agents are the mains, `mode: subagent` the
   workers, any other or absent mode neither, and a plain `provider/model` agent
   contributes no pair; every intent ref must resolve before any filtering (unknown
   key/effort/model is a named expansion error); chain sources outside mains ∪ workers
   drop, chains left without rungs drop, and zero surviving chains omit the whole block
   (absence means ignore, never an error); surviving chains' rung refs join the expansion
   universe after `expansionModels` — a rung-only model files without `expansionModels` —
    and translate per carrier (`provider/key@effort`; kimi rungs to the bare
   `provider/<aliases[effort]>`); `rungOptions` carries `{"reasoning_effort": effort}`
   (snake_case) for the used variant/reasoningEffort rungs in canonical effort order;
   `vision` derives from the catalogue's `carrierMeta.vision` facts (one entry per
   universe model, walked provider-major, alias entries per declared effort; a missing
   fact is a named error). `detect`/`maxWalk` are copied verbatim, never validated at
   expansion — launch-time `failover_spec` polices the emitted block. Collision rules: a
   v2 document carrying both `failoverIntent` and a precomputed `failover` block is a
   named expansion error; a v2 document with only the precomputed block passes it through
   untouched. Expansion
   runs after modelRef filing, before launch building, so both `--dry-run` and
   **`--print-config`** (print the effective merged+expanded config as YAML, exit 0, no
   launch, no env resolution — accepted before or after the provider like `--dry-run`)
   show the effective result. An **`abstract: true`** config is a base only — excluded from `--providers`, refuses direct launch;
   abstractness is not inherited (through extends or include); shared fragments carry it to stay
   out of `--providers`. A config's **identity/label is its path** (the `name` field is
   gone). Then resolve the env from `${AGENTS_ENV_FILE:-~/.config/agents/.env}` (or `--env`),
  validate `requiredEnv`, set the provider/model/auth env in `os.environ`, and run the same
  virtual-FS launch as wrapper mode. The harness is read **from the config**; no `--harness` flag.
  `--dry-run` prints the resolved env (secrets masked), the config's **source format**
  (`source     yaml` / `json`), the injected virtual files, and the argv.
  An optional top-level `sandbox` field (`{readWrite: [...]}`) requests the same write-confinement
  as wrapper `--sandbox`. `config.mcpServers` is a **canonical cross-harness** key: one stdio
  (`command`/`args`/`env`/`cwd`) or remote (`url`/`headers`/`transport`) vocabulary, translated
  per harness — claude gets `--mcp-config '<json>'` (additive; never `--strict-mcp-config`),
  opencode gets an `mcp` block merged **before** `opencodeConfig` so the passthrough still wins,
  codex gets `-c mcp_servers.<name>…` overrides (dotted-key TOML; `${VAR}` rejected),
  kimi keeps its older verbatim `mcp.json` passthrough. A `${VAR}` value is **respelled, never
  resolved** (claude verbatim, opencode `{env:VAR}`), so no token reaches argv, the config
  documents, or `--dry-run`; kimi rejects a placeholder outright since it is not known to expand
  one. `config.settings` (claude only) is the flag-shaped sibling: a settings document passed as
  `--settings '<json>'`, an **additional** layer merged over the user's own `settings.json` rather
  than replacing it. Both flag-shaped keys survive the **native** (no-`baseUrl`) path, which sets
  no env at all — so `settings: {"model": …}` is how a native launcher pins its default model,
  since the `model` → `ANTHROPIC_MODEL` mapping needs an endpoint. This is the primary,
  user-facing entry.
- **wrapper** — `agedum --wrapper <harness> [--sandbox] [--rw-dir DIR]... [--dry-run] -- <command...>`.
  The low-level entry provider mode builds on. The flag before `--` chooses the virtual-file
  context (`claude` / `kimi` / `opencode` / `cline` / `reasonix` / `aider` / `pi` / `codex`);
   everything after `--` is the child argv (aider appends `--read` per scope, pointing at
   read-only bound cache paths; the other harnesses use native discovery and binds).
  `--sandbox` switches to **write-confinement** (read-only host; `--rw-dir DIR`, repeatable,
  adds a writable dir and implies `--sandbox`). `--dry-run` prints the injected virtual files
  (and, under `--sandbox`, the writable set) without running. Context and command are decoupled.

Auxiliary first-argument flags (handled in `app()` before the two-mode dispatch, like
`--version`): **`--providers`** prints every launchable config under `providers_dir()`
(walked **recursively** over `*.json` / `*.yaml` / `*.yml`; `abstract` bases skipped; ids are
extension-stripped and when both extensions exist for one stem the `.json` file is the one
listed) as `path  harness  model` — the path
relative to the root, e.g. `claude/deepseek` (via `provider.list_providers` →
`_run_list_providers`), honouring `$AGENTS_PROVIDERS_DIR`; a config that won't parse or
resolve is listed with its error, never fatal.

Module layout: `sources.py` (locate the source), `harness.py` (`compile_claude` /
`compile_kimi` / `compile_opencode` / `compile_cline` / `compile_reasonix` / `compile_aider` / `compile_pi` / `compile_codex` → a `Plan` of absolute binds **+ `extra_args`** for
the command), `launcher.py` (`build_bwrap_argv`, `assert_safe`, `run_virtualfs` —
appends `plan.extra_args`; an optional `Sandbox` switches the base bind to a read-only host
+ writable `writable_roots`), `provider.py` (`resolve_config_path` providers-root-anchored
with the `.json`→`.yaml` fallback / `load_config` raw — JSON, or YAML declaring
`schema: agedum-provider/v1` (or `/v2`), via `load_config_with_format` which carries the
source format + the declared schema —
+ `load_merged_config` resolving the `include` fragments + `extends` chain into one effective config /
`expand_model_refs` (catalogue filing) + `expand_carrier_refs` (`agedum-provider/v2`
intent expansion, gated by the root document's schema) /
`parse_env_file` / `build_launch` → a `Launch` of env-to-set/unset + base command;
`list_providers` walks recursively + skips `abstract` → `ProviderSummary` rows for `--providers`;
per-harness env mapping mirrors condash's pre-4.0 launcher), `proxy.py` (three localhost reverse
proxies sharing one `_BaseProxyHandler` transport skeleton + `_LocalProxy` lifecycle: the claude
`FoldProxy` (`foldSystemMessages`) and `TranslateProxy` (`upstreamApi: openai-completions`,
Anthropic⇄OpenAI), and the codex `ResponsesToChatProxy` that translates the Responses API ⇄ Chat
Completions for chat-only providers), `cli/main.py` (parse + `_COMPILERS` dispatch + `_run_config`
/ `_run_wrapper` / `_run_list_providers`; `_maybe_proxy` interposes the claude proxies via
`ANTHROPIC_BASE_URL`, `_maybe_codex_proxy` interposes the codex proxy by rewriting the `base_url`
override).

## Proxy admission and diagnostic safety

- Provider launches clear inherited agedum-owned proxy control switches before installing
  their current protocol settings; do not scrub unrelated user/auth environment variables.
- A v2 default-model effort becomes model-level `options.reasoningEffort` for DeepSeek,
  GLM and GPT; explicit agent options/variants still override it. Variant preservation uses
  effective modeled/native agent selections: modeled rows first, native deep-merge last,
  exactly as the runtime builder does. Kimi keeps alias routing.
- Codex custom-agent TOML is structurally parsed; only an absent root `sandbox_mode` receives
  a prepended default. Generated TOML strings escape C0 controls and DEL.
- Kimi/Cline state uses a SHA-256 identity of the exact endpoint/model pair, preserving case
  and punctuation. The same pair intentionally shares state even with different credentials,
  MCP settings or effort. Old slug directories are not migrated automatically.
- Failover validates every non-null declaration, including empty/falsy malformed values and
  wrong-harness declarations. Built-in OpenAI routing seeds include the effective default and
  modeled/native agent selections. Wait caps must be finite positive numbers; no total elapsed
  deadline or new numeric ceiling is imposed. Dotenv quoting/comments are parsed without shell
  evaluation; malformed quoted suffixes fail without exposing the value.
- Every local proxy requires its own random 256-bit launch capability in
  `X-Agedum-Proxy-Capability`, checked in constant time before reading a body, answering a
  local probe, or contacting any upstream. Loopback is not authorization; browser `Origin`
  is rejected as additional defense. Duplicate/missing/wrong capability headers fail closed.
- Keep provider keys and OAuth authentication separate from admission. Claude adds the
  capability to runtime-only `ANTHROPIC_CUSTOM_HEADERS`; Codex uses an `env_http_headers`
  override naming `AGEDUM_PROXY_CAPABILITY` (never its value in argv); OpenCode routed
  `options.headers` reference that runtime env var. Restore the temporary env on exit.
  Never persist, print or log the capability. Strip it before forwarding; remove all
  case-insensitive authorization/API-key fields before installing a fallback rung's key.
- Responses translation requires both a successful finish reason and `[DONE]` before
  emitting `response.completed` or finalizing tool arguments. EOF/length/filter produce
  `response.incomplete`; upstream errors, malformed streams and invalid tool arguments
  produce `response.failed`. Apply the same converter to Codex and OpenCode translated rungs.
  Validate every chunk at one typed boundary before mutating output/lifecycle: choices and
  deltas, nullable text/refusal/identity/name/argument fragments, optional role/tool-type/finish
  enums, integer indices and usage counters. Missing fields and protocol-valid null/empty
  metadata remain accepted; optional tool containers may be null, never coerced from other
  falsy types. Unused vendor metadata stays unrestricted. Non-empty refusal or unsupported
  legacy function-call output fails explicitly rather than becoming an empty successful turn.
  A non-null finish reason cannot later change; repeated/null finish metadata remains valid.
  A prior successful finish never makes a later malformed frame acceptable.
- Passthrough and Responses SSE use `HTTPResponse.read1` so a flushed upstream payload
  reaches the client before EOF, including Content-Length and chunked framing. Responses
  translation negotiates exactly one case-insensitive `Accept-Encoding: identity`; a
  non-identity upstream `Content-Encoding` is an explicit 502, never parsed as SSE.
- Anthropic translation buffers tool fragments per upstream index, then emits each complete
  block lifecycle sequentially after streamed text. Parallel `0,1,0` deltas must retain every
  argument fragment without targeting a stopped block. Tool display waits until stream end.
  Before exposing any buffered tool, require `stop`/`tool_calls`, `[DONE]`, non-empty id/name
  and complete JSON-object arguments for every call. SSE errors, malformed frames, transport
  failure, missing terminals and length/filter termination with tools emit an Anthropic
  `error` event, never tool blocks or normal `message_delta`/`message_stop`. Keep already
  streamed text; a text-only `length` plus `[DONE]` may finish with `max_tokens`. Reuse the
  typed Chat boundary and available-data SSE parser without weakening Responses validation.
- Failover selects a provider on the URL path alone and preserves the original query on
  primary and untranslated fallback hops. Responses-to-Chat translation deliberately replaces
  the route with `/chat/completions` without the source protocol's query.
- Diagnostic JSON/TOML/structured argv are parsed, redacted as typed values, then serialized.
  Preserve numeric/boolean types, env references and unrelated Unicode prompt text; required
  switch values such as `1` must not be replaced inside serialized JSON or arbitrary words.
  Unparseable generated content is withheld, never printed as a raw fallback. Diagnostics
   must not mutate the actual launch documents. Regression/runtime fixtures use fake keys and
   localhost only; `tests/test_proxy_trust.py` covers these boundaries.
   Exact string matches are masked even for one-character secrets; embedded one-character
   values are masked only in credential fields, not globally across unrelated text.

## Virtual-FS safety rules (validated empirically — don't regress)

- The namespace shares the **real `.git`**, so an in-namespace `git add`/`commit`
  writes to the real repo. `assert_safe` **refuses to inject over a git-tracked
   path**; injected targets must be untracked, with gitignore an operator prerequisite
   rather than an enforced gate. The check runs over the
  **effective per-child binds** (the paths actually mounted), so a tracked but
  unrelated sibling in a skills dir never blocks a launch it could not endanger.
- bwrap creates mountpoints on the real FS, leaving empty stubs after exit;
  `run_virtualfs` sweeps the ones it created (each target **and its parent**, deepest
  first, only if it didn't pre-exist) — including `safe_overrides` tmpfs shadows,
  whose mountpoints bwrap stubs the same way. Plain `--ro-bind`s mask any
  pre-existing dir; injected content never leaks (leftovers are 0-byte / empty).
- This is not a no-writes contract: sandbox preparation creates exact injection parents
  and harness state dirs that remain, writable Kimi/Cline provider seeds persist, and
  harness state/transcript capture can write host files. Read-only injected content is
  temporary; individual settings/config overlays stay read-only even in writable dirs.
  Claude's automatic project transcript settings overlay also binds
  `.claude/settings.local.json` read-only, so do not promise permission saves to that file.
- **Write-confinement** (`--sandbox` / a provider `sandbox` block) replaces the default
  `--dev-bind / /` (full read-write host) with `--ro-bind / /` + `--dev /dev` + `--proc /proc`
  + `--tmpfs /tmp`, then `--bind`s only `writable_roots` (launch directory + the prepared exact
  parent of every injection target + **each harness's own state/config dir** (`Plan.writable_dirs`,
  e.g. `~/.cline`, `~/.claude` — `run_virtualfs` `mkdir`s any that are missing so the bind lands)
  + the declared `read_write` paths, each glob-expanded — `*`/`?`/`[` resolves to every existing
  match, so `~/src/*` binds each child of `~/src`). Each harness declares its state dir in its
  `compile_*` so writable state can persist, not by an injection happening to land under it;
  individual settings/config binds stay read-only. Two facts the recipe depends on, both validated empirically: bwrap
  **cannot create a mount point on a read-only parent** (so every injection target's parent must
  be writable), and a `--ro-bind`/`--bind` **source resolves from the host** even when its
  path is tmpfs-shadowed in the namespace (so agedum's compiled files under `/tmp` still bind
  with `--tmpfs /tmp` active). Off by default — every existing launch is unchanged.

- Git ownership is queried from each target's actual worktree, including nested source
  roots and global targets in another repository. Unexpected Git errors refuse launch.
  Shadows receive the same guard: even read-only masking can stage tracked-file deletions.
- Pi never shadows source skills. It merges native exact `-<source SKILL.md path>`
  exclusions into untracked project `.pi/settings.json` for only the skills it compiles.
  Manual/global/package skills and unrelated settings remain enabled. Tracked or invalid
  project settings refuse launch; an owner decision is required to use an untracked layer.
- Automatic transcript capture remains enabled. Claude/OpenCode sidecars require an owned
  0700 storage directory and an owned regular, single-link 0600 file; unsafe existing paths
  are rejected without chmod or writes. Linux directory handles anchor no-follow writes.
  Claude checkpoints live in owned 0700 `agedum-claude-transcript-<uid>` storage under TMPDIR,
  keyed by session and transcript path, with exclusive locks and atomic 0600 replacement.
  A stale lock suppresses checkpoint capture until the owner removes it; harness execution
  remains best-effort and unchanged. Tests use fake hook/plugin events, never real logs.

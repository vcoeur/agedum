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
  the writable config-agents root, never straight into `~/.claude` (read-only under a sandbox), so
  agedum injects them the same way as `CLAUDE.md`/skills. `~/.claude.json` auth untouched.
- **kimi** — project `AGENTS.md` is read natively (kimi merges `AGENTS.md` from the
  project root down to the work dir into `KIMI_AGENTS_MD`), so agedum leaves it in
  place. kimi has no user-scope `AGENTS.md`, so the global `AGENTS.md` is injected via a
  transient `--agent-file` YAML (`extend: default`, `system_prompt_args.ROLE_ADDITIONAL`)
  — appended only when a global scope exists; the two coexist. Skills are binds: global
  → `~/.kimi/skills/`, project → `./.kimi/skills/` (both auto-read). Matches condash's
  prior kimi layout; uniform with the Claude harness.
- **opencode** — pure path-discovery (no flags). Project `AGENTS.md` is read natively at
  `./AGENTS.md`, so agedum leaves it in place. Global `AGENTS.md` → `<config>/AGENTS.md`;
  skills → `./.opencode/skills/` (project) + `<config>/skills/` (global), where
  `<config>` is `$XDG_CONFIG_HOME/opencode` (default `~/.config/opencode`). opencode
  searches those skills dirs before the project's raw `.agents/skills/`, so the
  overlaid (`SKILL.opencode.md`) copy wins. Matches condash's
  opencode layout; uniform with the Claude harness, no `extra_args`.
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
  **`CLINE_DATA_DIR`** (`~/.cache/agedum/cline/<endpoint-slug>`, one per endpoint+model so
  there's no Cline account to fall back to), then launches with **no** `--provider`/`--model`
  so Cline selects the stored provider (base URL intact) via `lastUsedProvider`. That
  `providers.json` is **seeded writable** straight into `CLINE_DATA_DIR` (the 4th
  `config_files` field; **not** read-only bound), because Cline rewrites it to persist its
  provider selection and a ro-bind makes that write fail with `EROFS` — agedum re-seeds the
  correct endpoint config on every launch. `baseUrl` and a named `provider` are mutually
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
  global; the instructions analogue of kimi's `--agent-file`, so `Plan.binds` stays empty and
  the compiled files are read at their real `dest` path through `--dev-bind / /`), and injects
  **no skills** (no `SKILL.aider.md`). There is an `AGENTS.aider.md` instruction overlay
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
  rejected. **`requireExtensions`** (+ implicit `pi-subagents` when `subagentModel`/
  `piSettings.subagents` is set) warns at launch — via `Launch.warnings` — when a needed extension
  is absent from the host (`settings.json packages` / `~/.pi/agent/npm/node_modules`); `strict:
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
make run -- --version
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

## CLI contract

Two modes, dispatched in `cli/main.py` on the first argument:

- **provider** (primary) — `agedum <config-ref> [--env <file>] [--dry-run] [harness args...]`.
  Read a condash-style provider config JSON. The reference resolves **relative to the providers
  root** (`<providers_dir>/<ref>`, `.json` appended if absent), or absolute when it starts with
  `/`; nested paths are allowed (`agedum claude/deepseek.json`) and not-found is an error (no CWD
  fallback). A config may **`extends`** one or more bases (a string or list, same resolution):
  bases are deep-merged left→right and the child applied last (recursive; cycles error), except
  **`requiredEnv`, which unions** down the chain — a plain list-replace would silently drop a
  base's requirement the moment a child declared its own. An
  **`abstract: true`** config is a base only — excluded from `--providers`, refuses direct launch;
  abstractness is not inherited. A config's **identity/label is its path** (the `name` field is
  gone). Then resolve the env from `${AGENTS_ENV_FILE:-~/.config/agents/.env}` (or `--env`),
  validate `requiredEnv`, set the provider/model/auth env in `os.environ`, and run the same
  virtual-FS launch as wrapper mode. The harness is read **from the config**; no `--harness` flag.
  `--dry-run` prints the resolved env (secrets masked), the injected virtual files, and the argv.
  An optional top-level `sandbox` field (`{readWrite: [...]}`) requests the same write-confinement
  as wrapper `--sandbox`. `config.mcpServers` is a **canonical cross-harness** key: one stdio
  (`command`/`args`/`env`/`cwd`) or remote (`url`/`headers`/`transport`) vocabulary, translated
  per harness — claude gets `--mcp-config '<json>'` (additive; never `--strict-mcp-config`),
  opencode gets an `mcp` block merged **before** `opencodeConfig` so the passthrough still wins,
  codex gets `-c mcp_servers.<name>…` overrides (dotted-key TOML; `${VAR}` rejected),
  kimi keeps its older verbatim `mcp.json` passthrough. A `${VAR}` value is **respelled, never
  resolved** (claude verbatim, opencode `{env:VAR}`), so no token reaches argv, the config
  documents, or `--dry-run`; kimi rejects a placeholder outright since it is not known to expand
  one. This is the primary, user-facing entry.
- **wrapper** — `agedum --wrapper <harness> [--sandbox] [--rw-dir DIR]... [--dry-run] -- <command...>`.
  The low-level entry provider mode builds on. The flag before `--` chooses the virtual-file
  context (`claude` / `kimi` / `opencode` / `cline` / `reasonix` / `aider` / `pi` / `codex`);
  everything after `--` is the child argv (some harnesses get extra flags appended — kimi's
  `--agent-file`, aider's `--read` per scope; Claude, opencode, cline, reasonix, pi, and codex
  are pure binds).
  `--sandbox` switches to **write-confinement** (read-only host; `--rw-dir DIR`, repeatable,
  adds a writable dir and implies `--sandbox`). `--dry-run` prints the injected virtual files
  (and, under `--sandbox`, the writable set) without running. Context and command are decoupled.

Auxiliary first-argument flags (handled in `app()` before the two-mode dispatch, like
`--version`): **`--providers`** prints every launchable config under `providers_dir()`
(walked **recursively**; `abstract` bases skipped) as `path  harness  model` — the path
relative to the root, e.g. `claude/deepseek` (via `provider.list_providers` →
`_run_list_providers`), honouring `$AGENTS_PROVIDERS_DIR`; a config that won't parse or
resolve is listed with its error, never fatal.

Module layout: `sources.py` (locate the source), `harness.py` (`compile_claude` /
`compile_kimi` / `compile_opencode` / `compile_cline` / `compile_reasonix` / `compile_aider` / `compile_pi` / `compile_codex` → a `Plan` of absolute binds **+ `extra_args`** for
the command), `launcher.py` (`build_bwrap_argv`, `assert_safe`, `run_virtualfs` —
appends `plan.extra_args`; an optional `Sandbox` switches the base bind to a read-only host
+ writable `writable_roots`), `provider.py` (`resolve_config_path` providers-root-anchored /
`load_config` raw + `load_merged_config` resolving the `extends` chain into one effective config /
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

## Virtual-FS safety rules (validated empirically — don't regress)

- The namespace shares the **real `.git`**, so an in-namespace `git add`/`commit`
  writes to the real repo. `assert_safe` **refuses to inject over a git-tracked
  path**; injected targets must be untracked + gitignored. The check runs over the
  **effective per-child binds** (the paths actually mounted), so a tracked but
  unrelated sibling in a skills dir never blocks a launch it could not endanger.
- bwrap creates mountpoints on the real FS, leaving empty stubs after exit;
  `run_virtualfs` sweeps the ones it created (each target **and its parent**, deepest
  first, only if it didn't pre-exist) — including `safe_overrides` tmpfs shadows,
  whose mountpoints bwrap stubs the same way. Plain `--ro-bind`s mask any
  pre-existing dir; injected content never leaks (leftovers are 0-byte / empty).
- **Write-confinement** (`--sandbox` / a provider `sandbox` block) replaces the default
  `--dev-bind / /` (full read-write host) with `--ro-bind / /` + `--dev /dev` + `--proc /proc`
  + `--tmpfs /tmp`, then `--bind`s only `writable_roots` (project root + the nearest existing
  ancestor of every injection target + **each harness's own state/config dir** (`Plan.writable_dirs`,
  e.g. `~/.cline`, `~/.claude` — `run_virtualfs` `mkdir`s any that are missing so the bind lands)
  + the declared `read_write` paths, each glob-expanded — `*`/`?`/`[` resolves to every existing
  match, so `~/src/*` binds each child of `~/src`). Each harness declares its state dir in its
  `compile_*` so persistence (sessions/settings/auth) works **by design**, not by an injection
  happening to land under it. Two facts the recipe depends on, both validated empirically: bwrap
  **cannot create a mount point on a read-only parent** (so every injection target's parent must
  be writable), and a `--ro-bind`/`--bind` **source resolves from the host** even when its
  path is tmpfs-shadowed in the namespace (so agedum's compiled files under `/tmp` still bind
  with `--tmpfs /tmp` active). Off by default — every existing launch is unchanged.

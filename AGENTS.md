# agedum — operating instructions

A Python CLI that drives any agent CLI from an agent-neutral source shape
(`AGENTS.md` + `.agents/skills/`), compiling per harness and injecting it via a
private mount namespace at launch. Implemented: **Claude**, **kimi**, **opencode**,
**Cline**, and **reasonix** harnesses at **project + global scope**.

- **Claude** — each scope at its *own* location: project → `./CLAUDE.md` +
  `./.claude/skills/`; global (`~/.config/agents/AGENTS.md` + `~/.agents/skills/`) →
  `~/.claude/CLAUDE.md` + `~/.claude/skills/` (`$CLAUDE_CONFIG_DIR`-aware), never merged
  (only those two `~/.claude` paths overlaid; `~/.claude.json` auth untouched).
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
  searches those skills dirs before `.agents/skills/` / `~/.agents/skills/`, so the
  overlaid (`SKILL.opencode.md`) copy wins over the raw source. Matches condash's
  opencode layout; uniform with the Claude harness, no `extra_args`.
- **Cline** — pure path-discovery (no flags), same shape as opencode. Project `AGENTS.md`
  is read natively at `./AGENTS.md` (Cline reads it as a cross-tool rules file), so agedum
  leaves it in place. Global `AGENTS.md` → the cross-tool path `~/.agents/AGENTS.md` (not
  under the config dir); skills → `./.cline/skills/` (project) + `<cline-config>/skills/`
  (global), where `<cline-config>` is `$CLINE_DATA_DIR` (default `~/.cline`). Skills use
  the `SKILL.cline.md` overlay; no `extra_args`. **Provider mode** (`_cline_env`) maps the
  config to Cline CLI flags (`--model` / `--provider` / `--thinking` / `--plan`) and passes
  the token via `--key` — so the secret lands in argv (Cline's documented mechanism), which
  the dry-run masks. `baseUrl` is rejected (Cline has no base-URL flag). `agedum
  --prompt`/`--run` map to `cline --tui "<text>"` (interactive TUI, seeded) and
  `cline "<text>"` (positional, run-once act mode).
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
- **Global instructions overlay** — the user-scope `AGENTS.md` is merged with an optional
  sibling `AGENTS.<harness>.md` (`AGENTS.claude.md` / `AGENTS.kimi.md` /
  `AGENTS.opencode.md` / `AGENTS.cline.md` / `AGENTS.reasonix.md`) for the active harness — the instructions analogue of
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
- `.github/workflows/docs.yml` — on push to `main` touching `docs/**` or `mkdocs.yml`,
  build the site with `mkdocs build --strict` and deploy to GitHub Pages.

## CLI contract

Two modes, dispatched in `cli/main.py` on the first argument:

- **provider** (primary) — `agedum <name|path> [--env <file>] [--dry-run] [harness args...]`.
  Read a condash-style provider config JSON (a bare name → `<providers_dir>/<name>.json`;
  a `/`- or `.json`-bearing value → a path), resolve the env from
  `${AGENTS_ENV_FILE:-~/.config/agents/.env}` (or `--env`), validate `requiredEnv`, set
  the provider/model/auth env in `os.environ`, then run the same virtual-FS launch as
  wrapper mode with `command = [<harness-binary>, *harness-args]`. The harness is read
  **from the config**; there is no `--harness` flag. `--dry-run` prints the resolved env
  (secrets masked), the injected virtual files, and the argv without launching. Secrets
  are read into the agedum process (not kept out as the retired `--build-script` codegen
  did). This is the primary, user-facing entry.
- **wrapper** — `agedum --wrapper <harness> [--dry-run] -- <command...>`. The low-level
  entry provider mode builds on. The flag before `--` chooses the virtual-file context
  (`claude` / `kimi` / `opencode` / `cline` / `reasonix`); everything after `--` is the child
  argv (some harnesses get extra flags appended — kimi's `--agent-file`; Claude, opencode,
  cline, and reasonix are pure binds). `--dry-run` prints the injected virtual files without
  running. Context and command are decoupled.

Module layout: `sources.py` (locate the source), `harness.py` (`compile_claude` /
`compile_kimi` / `compile_opencode` / `compile_cline` / `compile_reasonix` → a `Plan` of absolute binds **+ `extra_args`** for
the command), `launcher.py` (`build_bwrap_argv`, `assert_safe`, `run_virtualfs` —
appends `plan.extra_args`), `provider.py` (`resolve_config_path` / `load_config` /
`parse_env_file` / `build_launch` → a `Launch` of env-to-set/unset + base command;
per-harness env mapping mirrors condash's pre-4.0 launcher), `proxy.py` (the
`foldSystemMessages` reverse proxy), `cli/main.py` (parse + `_COMPILERS` dispatch +
`_run_config` / `_run_wrapper`).

## Virtual-FS safety rules (validated empirically — don't regress)

- The namespace shares the **real `.git`**, so an in-namespace `git add`/`commit`
  writes to the real repo. `assert_safe` **refuses to inject over a git-tracked
  path**; injected targets must be untracked + gitignored.
- bwrap creates mountpoints on the real FS, leaving empty stubs after exit;
  `run_virtualfs` sweeps the ones it created (each target **and its parent**, deepest
  first, only if it didn't pre-exist). Plain `--ro-bind`s mask any pre-existing dir;
  injected content never leaks (leftovers are 0-byte / empty).

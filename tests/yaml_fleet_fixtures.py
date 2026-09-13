"""Child-4 fleet fixtures: the live kimi / pi / cline launchers in both source formats.

The JSON side of every pair is copied **verbatim** from ``~/.config/agents/providers``
(agentsconf tree, 2026-09-13) so the suite pins the real fleet shapes without reading
the host tree; the YAML side is the hand-written conversion — the
``agedum-provider/v1`` schema key plus the same content, with ``extends`` refs
respelled to the ``.yaml`` siblings. The ``*specs`` builders stage each family's real
extends chain in both spellings:

* JSON side — the files as they live today: JSON children and JSON mid-bases over the
  YAML sandbox root, reached through the ``.json`` → ``.yaml`` sibling fallback.
* YAML side — the conversion shape: every file YAML.

Tests assert both sides merge to the same config and build the same
:class:`agedum.provider.Launch`.
"""

import json

CONCEPTION_SANDBOX_YAML = """\
schema: agedum-provider/v1
abstract: true
sandbox:
  readWrite:
  - ~/.cache
  - ~/.npm
  - ~/.cargo
  - ~/.local
  - ~/.config
  - ~/src/*
"""

# The pre-conversion JSON spelling of the sandbox root, for the reverse mixed chain
# (YAML child → JSON base).
CONCEPTION_SANDBOX_JSON = {
    "abstract": True,
    "sandbox": {
        "readWrite": ["~/.cache", "~/.npm", "~/.cargo", "~/.local", "~/.config", "~/src/*"]
    },
}


# --- kimi/kimi ------------------------------------------------------------------------


KIMI_JSON = {
    "extends": "base/conception-sandbox.json",
    "harness": "kimi",
    "favorite": True,
    "secretEnv": "KIMI_API_KEY",
    "requiredEnv": ["KIMI_API_KEY"],
    "config": {
        "binary": "kimi",
        "baseUrl": "https://api.kimi.com/coding/v1",
        "providerType": "kimi",
        "model": "k3",
        "subagentModel": "kimi-for-coding",
        "models": {
            "k3": {
                "contextWindow": 1048576,
                "capabilities": [
                    "thinking",
                    "always_thinking",
                    "image_in",
                    "video_in",
                    "tool_use",
                ],
                "supportEfforts": ["low", "high", "max"],
                "defaultEffort": "high",
            },
            "kimi-for-coding": {
                "contextWindow": 262144,
                "capabilities": [
                    "thinking",
                    "image_in",
                    "video_in",
                    "audio_in",
                    "always_thinking",
                    "default_thinking",
                ],
            },
        },
        "thinking": True,
        "effortLevel": "high",
        "mcpServers": {
            "context7": {
                "command": "npx",
                "args": ["-y", "@upstash/context7-mcp@latest"],
            },
            "playwright": {
                "command": "npx",
                "args": ["-y", "@playwright/mcp@latest"],
            },
        },
        "yolo": True,
    },
}


KIMI_YAML = """\
schema: agedum-provider/v1
extends: base/conception-sandbox.yaml
harness: kimi
favorite: true
secretEnv: KIMI_API_KEY
requiredEnv:
  - KIMI_API_KEY
config:
  binary: kimi
  baseUrl: https://api.kimi.com/coding/v1
  providerType: kimi
  model: k3
  subagentModel: kimi-for-coding
  models:
    k3:
      contextWindow: 1048576
      capabilities: [thinking, always_thinking, image_in, video_in, tool_use]
      supportEfforts: [low, high, max]
      defaultEffort: high
    kimi-for-coding:
      contextWindow: 262144
      capabilities: [thinking, image_in, video_in, audio_in, always_thinking, default_thinking]
  thinking: true
  effortLevel: high
  mcpServers:
    context7:
      command: npx
      args: [-y, "@upstash/context7-mcp@latest"]
    playwright:
      command: npx
      args: [-y, "@playwright/mcp@latest"]
  yolo: true
"""


def kimi_specs(to_yaml):
    """The kimi/kimi chain: child + the sandbox root (the only link)."""
    root = [("base/conception-sandbox.yaml", "yaml", CONCEPTION_SANDBOX_YAML)]
    if to_yaml:
        return root + [("kimi/kimi.yaml", "yaml", KIMI_YAML)]
    return root + [("kimi/kimi.json", "json", KIMI_JSON)]


# --- pi family ------------------------------------------------------------------------


PI_DEEPSEEK_BASE_JSON = {
    "extends": "base/conception-sandbox.json",
    "abstract": True,
    "harness": "pi",
    "secretEnv": "DEEPSEEK_API_KEY",
    "config": {
        "baseUrl": "https://api.deepseek.com",
        "api": "openai-completions",
        "thinking": "high",
    },
}


PI_DEEPSEEK_BASE_YAML = """\
schema: agedum-provider/v1
extends: base/conception-sandbox.yaml
abstract: true
harness: pi
secretEnv: DEEPSEEK_API_KEY
config:
  baseUrl: https://api.deepseek.com
  api: openai-completions
  thinking: high
"""


PI_CHILDREN_JSON = {
    "deepseek": {
        "extends": "base/pi-deepseek.json",
        "config": {
            "model": "deepseek-v4-pro",
            "modelInputs": ["text", "image"],
            "contextWindow": 1048576,
        },
    },
    "deepseek-flash": {
        "extends": "base/pi-deepseek.json",
        "config": {
            "model": "deepseek-v4-pro",
            "subagentModel": "deepseek-v4-flash",
            "modelInputs": ["text", "image"],
            "contextWindow": 1048576,
            "piSettings": {
                "subagents": {
                    "agentOverrides": {
                        "oracle": {"thinking": "xhigh"},
                        "planner": {"thinking": "high"},
                        "reviewer": {"thinking": "high"},
                        "scout": {"thinking": "low"},
                        "context-builder": {"thinking": "low"},
                    }
                }
            },
        },
    },
    "flash": {
        "extends": "base/pi-deepseek.json",
        "config": {"model": "deepseek-v4-flash"},
    },
}


PI_CHILDREN_YAML = {
    "deepseek": """\
schema: agedum-provider/v1
extends: base/pi-deepseek.yaml
config:
  model: deepseek-v4-pro
  modelInputs: [text, image]
  contextWindow: 1048576
""",
    "deepseek-flash": """\
schema: agedum-provider/v1
extends: base/pi-deepseek.yaml
config:
  model: deepseek-v4-pro
  subagentModel: deepseek-v4-flash
  modelInputs: [text, image]
  contextWindow: 1048576
  piSettings:
    subagents:
      agentOverrides:
        oracle: {thinking: xhigh}
        planner: {thinking: high}
        reviewer: {thinking: high}
        scout: {thinking: low}
        context-builder: {thinking: low}
""",
    "flash": """\
schema: agedum-provider/v1
extends: base/pi-deepseek.yaml
config:
  model: deepseek-v4-flash
""",
}


def pi_specs(name, to_yaml):
    """A pi launcher's chain: child → base/pi-deepseek → the sandbox root."""
    root = [("base/conception-sandbox.yaml", "yaml", CONCEPTION_SANDBOX_YAML)]
    if to_yaml:
        return root + [
            ("base/pi-deepseek.yaml", "yaml", PI_DEEPSEEK_BASE_YAML),
            (f"pi/{name}.yaml", "yaml", PI_CHILDREN_YAML[name]),
        ]
    return root + [
        ("base/pi-deepseek.json", "json", PI_DEEPSEEK_BASE_JSON),
        (f"pi/{name}.json", "json", PI_CHILDREN_JSON[name]),
    ]


# --- cline family ---------------------------------------------------------------------


CLINE_DEEPSEEK_BASE_JSON = {
    "extends": "base/conception-sandbox.json",
    "abstract": True,
    "harness": "cline",
    "secretEnv": "DEEPSEEK_API_KEY",
    "config": {"provider": "deepseek", "effortLevel": "xhigh"},
}


CLINE_DEEPSEEK_BASE_YAML = """\
schema: agedum-provider/v1
extends: base/conception-sandbox.yaml
abstract: true
harness: cline
secretEnv: DEEPSEEK_API_KEY
config:
  provider: deepseek
  effortLevel: xhigh
"""


CLINE_AUTO_BASE_JSON = {
    "extends": "base/conception-sandbox.json",
    "abstract": True,
    "harness": "cline",
    "config": {"autoApprove": True, "compaction": "agentic"},
}


CLINE_AUTO_BASE_YAML = """\
schema: agedum-provider/v1
extends: base/conception-sandbox.yaml
abstract: true
harness: cline
config:
  autoApprove: true
  compaction: agentic
"""


CLINE_CHILDREN_JSON = {
    "deepseek": {
        "extends": "base/cline-deepseek.json",
        "config": {"model": "deepseek-v4-pro"},
    },
    "flash": {
        "extends": "base/cline-deepseek.json",
        "config": {"model": "deepseek-v4-flash"},
    },
    "kimi-code-auto": {
        "extends": "base/cline-auto.json",
        "secretEnv": "KIMI_API_KEY",
        "config": {
            "baseUrl": "https://api.kimi.com/coding/v1",
            "model": "kimi-for-coding",
            "contextWindow": 262144,
            "maxTokens": 32768,
        },
    },
}


CLINE_CHILDREN_YAML = {
    "deepseek": """\
schema: agedum-provider/v1
extends: base/cline-deepseek.yaml
config:
  model: deepseek-v4-pro
""",
    "flash": """\
schema: agedum-provider/v1
extends: base/cline-deepseek.yaml
config:
  model: deepseek-v4-flash
""",
    "kimi-code-auto": """\
schema: agedum-provider/v1
extends: base/cline-auto.yaml
secretEnv: KIMI_API_KEY
config:
  baseUrl: https://api.kimi.com/coding/v1
  model: kimi-for-coding
  contextWindow: 262144
  maxTokens: 32768
""",
}


def cline_specs(name, to_yaml):
    """A cline launcher's chain: child → its cline base → the sandbox root."""
    root = [("base/conception-sandbox.yaml", "yaml", CONCEPTION_SANDBOX_YAML)]
    if to_yaml:
        base = CLINE_AUTO_BASE_YAML if name == "kimi-code-auto" else CLINE_DEEPSEEK_BASE_YAML
        base_rel = (
            "base/cline-auto.yaml" if name == "kimi-code-auto" else "base/cline-deepseek.yaml"
        )
        return root + [
            (base_rel, "yaml", base),
            (f"cline/{name}.yaml", "yaml", CLINE_CHILDREN_YAML[name]),
        ]
    base = CLINE_AUTO_BASE_JSON if name == "kimi-code-auto" else CLINE_DEEPSEEK_BASE_JSON
    base_rel = "base/cline-auto.json" if name == "kimi-code-auto" else "base/cline-deepseek.json"
    return root + [
        (base_rel, "json", base),
        (f"cline/{name}.json", "json", CLINE_CHILDREN_JSON[name]),
    ]


# --- staging --------------------------------------------------------------------------


def family_specs(family, name, to_yaml):
    """The chain specs for one fleet launcher, uniform across families."""
    if family == "kimi":
        return kimi_specs(to_yaml)
    if family == "pi":
        return pi_specs(name, to_yaml)
    return cline_specs(name, to_yaml)


def write_chain(root, specs):
    """Stage a providers tree from ``(rel, kind, payload)`` specs; return the child path.

    ``kind`` is ``"yaml"`` (payload written verbatim) or ``"json"`` (payload serialised).
    The child — the file a launch resolves — must come last."""
    child = None
    for rel, kind, payload in specs:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload if kind == "yaml" else json.dumps(payload))
        child = path
    return child


# The required env var each live launcher validates, with a test token.
LAUNCHER_ENV = {
    "kimi/kimi": {"KIMI_API_KEY": "sk-kimi-test"},
    "pi/deepseek": {"DEEPSEEK_API_KEY": "sk-deepseek-test"},
    "pi/deepseek-flash": {"DEEPSEEK_API_KEY": "sk-deepseek-test"},
    "pi/flash": {"DEEPSEEK_API_KEY": "sk-deepseek-test"},
    "cline/deepseek": {"DEEPSEEK_API_KEY": "sk-deepseek-test"},
    "cline/flash": {"DEEPSEEK_API_KEY": "sk-deepseek-test"},
    "cline/kimi-code-auto": {"KIMI_API_KEY": "sk-kimi-test"},
}

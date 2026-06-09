"""agedum — drive an agent CLI from the agent-neutral source shape.

agedum reads the decided source layout (a root ``AGENTS.md`` for instructions and a
skills tree — ``.agents/skills/<name>/`` per project, ``~/.config/agents/skills/<name>/``
globally) and, at launch, translates and places it for the active harness — via a private
bubblewrap mount namespace — before running the underlying agent CLI. Provider mode
(``agedum <name>``) additionally resolves a provider config JSON into the harness's
env/flags. Harnesses: claude, kimi, opencode, cline, reasonix, aider, pi.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agedum")
except PackageNotFoundError:  # running from a source tree that was never built
    __version__ = "0.0.0"

__all__ = ["__version__"]

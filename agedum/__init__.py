"""agedum — drive an agent CLI from the agent-neutral source shape.

agedum reads the decided source layout (a root ``AGENTS.md`` for instructions and a
skills tree — ``.agents/skills/<name>/`` per project, ``~/.config/agents/skills/<name>/``
globally) and, at launch,
translates and places it for the active harness before exec'ing the underlying
agent CLI. This is the initial scaffold; the launch/translate pipeline is not
implemented yet.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agedum")
except PackageNotFoundError:  # running from a source tree that was never built
    __version__ = "0.0.0"

__all__ = ["__version__"]

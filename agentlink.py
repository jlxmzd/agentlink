"""AgentLink entry point: `python agentlink.py panel` opens the management panel; agents call the bus commands."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agentlink.cli import entry  # noqa: E402

if __name__ == '__main__':
    entry()

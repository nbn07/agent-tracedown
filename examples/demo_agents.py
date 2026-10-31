"""The demo agents now live in the server package, so `tracedown up` can load them:
server/tracedown_server/demo/agents.py. This module keeps old imports working."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(_ROOT, "sdk", "python"), os.path.join(_ROOT, "server")]

from tracedown_server.demo.agents import *  # noqa: E402,F401,F403
from tracedown_server.demo.agents import ALL  # noqa: E402,F401

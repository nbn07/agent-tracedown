"""Run the demo agents and print Tracedown's diagnosis of each failure.

    python examples/run_demo.py                                  # offline, no server needed
    python examples/run_demo.py --endpoint http://localhost:7070 # send the runs to a server

To see the same runs in the web app instead: `tracedown up`.
The code lives in server/tracedown_server/demo/.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(_ROOT, "sdk", "python"), os.path.join(_ROOT, "server")]

from tracedown_server.demo.runner import main  # noqa: E402

if __name__ == "__main__":
    main()

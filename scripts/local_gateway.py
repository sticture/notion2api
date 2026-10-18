#!/usr/bin/env python3
"""Run the experimental gateway bound to loopback only."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run("app.agent_gateway.server:app", host="127.0.0.1", port=args.port, access_log=False)

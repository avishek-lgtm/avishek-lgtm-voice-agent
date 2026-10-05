"""Entry point: `python -m app.main` then open http://localhost:8000"""

import argparse
import logging
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local voice agent.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config", help="Path to agent YAML (default: config/agent.yaml)")
    args = parser.parse_args()

    if args.config:
        os.environ["AGENT_CONFIG"] = args.config
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .server import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()

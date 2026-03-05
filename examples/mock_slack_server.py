"""Run the mock Slack server used by the interrupt demo.

Run in terminal A:
    uv run python examples/mock_slack_server.py --port 8765

Queue replies from another terminal:
    curl -X POST http://127.0.0.1:8765/responses \
      -H 'content-type: application/json' \
      -d '{"text":"first human reply"}'
"""

from __future__ import annotations

import argparse

from slack_mock import MockSlackServer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mock Slack server for interrupt demos.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = MockSlackServer(host=args.host, port=args.port)
    server.print_banner()
    print("")
    print("Example reply:")
    print(
        f"""curl -X POST http://{args.host}:{args.port}/responses \\
  -H 'content-type: application/json' \\
  -d '{{"text":"yes, tighten the language"}}'"""
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down mock Slack server...")
    finally:
        server.close()


if __name__ == "__main__":
    main()

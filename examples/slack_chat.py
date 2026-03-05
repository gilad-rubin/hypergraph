"""Interactive terminal client for the mock Slack server.

Run in a second terminal while the notebook executes the graph:

    uv run python examples/slack_chat.py          # default port 8765
    uv run python examples/slack_chat.py 9000      # custom port
"""

from __future__ import annotations

import sys
import time

from slack_mock import SlackClient


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    url = f"http://127.0.0.1:{port}"
    client = SlackClient(url)

    print(f"Connected to mock Slack at {url}")
    print("Watching for messages from the graph...")
    print("When a message appears, type your reply and press Enter.")
    print("Ctrl+C to quit.\n")

    seen = 0

    try:
        while True:
            messages = client.list_messages()
            if len(messages) > seen:
                for msg in messages[seen:]:
                    print(f"\n--- graph says ---\n{msg}\n------------------")
                seen = len(messages)
                reply = input("Your reply> ")
                client.queue_response(reply)
                print(f"  (sent: {reply!r})")
            else:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()

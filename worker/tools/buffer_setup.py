"""Find your Buffer YouTube channel id + check the YouTube post fields Buffer supports.
Usage: BUFFER_API_KEY=... python -m worker.tools.buffer_setup"""
from ..core import buffer


def main():
    for org in buffer.organizations():
        print(f"Organization: {org['name']}  id={org['id']}")
        for ch in buffer.channels(org["id"]):
            print(f"   {ch['service']:<12} {ch['displayName'] or ch['name']:<30} id={ch['id']}"
                  f"{'  (queue paused)' if ch.get('isQueuePaused') else ''}")
    print("\nYouTube metadata fields Buffer accepts:", buffer.youtube_meta_fields() or "none found")
    print("\n→ Put the YouTube channel id in BUFFER_CHANNEL_ID on Render.")


if __name__ == "__main__":
    main()

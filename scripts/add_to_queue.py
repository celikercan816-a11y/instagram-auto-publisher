"""CLI to add one item to content_queue.json.

Usage examples:

  # single image, using a file already committed under media/
  python -m scripts.add_to_queue --media-type IMAGE --media media/sunset.jpg \\
      --caption "Gun batimi... #huzur #manzara" --scheduled-at "2026-09-01T19:30:00+03:00"

  # reels from an external URL
  python -m scripts.add_to_queue --media-type REELS --media https://example.com/clip.mp4 \\
      --caption "..." --scheduled-at "2026-09-02T12:00:00+03:00"

  # carousel: repeat --media for each slide (2-10)
  python -m scripts.add_to_queue --media-type CAROUSEL \\
      --media media/slide1.jpg --media media/slide2.jpg --media media/slide3.jpg \\
      --caption "..." --scheduled-at "2026-09-03T09:00:00+03:00"

A relative path (doesn't start with http) is resolved against GH_REPO/GH_BRANCH from
.env into a public raw.githubusercontent.com URL, since Instagram requires publicly
fetchable URLs. Commit the actual file under media/ yourself (or let Claude do it)
before its scheduled time comes due.
"""
import argparse

from src.config import Config
from src.queue_manager import add_item, load_queue, save_queue


def resolve_url(config: Config, media: str) -> str:
    if media.startswith("http://") or media.startswith("https://"):
        return media
    return config.media_public_url(media)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--media-type", required=True, choices=["IMAGE", "VIDEO", "REELS", "CAROUSEL"])
    parser.add_argument("--media", action="append", required=True, help="repo-relative path or full URL; repeat for CAROUSEL")
    parser.add_argument("--caption", default="", help="caption text, include hashtags inline")
    parser.add_argument("--scheduled-at", required=True, help="ISO 8601 datetime with UTC offset, e.g. 2026-09-01T19:30:00+03:00")
    parser.add_argument("--allow-duplicate", action="store_true")
    args = parser.parse_args()

    config = Config.load(require_token=False)

    if args.media_type == "CAROUSEL":
        if len(args.media) < 2 or len(args.media) > 10:
            parser.error("CAROUSEL needs between 2 and 10 --media entries")
        media_url = [resolve_url(config, m) for m in args.media]
    else:
        if len(args.media) != 1:
            parser.error(f"{args.media_type} needs exactly one --media entry")
        media_url = resolve_url(config, args.media[0])

    items = load_queue()
    try:
        item = add_item(
            items,
            media_type=args.media_type,
            media_url=media_url,
            caption=args.caption,
            scheduled_at=args.scheduled_at,
            allow_duplicate=args.allow_duplicate,
        )
    except ValueError as e:
        print(f"Reddedildi: {e}")
        return 1

    save_queue(items)
    print(f"Kuyruga eklendi: id={item['id']} scheduled_at={item['scheduled_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

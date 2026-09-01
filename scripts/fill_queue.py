"""Run daily (see .github/workflows/daily-content-fill.yml) to make sure at
least 3 ready ("pending") items exist in content_queue.json for the next 7
days -- generating images (local library first, else AI), captions and
hashtags, and running everything through content_quality before it's allowed
into the queue as "pending"."""
from src.content_planner import ensure_queue_filled


def main() -> int:
    report = ensure_queue_filled()
    print(f"Doluluk (once/sonra): {report['ready_before']} -> {report.get('ready_after', report['ready_before'])}")
    if report.get("created"):
        print(f"Yeni hazir icerik: {len(report['created'])}")
    if report.get("needs_review"):
        print(f"Kalite kontrolden gecemedi, needs_review: {report['needs_review']}")
    if report.get("deferred_person_visible"):
        print(f"Kisi-gorunur slotlar ertelendi (local composite onayi gerekiyor, bkz. scripts/prepare_person_previews.py): {report['deferred_person_visible']}")
    if report.get("quota_stopped"):
        print("Ucretsiz Hugging Face kotasi tukendi, bu calistirmada durduruldu (yarin tekrar denenecek).")
    if report.get("errors"):
        print(f"Hatalar/uyarilar: {report['errors']}")
        if not report.get("quota_stopped"):
            return 1
    if report.get("note"):
        print(report["note"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

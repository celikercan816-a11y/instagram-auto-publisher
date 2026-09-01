"""Run daily (see .github/workflows/daily-content-fill.yml) to generate the
day's quote_v1 FEED+STORY content -- see src/daily_planner.py for the full
flow. Never calls a paid provider; never force-fills below quality just to
hit the daily target counts."""
from src.daily_planner import run_daily_content_generation


def main() -> int:
    report = run_daily_content_generation()
    if report.get("status") == "no_pool":
        print(report["note"])
        return 1

    print(f"Tarih: {report['date']}")
    print(f"Feed bugüne planlandı: {len(report['feed_scheduled_today'])}, reserve'e eklendi: {len(report['feed_reserved'])}, "
          f"geçmiş saat nedeniyle atlandı: {report['feed_skipped_past_time']}")
    print(f"Story bugüne planlandı: {len(report['story_scheduled_today'])}, reserve'e eklendi: {len(report['story_reserved'])}, "
          f"geçmiş blok nedeniyle atlandı: {report['story_skipped_blocks']}")
    print(f"Tahmini Cloudflare Neuron kullanımı: {report['total_neurons']:.1f}")
    if report["needs_review"]:
        print(f"QC geçemeyen/needs_review: {len(report['needs_review'])}")
        for r in report["needs_review"]:
            print(f"  - [{r['type']}] {r['quote'][:50]}... -> {r['reason']}")
    if report["errors"]:
        print(f"Hatalar/uyarılar: {report['errors']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

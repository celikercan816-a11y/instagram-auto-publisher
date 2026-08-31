"""Run weekly (Sundays, see .github/workflows/weekly-plan.yml) to lay out next
week's 6 content slots in weekly_content_plan.json. Does not touch
content_queue.json or generate any media/captions yet -- see fill_queue.py."""
from src.content_planner import generate_weekly_plan


def main() -> int:
    plan = generate_weekly_plan()
    print(f"weekly_content_plan.json olusturuldu: {plan['week_start']} -> {plan['week_end']}")
    for item in plan["items"]:
        print(f"  {item['day']} {item['time']} [{item['content_type']}] {item['theme']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

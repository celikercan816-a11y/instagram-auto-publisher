"""Run periodically (see .github/workflows/daily-content-fill.yml, same job)
to pull Instagram Insights for recently-matured posts and, once enough
samples exist, refresh strategy_weights.json."""
from src.performance import analyze_strategy, update_history_with_performance


def main() -> int:
    updated = update_history_with_performance()
    print(f"{updated} gonderi icin performans verisi cekildi.")
    weights = analyze_strategy()
    print(f"strategy_weights.json guncellendi (sample_size={weights['sample_size']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

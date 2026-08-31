"""One-time helper: reads .env locally and pushes IG_ACCESS_TOKEN, IG_USER_ID,
IG_APP_SECRET (and GH_PAT if present) into GitHub Actions repo secrets using the
already-authenticated `gh` CLI. Values are piped to `gh secret set` via stdin and
are never printed to stdout/stderr by this script.
"""
import os
import subprocess
import sys

GH_EXE = r"C:\Program Files\GitHub CLI\gh.exe"
SECRET_NAMES = ["IG_ACCESS_TOKEN", "IG_USER_ID", "IG_APP_SECRET", "GH_PAT"]


def main() -> int:
    from dotenv import dotenv_values

    env = dotenv_values(".env")
    repo = env.get("GH_REPO", "").strip()
    if not repo:
        print("GH_REPO .env icinde bos, once onu doldur.")
        return 1

    set_count = 0
    for name in SECRET_NAMES:
        value = (env.get(name) or "").strip()
        if not value:
            print(f"  {name}: .env icinde bos, atlaniyor")
            continue
        result = subprocess.run(
            [GH_EXE, "secret", "set", name, "--repo", repo],
            input=value,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            print(f"  {name}: BASARISIZ (detay gizlendi, returncode={result.returncode})")
        else:
            print(f"  {name}: OK")
            set_count += 1

    print(f"\n{set_count} secret GitHub'a yazildi ({repo}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

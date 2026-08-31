"""Small helper to update key=value lines in .env without ever printing their values."""
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def set_env_var(key: str, value: str) -> None:
    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}=") or line.strip() == key:
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

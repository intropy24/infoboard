
import json
import sys

from .config import JPLAG_SUMMARY_PATH


def jplag_data() -> dict | None:
    try:
        raw = JPLAG_SUMMARY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        print(f"jplag: не прочитать {JPLAG_SUMMARY_PATH}: {e}", file=sys.stderr)
        return None
    try:
        doc = json.loads(raw)
    except ValueError as e:
        print(f"jplag: битый JSON в {JPLAG_SUMMARY_PATH}: {e}", file=sys.stderr)
        return None
    return doc if doc.get("labs") else None

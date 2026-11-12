
from datetime import timedelta, timezone
from pathlib import Path

JPLAG_SUMMARY_PATH = Path(__file__).resolve().parent.parent / "jplag-summary.json"

MSK = timezone(timedelta(hours=3))

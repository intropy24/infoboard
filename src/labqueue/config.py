
import re
from datetime import timedelta, timezone
from pathlib import Path

LAB_BRANCH_RE = re.compile(r"^lab-[0-9]{2}([-.].*)?$")

EVENTS_CACHE_PATH = Path(__file__).resolve().parent.parent / "events-cache.json"
STATS_CACHE_PATH = Path(__file__).resolve().parent.parent / "commit-stats-cache.json"

JPLAG_SUMMARY_PATH = Path(__file__).resolve().parent.parent / "jplag-summary.json"


REVIEW_MARKS = {
    "APPROVED": "✅ approved",
    "CHANGES_REQUESTED": "🔄 changes",
    "COMMENTED": "💬 commented",
    "DISMISSED": "🚫 dismissed",
}


MSK = timezone(timedelta(hours=3))

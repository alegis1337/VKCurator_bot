import os
from datetime import date, datetime

import pytz


def get_curator_ids() -> set[int]:
    raw = os.getenv("CURATOR_IDS", "")
    return {int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}


def get_head_curator_id() -> int | None:
    raw = os.getenv("HEAD_CURATOR_ID", "").strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return None


def get_today_local() -> date:
    tz = pytz.timezone(os.getenv("TIMEZONE", "Europe/Moscow"))
    return datetime.now(tz).date()

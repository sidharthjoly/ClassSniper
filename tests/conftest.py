import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "bot"
sys.path.insert(0, str(BOT))

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sessions.json"


@pytest.fixture
def sessions():
    """A recorded slice of the real payload, shifted so it sits in the future.

    The scraper drops anything inside the 30h cancellation-fee margin, so a fixture
    with dates baked in would start passing for the wrong reason and then fail
    outright once it aged past the window.
    """
    recorded = json.loads(FIXTURE.read_text())
    stamps = [datetime.strptime(s["booking_start_datetime"], "%Y-%m-%d %H:%M:%S") for s in recorded]
    shift = (datetime.now() + timedelta(days=3)) - min(stamps)

    shifted = []
    for s in recorded:
        s = dict(s)
        start = datetime.strptime(s["booking_start_datetime"], "%Y-%m-%d %H:%M:%S") + shift
        s["booking_start_datetime"] = start.strftime("%Y-%m-%d %H:%M:%S")
        shifted.append(s)
    return shifted


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Every script in this project reads and writes JSON in the working directory."""
    monkeypatch.chdir(tmp_path)
    return tmp_path

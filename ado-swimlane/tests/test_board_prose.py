import re
from pathlib import Path

BOARD = Path(__file__).resolve().parent.parent / "board" / "board.html"
MAX_NOTE_WORDS = 40


def test_card_notes_stay_short():
    # Card notes are a glance at current status; history belongs in the
    # key-fact panels, the delta panel and Azure Boards, not on the card.
    html = BOARD.read_text(encoding="utf-8")
    long = []
    for card_id, note in re.findall(r'id="(c\d+)"(?:(?!class="card).)*?<div class="n">(.*?)</div>', html, re.S):
        words = len(re.sub(r"<[^>]+>", " ", note).split())
        if words > MAX_NOTE_WORDS:
            long.append(f"{card_id}: {words} words")
    assert not long, f"card notes over {MAX_NOTE_WORDS} words: {long}"

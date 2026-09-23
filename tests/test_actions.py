"""Ranking tests.

The point of ranking is that the top of the list is what a reader would act on
today. These pin the judgements that encodes -- especially the one that is
counter-intuitive: recency counts AGAINST urgency.
"""
import datetime

from meeting_scribe import actions as A

ASOF = datetime.date(2026, 9, 23)


def row(id="A-001", first="2026-09-09", last="2026-09-23", due="", owner="CLIENT",
        status="open", action="do a thing", blocks=""):
    return {"id": id, "first_seen": first, "last_discussed": last, "due": due,
            "owner": owner, "org": "CLIENT", "action": action, "status": status,
            "source_ts": "01:00", "notes": "", "blocks": blocks}


def test_closed_items_are_excluded():
    assert A.score(row(status="closed"), ASOF)[0] is None
    assert A.score(row(status="superseded"), ASOF)[0] is None
    assert A.score(row(status="in_progress"), ASOF)[0] is not None


def test_a_negative_score_still_appears_in_the_table():
    """Regression: scores can go negative (discussed today), and a numeric
    sentinel for 'excluded' silently dropped those rows entirely."""
    rows = [row(id="A-001", last="2026-09-23")]      # today -> negative score
    assert len(A.ranked(rows, ASOF, top=8)) == 1


def test_blocking_outranks_an_equivalent_non_blocker():
    a = A.score(row(blocks="UAT testing"), ASOF)[0]
    b = A.score(row(), ASOF)[0]
    assert a > b


def test_unowned_outranks_an_owned_equivalent():
    """An item nobody owns cannot progress by itself."""
    assert A.score(row(owner=""), ASOF)[0] > A.score(row(owner="CLIENT"), ASOF)[0]


def test_overdue_outranks_due_soon():
    overdue = A.score(row(due="2026-09-17"), ASOF)[0]
    soon = A.score(row(due="2026-09-28"), ASOF)[0]
    assert overdue > soon


def test_discussed_today_is_deprioritised_not_promoted():
    """Regression: the first cut gave a bonus for 'moved today', which pushed a
    14-day-silent blocker off the table on meeting days. An item discussed today
    is in somebody's hands; a silent one is not."""
    today = A.score(row(last="2026-09-23", blocks="X"), ASOF)[0]
    silent = A.score(row(last="2026-09-09", blocks="X"), ASOF)[0]
    assert silent > today, "a long-silent blocker must outrank one discussed today"


def test_silence_bands_increase_monotonically():
    s5 = A.score(row(last="2026-09-18"), ASOF)[0]
    s10 = A.score(row(last="2026-09-13"), ASOF)[0]
    s14 = A.score(row(last="2026-09-09"), ASOF)[0]
    assert s5 < s10 < s14


def test_reasons_are_reported_not_just_a_score():
    """The table has to say WHY an item is there, or it is just another list."""
    _, why = A.score(row(blocks="UAT testing", owner="", due="2026-09-17"), ASOF)
    joined = " ".join(why)
    assert "blocks UAT testing" in joined
    assert "UNOWNED" in joined
    assert "overdue" in joined


def test_output_is_capped():
    rows = [row(id=f"A-{i:03d}") for i in range(40)]
    assert len(A.ranked(rows, ASOF, top=8)) == 8
    assert len(A.ranked(rows, ASOF, top=3)) == 3


def test_summary_counts_what_matters():
    rows = [row(id="A-001", owner="", blocks="X"),
            row(id="A-002", due="2026-09-30"),
            row(id="A-003", status="closed"),
            row(id="A-004", last="2026-09-01")]
    s = A.summary(rows, ASOF)
    assert s["total"] == 4 and s["open"] == 3
    assert s["unowned"] == 1 and s["blocking"] == 1 and s["with_due"] == 1
    assert s["silent_10"] == 1


def test_render_mentions_the_hidden_remainder():
    """A capped table must say how much it is hiding, or it misleads."""
    rows = [row(id=f"A-{i:03d}") for i in range(30)]
    out = A.render_markdown(rows, ASOF, top=5)
    assert "further open items not shown" in out
    assert out.count("| **A-") == 5

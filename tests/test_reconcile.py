"""Reconciliation tests.

The judgement being pinned here is that links are explicit and fuzzy matching is
only ever a suggestion. Auto-linking on token overlap reads well in a demo and
quietly merges unrelated items in real data.
"""
from meeting_scribe import reconcile as R


def ours(id="A-001", action="Submit data access request", status="open", counterpart=""):
    return {"id": id, "action": action, "status": status, "counterpart": counterpart}


def theirs(item="Datastore", status="OPEN"):
    raw = status.strip().lower()
    return {"item": item, "status": status, "status_norm": R.STATUS_MAP.get(raw, raw)}


def test_status_vocabulary_is_mapped_not_guessed():
    assert theirs(status="IN PROGRESS")["status_norm"] == "in_progress"
    assert theirs(status="Done")["status_norm"] == "closed"
    assert theirs(status="Parked")["status_norm"] == "parked", "unknown states kept verbatim"


def test_linked_items_with_matching_state_are_quiet():
    res = R.reconcile([ours(counterpart="Datastore")], [theirs()])
    assert len(res["linked"]) == 1
    assert res["disagree"] == []


def test_disagreement_is_surfaced():
    """The whole point: they think it is done, we do not."""
    res = R.reconcile([ours(counterpart="Datastore", status="open")],
                      [theirs(status="Done")])
    assert len(res["disagree"]) == 1
    assert "we say open" in res["disagree"][0]["note"]


def test_their_items_we_are_not_tracking_are_reported():
    res = R.reconcile([ours(action="something unrelated entirely")],
                      [theirs(item="ERP Worklist Integration")])
    assert len(res["theirs_only"]) == 1


def test_fuzzy_is_a_suggestion_never_an_automatic_link():
    res = R.reconcile([ours(id="A-009", action="Data Store access application")],
                      [theirs(item="Datastore")])
    assert res["linked"] == [], "must not auto-link on overlap alone"
    assert len(res["suggestions"]) == 1
    assert res["suggestions"][0]["suggest"]["id"] == "A-009"


def test_weak_overlap_is_not_suggested():
    res = R.reconcile([ours(action="Fix the dropdown on the label screen")],
                      [theirs(item="ERP Worklist Integration")])
    assert res["suggestions"] == []
    assert len(res["theirs_only"]) == 1


def test_similarity_uses_containment_not_jaccard():
    """Their labels are short, ours are sentences. Symmetric overlap would score
    every genuine pair near zero."""
    short, long_ = "Datastore", "Submit the Data Store access request with Legal"
    assert R.similarity(short, long_) > 0.4


def test_a_dangling_link_is_reported():
    """We linked to an item that has since vanished from their tracker."""
    res = R.reconcile([ours(counterpart="Removed Item")], [theirs()])
    assert len(res["ours_only"]) == 1
    assert "not on their tracker" in res["ours_only"][0]["why"]


def test_render_mentions_each_section_present():
    res = R.reconcile([ours(counterpart="Datastore", status="closed")],
                      [theirs(status="OPEN"), theirs(item="SSO Worklist")])
    md = R.render_markdown(res)
    assert "Status disagreements" in md
    assert "On their tracker, not on ours" in md


def test_compound_and_spaced_spellings_match():
    """Domain vocabulary spells the same term both ways -- Datastore/Data Store,
    Worklist/work list. Token overlap alone scores those exactly zero."""
    assert R.similarity("Datastore", "Submit the Data Store access request") == 1.0
    assert R.similarity("LDAP Worklist Integrations", "build the LDAP work list integration") > 0.5


def test_squash_match_needs_length_to_avoid_noise():
    assert R.similarity("UAT", "evaluate a thing") < 0.5


def test_one_of_ours_can_answer_for_several_of_theirs():
    """Their deck summarises; our list is granular. A single row covering two of
    their lines must not leave the second reported as untracked."""
    res = R.reconcile([ours(counterpart="ERP Worklist; SSO Worklist")],
                      [theirs(item="ERP Worklist"), theirs(item="SSO Worklist")])
    assert len(res["linked"]) == 2
    assert res["theirs_only"] == []


def test_generic_words_do_not_carry_a_match():
    """The failure this is built to avoid: one ubiquitous word covering a short
    label and matching everything to everything."""
    theirs_list = [theirs(item="CRM Integrations"), theirs(item="SFTP Integrations"),
                   theirs(item="ERP Integrations"), theirs(item="SSO Integrations")]
    ours_list = [ours(id="A-100", action="Resolve the HTTP timeout on the MQ integration")]
    res = R.reconcile(ours_list, theirs_list)
    assert res["suggestions"] == [], "sharing only 'integration' is not a match"

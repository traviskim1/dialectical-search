"""Tests for v6 (pytest -q). They use the mock backend, so no model is needed."""

import os
import re
from pathlib import Path

import pytest

os.environ["DATABASE_URL"] = ""   # before importing value_tree: .env must never point tests at the real database

import value_tree as vt
from mock_backend import MockBackend


@pytest.fixture(autouse=True)
def tmp_journals(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNALS_DIR", str(tmp_path / "journals"))


def started():
    b = MockBackend()
    j = vt.start(b, "I'm getting into it. I already do the obvious thing.")
    assert j["questions"], "the mock asks one follow-up question"
    vt.answer_questions(b, j, ["About a month, twice a week."])
    return b, j


def give_feedback(b, j, texts, general=""):
    rnd = vt.current_round(j)
    for c in rnd["cards"]:
        c["free"] = texts.get(c["action"], "It went fine.")
    vt.ask_guided(b, j)
    for c in rnd["cards"]:
        c["guided_a"] = ["They were nearby." for _ in c["guided_q"]]
    rnd["general"] = general
    return vt.close_round(b, j)


def test_search_prompt_is_travis_wording():
    assert vt.SEARCH_PROMPT.startswith("Tell me about an activity you are interested in.")


def test_tree_shape_and_shared_action():
    _, j = started()
    t = j["tree"]
    assert len(t["values"]) == 3 and len(t["principles"]) == 6
    assert len(vt.parents(t, "a1")) == 2, "a1 is listed under two principles in two values, stored once"
    assert t["principles"][vt.parents(t, "a1")[0]]["value"] != t["principles"][vt.parents(t, "a1")[1]]["value"]


def test_crossing_off_a_shared_action_counts_everywhere():
    _, j = started()
    # the intake said "already", so the mock update crossed off a1
    assert j["tree"]["actions"]["a1"]["state"] == "done"
    for pid in vt.parents(j["tree"], "a1"):
        assert vt.coverage(j, pid) > 0


def test_first_round_has_2_to_3_cards_within_reach():
    _, j = started()
    rnd = vt.current_round(j)
    assert vt.MIN_CARDS <= len(rnd["cards"]) <= vt.CARDS
    for c in rnd["cards"]:
        a = j["tree"]["actions"][c["action"]]
        assert a["state"] != "done"
        assert a["overall"] <= j["profile"]["level"] + vt.AWARENESS + vt.STRETCH
        assert c["card"]["title"]


def test_miss_weight_shrinks_with_closed_rounds():
    b, j = started()
    w0 = vt.miss_weight(j)
    give_feedback(b, j, {})
    assert vt.miss_weight(j) < w0


def test_partial_counts_half():
    _, j = started()
    pid = "p6"
    for a in j["tree"]["principles"][pid]["actions"]:
        j["tree"]["actions"][a]["state"] = "open"
    first = j["tree"]["principles"][pid]["actions"][0]
    j["tree"]["actions"][first]["state"] = "partial"
    n = len(j["tree"]["principles"][pid]["actions"])
    assert vt.coverage(j, pid) == pytest.approx(vt.PARTIAL_CREDIT / n)


def test_engaged_actions_are_crossed_off_and_new_ones_suggested():
    b, j = started()
    first = [c["action"] for c in vt.current_round(j)["cards"]]
    give_feedback(b, j, {})
    for aid in first:
        assert j["tree"]["actions"][aid]["state"] == "done"
    second = [c["action"] for c in vt.current_round(j)["cards"]]
    assert not set(first) & set(second)


def test_failure_comes_back_with_a_reply():
    b, j = started()
    aid = vt.current_round(j)["cards"][0]["action"]
    give_feedback(b, j, {aid: "I was scared to try it."})
    nxt = vt.current_round(j)["cards"]
    retry = [c for c in nxt if c["action"] == aid]
    assert retry and retry[0]["retry"] and retry[0]["reply"]
    assert j["tree"]["actions"][aid]["attempts"] == 1


def test_no_chance_doesnt_count_as_an_attempt():
    b, j = started()
    aid = vt.current_round(j)["cards"][0]["action"]
    give_feedback(b, j, {aid: "It didn't come up this week."})
    assert j["tree"]["actions"][aid]["attempts"] == 0
    assert any(c["action"] == aid for c in vt.current_round(j)["cards"])


def test_backtrack_after_three_failures():
    b, j = started()
    aid = vt.current_round(j)["cards"][0]["action"]
    for k in range(vt.MAX_ATTEMPTS):
        assert any(c["action"] == aid for c in vt.current_round(j)["cards"]), f"retry {k} missing"
        summary = give_feedback(b, j, {aid: "I did the other thing instead."})
    assert summary.get("backtracked"), "third failure backtracks"
    a = j["tree"]["actions"][aid]
    assert a["blocked_by"] and not vt.available(j, aid)
    new_pid = summary["backtracked"][0]["principle"]
    assert new_pid in j["boost"]
    cards = [c["action"] for c in vt.current_round(j)["cards"]]
    assert aid not in cards
    assert set(cards) & set(a["blocked_by"]), "the easier steps come next"


def test_unrelated_things_they_noticed_are_added_to_the_tree():
    b, j = started()
    n = len(j["tree"]["actions"])
    summary = give_feedback(b, j, {}, general="I noticed something new about the people across from me.")
    assert summary["added"] and len(j["tree"]["actions"]) == n + 1
    assert j["tree"]["actions"][summary["added"][0]]["state"] == "done"


def test_misc_details_go_outside_the_tree():
    b, j = started()
    give_feedback(b, j, {}, general="I'm heavier than most people there.")
    assert "heavier build" in j["profile"]["misc"]


def test_journal_saved_and_reloads():
    b, j = started()
    again = vt.load(j["slug"])
    assert again["tree"] == j["tree"] and again["rounds"][0]["cards"]
    assert (vt.journals_dir() / j["slug"] / "tree.md").exists()
    assert j["calls"] and all("prompt" in c and "raw" in c for c in j["calls"])


def test_build_tree_drops_unknown_and_orphans():
    data = {"values": [{"name": "V", "principles": [{"name": "P", "actions": ["x1", "nope"]}]}],
            "actions": [{"id": "x1", "name": "Do it"}, {"id": "x2", "name": "Orphan"}]}
    tree, problems = vt.build_tree(data)
    assert list(tree["actions"]) == ["a1"]
    assert any("unknown" in p for p in problems) and any("dropped" in p for p in problems)


def test_level_moves_at_most_one():
    b, j = started()
    j["profile"]["level"] = 2
    orig = b.complete
    b.complete = lambda p, step="", max_tokens=0: (
        '{"done": [], "partial": [], "new_actions": [], "knowledge": [], "level": 9, "principle_levels": {}, "misc": []}'
        if step.startswith("update") else orig(p, step))
    vt.update(b, j, "anything", "test")
    assert j["profile"]["level"] == 3


ACTIVITY_WORDS = r"\b(basketball|screen|rebound|dribbl\w*|layup|shot|court|teammate|defender|guitar|chess|choir|paint\w*)\b"


def test_prompts_know_no_activity():
    prompts = [v for k, v in vars(vt).items() if k.startswith("PROMPT_") or k.endswith("_RULE")]
    for p in prompts:
        assert not re.search(ACTIVITY_WORDS, p, re.I), re.search(ACTIVITY_WORDS, p, re.I).group(0)


def test_shortlist_prefers_high_impact_at_equal_stretch():
    _, j = started()
    tree = j["tree"]
    pid = next(iter(tree["principles"]))
    base = {"called": "", "what": "", "why": "", "overall": j["profile"]["level"], "principle": 1}
    low = vt.new_action(tree, {**base, "name": "Low impact one", "impact": 2}, "test")
    high = vt.new_action(tree, {**base, "name": "High impact one", "impact": 9}, "test")
    vt.attach(tree, low, pid)
    vt.attach(tree, high, pid)
    order = [a for a, _ in vt.shortlist(j, [(pid, 1.0)])]
    assert order.index(high) < order.index(low)


def test_high_impact_action_can_outrank_a_slightly_needier_principle():
    _, j = started()
    tree = j["tree"]
    p1, p2 = list(tree["principles"])[:2]
    base = {"called": "", "what": "", "why": "", "overall": j["profile"]["level"], "principle": 1}
    low = vt.new_action(tree, {**base, "name": "Low impact, neediest principle", "impact": 3}, "test")
    high = vt.new_action(tree, {**base, "name": "High impact, next principle", "impact": 9}, "test")
    vt.attach(tree, low, p1)
    vt.attach(tree, high, p2)
    order = [a for a, _ in vt.shortlist(j, [(p1, 0.8), (p2, 0.7)])]
    assert order.index(high) < order.index(low)


def test_tree_is_built_while_the_questions_are_answered():
    b = MockBackend()
    j = vt.start(b, "I'm getting into it. I already do the obvious thing.")
    assert j["questions"] and j["tree"] is None
    ahead = vt._trees[j["key"]]                      # started by start(), before any answer
    assert ahead.result()["tree"]["actions"]
    vt.answer_questions(b, j, ["About a month, twice a week."])
    assert b.calls["tree"] == 1, "the tree built ahead is used, not asked for again"
    assert j["key"] not in vt._trees
    steps = [c["step"] for c in j["calls"]]
    assert steps.count("tree") == 1 and steps.index("tree") < steps.index("update (intake)")
    # same tree, cards and prompts as when nothing is built ahead
    assert plain_run(vt.PREFETCH_TREE) == plain_run(False)


def plain_run(prefetch):
    old, vt.PREFETCH_TREE = vt.PREFETCH_TREE, prefetch
    try:
        _, j = started()
    finally:
        vt.PREFETCH_TREE = old
    return (j["tree"], [c["card"] for c in j["rounds"][0]["cards"]],
            sorted((c["step"], c["prompt"], c["raw"]) for c in j["calls"]))


def test_miss_query_never_sees_the_tree_and_each_card_is_its_own_call():
    b, j = started()
    cards = vt.current_round(j)["cards"]
    assert b.calls["miss"] == 1 and b.calls["locate misses"] == 1
    assert b.calls["expand"] == len(cards)
    steps = [c["step"] for c in j["calls"]]
    assert sorted(s for s in steps if s.startswith("expand")) == sorted(f"expand ({c['action']})" for c in cards)
    miss = next(c for c in j["calls"] if c["step"] == "miss")
    locate = next(c for c in j["calls"] if c["step"] == "locate misses")
    names = [p["name"] for p in j["tree"]["principles"].values()]
    assert not any(n in miss["prompt"] for n in names), "the question is asked without the tree in view"
    assert all(n in locate["prompt"] for n in names)
    assert vt.current_round(j)["misses"] and all(c["card"]["id"] == c["action"] for c in cards)
    give_feedback(b, j, {})
    assert b.calls["miss"] == 2 and b.calls["expand"] == len(cards) + len(vt.current_round(j)["cards"])


def test_every_card_call_is_given_every_cards_moment():
    b, j = started()
    cards = vt.current_round(j)["cards"]
    moments = [c["moment"] for c in cards]
    assert all(moments) and len(set(moments)) == len(moments), "the choose call gives each card its own moment"
    for call in (c for c in j["calls"] if c["step"].startswith("expand")):
        assert all(f"Moment: {m}" in call["prompt"] for m in moments)
    # a retried action keeps the scene it had, and the newly chosen ones are told to stay clear of it
    first = cards[0]["action"]
    give_feedback(b, j, {first: "I was scared to."})
    again = next(c for c in vt.current_round(j)["cards"] if c["action"] == first)
    assert again["retry"] and again["moment"] == "Imagine you're there."
    choose = [c for c in j["calls"] if c["step"] == "choose"][-1]
    assert "- Imagine you're there." in choose["prompt"]
    new_cards = [c for c in j["calls"] if c["step"].startswith("expand")][-len(vt.current_round(j)["cards"]):]
    assert all("Moment: Imagine you're there." in c["prompt"] for c in new_cards)


def test_miss_query_reads_the_profile_from_before_the_update():
    b, j = started()
    j["profile"]["knowledge"].append("a line known before this round closed")
    real_update = vt.update

    def update_adding_a_line(backend, jj, new_text, why):
        jj["profile"]["knowledge"].append("a line the update added")
        return real_update(backend, jj, new_text, why)

    vt.update = update_adding_a_line
    try:
        give_feedback(b, j, {})
    finally:
        vt.update = real_update
    miss = [c for c in j["calls"] if c["step"] == "miss"][-1]
    assert "a line known before this round closed" in miss["prompt"]
    assert "a line the update added" not in miss["prompt"]


def test_a_card_that_fails_keeps_the_trees_write_up():
    b, j = started()
    real = b.complete
    b.complete = lambda prompt, step="", max_tokens=0: "not json" if step.startswith("expand") else real(prompt, step)
    give_feedback(b, j, {})
    for c in vt.current_round(j)["cards"]:
        assert c["card"]["id"] == c["action"] and c["card"]["title"] == j["tree"]["actions"][c["action"]]["name"]


def test_a_failed_early_tree_is_asked_for_again():
    b = MockBackend()
    j = vt.start(b, "I'm getting into it. I already do the obvious thing.")
    vt._trees[j["key"]].result()
    failed = vt.Future()
    failed.set_exception(RuntimeError("network"))
    vt._trees[j["key"]] = failed
    vt.answer_questions(b, j, ["About a month, twice a week."])
    assert b.calls["tree"] == 2 and j["tree"]["actions"] and vt.current_round(j)["cards"]


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL to test the Postgres store")
def test_postgres_store(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    with vt._db() as conn:
        conn.execute("delete from dtr_journals")
    b, j = started()
    assert j["slug"] in vt.list_journals()
    assert vt.find_by_key(j["key"]) == j["slug"]
    assert vt.find_by_key("not-a-key") is None
    back = vt.load(j["slug"])
    assert back["key"] == j["key"] and back["calls"] and back["tree"]["actions"]
    j2 = vt.start(b, "I'm getting into it. I already do the obvious thing.")
    assert j2["slug"] != j["slug"]                       # same activity, never the same slug
    assert len(vt.export_all()) == 2
    with pytest.raises(FileNotFoundError):
        vt.load("missing")
    # the connection is kept open between statements, and replaced when the server has dropped it
    kept = vt._conn[1]
    vt.load(j["slug"])
    assert vt._conn[1] is kept
    with vt._db() as other:
        other.execute("select pg_terminate_backend(%s)", (kept.info.backend_pid,))
    assert vt.load(j["slug"])["key"] == j["key"] and vt._conn[1] is not kept

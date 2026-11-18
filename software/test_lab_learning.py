"""Tests for source/server/lab_learning.py (schema + behaviour).

Runs entirely against `tmp_path` databases - never `~/.friday`, never a
hardcoded path, never the network.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.getcwd())

import pytest

from source.server import lab_learning
from source.server.lab_learning import (LIST_CAP, SCHEMA_STATEMENTS,
                                        TABLE_COLUMNS, LabLearning)

FORBIDDEN_TRAINING = re.compile(r"train|fine_tune|finetune|upload_dataset",
                                re.IGNORECASE)
FORBIDDEN_EXECUTION = re.compile(
    r"exec|eval|compile|interpret|run_card|execute", re.IGNORECASE)


@pytest.fixture()
def ll(tmp_path):
    store = LabLearning(tmp_path / "learning.db")
    store.init_schema()
    yield store
    store.close()


# ---------------------------------------------------------------------------
# Stage 1: schema smoke tests
# ---------------------------------------------------------------------------
def test_init_schema_is_idempotent_and_init_does_no_io(tmp_path):
    path = tmp_path / "nested" / "learning.db"
    store = LabLearning(path)
    # __init__ must not touch the filesystem at all.
    assert not path.exists()
    assert not path.parent.exists()
    first = store.init_schema()
    second = store.init_schema()
    assert first["ok"] is True and second["ok"] is True
    assert path.exists()
    assert store._table_names() == store._table_names()  # stable
    assert len(store._table_names()) == len(SCHEMA_STATEMENTS)
    store.close()


def test_schema_has_exactly_the_ten_mandated_tables(ll):
    expected = sorted(TABLE_COLUMNS)
    assert ll._table_names() == expected
    for statement in SCHEMA_STATEMENTS:
        match = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", statement)
        assert match and match.group(1) in TABLE_COLUMNS
        for column in TABLE_COLUMNS[match.group(1)]:
            assert re.search(r"\b%s\b" % re.escape(column), statement)
    # The store must never grow an undocumented table.
    assert len(ll._table_names()) == 10


def test_no_hardcoded_path_and_no_training_entry_point(tmp_path):
    with pytest.raises(ValueError):
        LabLearning(None)
    with open(lab_learning.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert ".friday" not in source
    assert "Path.home" not in source
    # No attribute - public or private - may look like a training / upload
    # entry point. Learning here is practice + feedback, never weights.
    offenders = [name for name in dir(lab_learning)
                 if FORBIDDEN_TRAINING.search(name)]
    assert offenders == []


# ---------------------------------------------------------------------------
# Stage 2: goal lifecycle + held-out split
# ---------------------------------------------------------------------------
def _open_goal(ll, text="learn the drill", cases=("case-a", "case-b")):
    created = ll.open_goal(text, "score >= 0.9", list(cases))
    assert created["ok"] is True
    return created["goal"]


def test_goal_creation_requires_criteria_and_held_out_split(ll):
    refused = ll.open_goal("no criteria", "", ["case-a"])
    assert refused["ok"] is False
    assert "success_criteria" in refused["reason"]
    refused = ll.open_goal("no split", "score >= 0.9", [])
    assert refused["ok"] is False
    assert "held_out_case_ids" in refused["reason"]
    assert ll.goals() == []
    goal = _open_goal(ll)
    assert goal["state"] == "open"
    assert goal["success_criteria"] == "score >= 0.9"
    assert goal["held_out_case_ids"] == ["case-a", "case-b"]
    # Ids are opaque prefix-hex8 strings.
    assert re.fullmatch(r"goal-[0-9a-f]{8}", goal["id"])


def test_goal_lifecycle_and_promotion_refused_without_validation(ll):
    goal = _open_goal(ll)
    # Illegal jump: a fresh goal cannot skip straight to rolled_back.
    bad = ll.set_goal_state(goal["id"], "rolled_back")
    assert bad["ok"] is False and "cannot go from" in bad["reason"]
    # Promotion by fiat is refused: there is no validation run yet.
    promoted = ll.promote_goal(goal["id"])
    assert promoted["ok"] is False
    assert "validation" in promoted["reason"]
    assert ll.goal(goal["id"])["state"] == "open"
    # Practice moves open -> practising automatically.
    ex = ll.add_exercise(goal["id"], "drill one", "answer", held_out=False)
    ll.record_practice(goal["id"], "char-1", ex["exercise"]["id"],
                       "my answer", correct=True, latency_ms=12,
                       model_calls=1, provider="modal", model="glm")
    assert ll.goal(goal["id"])["state"] == "practising"
    # ... and practising -> validating is a legal, explicit step.
    moved = ll.set_goal_state(goal["id"], "validating")
    assert moved["ok"] is True
    assert ll.goal(goal["id"])["state"] == "validating"
    # Still no validation run, so promotion stays refused from validating too.
    assert ll.promote_goal(goal["id"])["ok"] is False
    # Unknown ids and states are refused, never raised.
    assert ll.set_goal_state("goal-deadbeef", "promoted")["ok"] is False
    assert ll.set_goal_state(goal["id"], "sideways")["ok"] is False
    # Every return is JSON-safe.
    json.dumps(ll.goal(goal["id"]))
    json.dumps(ll.goals())


def test_held_out_exercise_cannot_become_a_practice_run(ll):
    goal = _open_goal(ll, cases=("case-a", "case-b"))
    drill = ll.add_exercise(goal["id"], "practice me", "reference")
    held = ll.add_exercise(goal["id"], "secret case", "reference",
                           held_out=True)
    assert held["exercise"]["held_out"] is True
    ok = ll.record_practice(goal["id"], "char-1", drill["exercise"]["id"],
                            "answer", correct=True)
    assert ok["ok"] is True
    refused = ll.record_practice(goal["id"], "char-1",
                                 held["exercise"]["id"], "answer",
                                 correct=True)
    assert refused["ok"] is False
    assert "held-out" in refused["reason"]
    # Also refused when the id is only on the goal's held-out list.
    listed = ll.add_exercise(goal["id"], "sneaky", "reference")
    ll._write("UPDATE learning_goals SET held_out_case_ids=? WHERE id=?",
                (json.dumps(["case-a", listed["exercise"]["id"]]), goal["id"]))
    refused = ll.record_practice(goal["id"], "char-1",
                                 listed["exercise"]["id"], "answer",
                                 correct=True)
    assert refused["ok"] is False and "held-out" in refused["reason"]
    # Exactly one practice run exists - the refused ones wrote nothing.
    assert len(ll.practice_runs(goal["id"])) == 1
    json.dumps(ll.practice_runs(goal["id"]))


# ---------------------------------------------------------------------------
# Stage 3: tutoring + bounded debate
# ---------------------------------------------------------------------------
def test_tutoring_stores_only_what_was_exchanged(ll):
    request = ll.request_tutor("gap: never verifies her own work",
                               "alice", "bob")
    assert request["ok"] is True
    req = request["request"]
    assert re.fullmatch(r"tut-[0-9a-f]{8}", req["id"])
    # Requesting stored no exchange: nothing was said yet.
    assert ll.tutor_sessions(req["id"]) == []
    recorded = ll.record_tutor_session(req["id"],
                                       "run one check before claiming done")
    assert recorded["ok"] is True
    sessions = ll.tutor_sessions(req["id"])
    assert len(sessions) == 1
    session = sessions[0]
    # Exactly the exchange, no extra fields.
    assert set(session) == {"id", "class_id", "char_id", "question", "answer",
                            "attempt", "gap", "joined_late", "at"}
    assert session["question"] == "gap: never verifies her own work"
    assert session["answer"] == "run one check before claiming done"
    assert session["char_id"] == "alice"
    assert session["attempt"] == 1
    # No invented content: every non-empty string stored for this exchange is
    # either caller-supplied or an opaque id/state. Nothing summarised,
    # nothing interpreted, nothing echoed back that was never said.
    allowed = {"gap: never verifies her own work",
               "run one check before claiming done",
               "alice", "bob", "tutoring"}
    for row in [ll.tutor_request(req["id"])] + sessions:
        for key, value in row.items():
            if isinstance(value, str) and value:
                assert value in allowed or re.fullmatch(
                    r"(tut|ses)-[0-9a-f]{8}", value), (key, value)
    # Bad requests are refused, not papered over.
    assert ll.request_tutor("", "alice", "bob")["ok"] is False
    assert ll.request_tutor("gap", "", "bob")["ok"] is False
    assert ll.record_tutor_session("tut-deadbeef", "answer")["ok"] is False
    assert ll.record_tutor_session(req["id"], "   ")["ok"] is False
    # A second, separate request keeps its own exchange.
    other = ll.request_tutor("another gap", "carol", "bob")["request"]
    ll.record_tutor_session(other["id"], "second answer")
    assert len(ll.tutor_sessions(other["id"])) == 1
    assert len(ll.tutor_sessions(req["id"])) == 1
    assert len(ll.tutor_requests()) == 2
    json.dumps(ll.tutor_requests())
    json.dumps(sessions)


def test_debate_is_bounded_and_never_silently_agrees(ll):
    goal = _open_goal(ll)
    ex = ll.add_exercise(goal["id"], "drill", "answer", held_out=False)
    ll.record_practice(goal["id"], "ada", ex["exercise"]["id"], "a",
                       correct=True)
    assert ll.goal(goal["id"])["state"] == "practising"
    started = ll.start_debate(goal["id"], "ada", "bea")   # default bound
    assert started["ok"] is True and started["max_rounds"] == 3
    debate_id = started["debate"]["id"]
    assert started["debate"]["round"] == 0
    first = ll.debate_round(debate_id, "round one")
    assert first["ok"] is True
    assert first["debate"]["state"] == "active"
    assert first["debate"]["round"] == 1
    ll.debate_round(debate_id, "round two")
    third = ll.debate_round(debate_id, "round three")
    assert third["ok"] is True and third["capped"] is True
    final = ll.debate(debate_id)
    # Cap without agreement -> disputed. Never a silent agreement.
    assert final["round"] == 3
    assert final["state"] == "disputed"
    assert final["state"] != "agreed"
    assert [entry["text"] for entry in final["transcript"]] == [
        "round one", "round two", "round three"]
    assert ll.goal(goal["id"])["state"] == "disputed"
    # The cap is real: no fourth round, and no state change afterwards.
    refused = ll.debate_round(debate_id, "round four")
    assert refused["ok"] is False
    assert ll.debate(debate_id)["state"] == "disputed"
    # Explicit agreement before the cap is honoured - it is a decision, not a
    # side effect of running out of rounds.
    fresh = ll.start_debate(goal["id"], "ada", "bea", max_rounds=2)
    agreed = ll.debate_round(fresh["debate"]["id"], "we agree", agreed=True)
    assert agreed["ok"] is True
    assert agreed["debate"]["state"] == "agreed"
    assert agreed["debate"]["round"] == 1
    # A debate capped on its very first round also disputes.
    tiny = ll.start_debate(goal["id"], "ada", "bea", max_rounds=1)
    capped = ll.debate_round(tiny["debate"]["id"], "only round")
    assert capped["capped"] is True
    assert capped["debate"]["state"] == "disputed"
    json.dumps(final["transcript"])
    json.dumps(ll.debates(goal["id"]))


# ---------------------------------------------------------------------------
# Stage 4: validation gate + skill cards
# ---------------------------------------------------------------------------
def _make_card(ll, name="verify-then-claim"):
    created = ll.create_skill_card(
        name,
        when_applicable="whenever a result is claimed",
        procedure="1. run the check\n2. read the output\n3. then claim",
        limitations="not for trivial answers",
        evidence="12 practice runs",
        checks="one follow-up check",
        provenance="practice+feedback")
    assert created["ok"] is True
    return created["card"]


def test_validation_gate_promotes_only_on_full_evidence(ll):
    goal = _open_goal(ll, cases=("case-a", "case-b"))
    card = _make_card(ll)
    result = ll.run_validation(goal["id"], card["id"],
                               held_out_cases=["case-a", "case-b"],
                               baseline_answers=[True, False],   # 0.50
                               reference_outcomes=[True, True])  # 1.00
    assert result["ok"] is True and result["promoted"] is True
    assert result["held_out_score"] == 1.0
    assert result["baseline_score"] == 0.5
    assert ll.goal(goal["id"])["state"] == "promoted"
    refreshed = ll.skill_card(card["id"])
    assert refreshed["state"] == "promoted"
    assert refreshed["promoted_at"] is not None
    run = ll.validation_runs(goal_id=goal["id"])[0]
    assert run["regression_pass"] is True
    assert run["evidence"].startswith("reference outcomes met")
    assert run["skill_id"] == card["id"]
    # An already-promoted goal refuses further validation runs.
    again = ll.run_validation(goal["id"], card["id"],
                              held_out_cases=["case-a"],
                              baseline_answers=[False],
                              reference_outcomes=[True])
    assert again["ok"] is False and "rollback" in again["reason"]
    json.dumps(result)


def test_baseline_at_target_has_no_headroom(ll):
    goal = _open_goal(ll, cases=("case-a",))
    card = _make_card(ll, name="perfect-baseline")
    result = ll.run_validation(goal["id"], card["id"],
                               held_out_cases=["case-a"],
                               baseline_answers=[True],      # 1.00 == target
                               reference_outcomes=[True])
    assert result["ok"] is True
    assert result["promoted"] is False
    assert result["validation"]["evidence"] == "no headroom"
    assert result["validation"]["regression_pass"] is True   # no regression
    assert ll.goal(goal["id"])["state"] == "validating"
    assert ll.skill_card(card["id"])["state"] == "draft"


def test_unanimous_yes_cannot_override_failing_outcomes(ll):
    goal = _open_goal(ll, cases=("case-a", "case-b"))
    card = _make_card(ll, name="voted-in")
    result = ll.run_validation(goal["id"], card["id"],
                               held_out_cases=["case-a", "case-b"],
                               baseline_answers=[True, False],  # 0.50
                               reference_outcomes=[True, False],
                               votes=["yes", "yes", "yes"])
    assert result["votes"]["unanimous_yes"] is True
    assert result["votes"]["yes"] == 3 and result["votes"]["total"] == 3
    assert result["promoted"] is False
    assert result["validation"]["evidence"] == "reference outcomes not met"
    assert result["validation"]["regression_pass"] is False
    assert ll.goal(goal["id"])["state"] != "promoted"
    assert ll.skill_card(card["id"])["state"] == "draft"


def test_held_out_below_baseline_refuses(ll):
    goal = _open_goal(ll, cases=("case-a", "case-b", "case-c"))
    card = _make_card(ll, name="worse-than-baseline")
    result = ll.run_validation(goal["id"], card["id"],
                               held_out_cases=["case-a", "case-b", "case-c"],
                               baseline_answers=[True, True, True, False],
                               reference_outcomes=[True, False, False])
    assert result["promoted"] is False
    assert result["validation"]["evidence"] == "held_out below baseline"
    assert result["validation"]["regression_pass"] is False
    assert result["held_out_score"] < result["baseline_score"]
    assert ll.goal(goal["id"])["state"] == "validating"


def test_validation_cap_without_evidence_disputes_the_goal(ll):
    goal = _open_goal(ll, cases=("case-a",))
    card = _make_card(ll, name="no-evidence")
    first = ll.run_validation(goal["id"], card["id"], held_out_cases=[],
                              baseline_answers=[True], cap=2)
    assert first["promoted"] is False
    assert first["validation"]["evidence"] == "no evidence"
    assert first["validation"]["held_out_score"] is None   # unknown, not 0.0
    assert ll.goal(goal["id"])["state"] == "validating"    # 1 attempt < cap
    # Cases without a baseline are equally evidence-free.
    no_baseline = ll.run_validation(goal["id"], card["id"],
                                    held_out_cases=["case-a"],
                                    baseline_answers=[], cap=2)
    assert no_baseline["validation"]["evidence"] == "no evidence"
    # The cap itself: attempts without evidence -> disputed, openly.
    assert ll.goal(goal["id"])["state"] == "disputed"
    assert ll.skill_card(card["id"])["state"] == "draft"


def test_validation_reads_every_case_verdict_shape(ll):
    # id -> verdict mapping
    goal = _open_goal(ll, cases=("case-a", "case-b"))
    card = _make_card(ll, name="shape-card")
    mapped = ll.run_validation(goal["id"], card["id"],
                               held_out_cases={"case-a": True,
                                               "case-b": True},
                               baseline_answers={"case-a": True,
                                                 "case-b": False})
    assert mapped["promoted"] is True
    assert mapped["held_out_score"] == 1.0
    assert mapped["baseline_score"] == 0.5
    # answer paired with reference: equal strings = outcome met
    other = _open_goal(ll, cases=("case-c",))
    compared = ll.run_validation(
        other["id"], card["id"],
        held_out_cases=[{"id": "case-c", "answer": "4", "reference": "4"}],
        baseline_answers=[False])
    assert compared["promoted"] is True
    # a wrong answer is a failed outcome, so no promotion
    third = _open_goal(ll, cases=("case-d",))
    failed = ll.run_validation(
        third["id"], card["id"],
        held_out_cases=[{"answer": "4", "reference": "5"}],
        baseline_answers=[True, False])
    assert failed["promoted"] is False
    # Bare case ids carry no verdict: that is "no evidence", never an
    # assumed pass.
    fourth = _open_goal(ll, cases=("case-e",))
    bare = ll.run_validation(fourth["id"], card["id"],
                             held_out_cases=["case-e"],
                             baseline_answers=[False])
    assert bare["promoted"] is False
    assert bare["validation"]["evidence"] == "no evidence"
    # Misaligned explicit outcomes are a caller error, refused outright.
    assert ll.run_validation(fourth["id"], card["id"],
                             held_out_cases=["case-e"],
                             baseline_answers=[False],
                             reference_outcomes=[True, False])["ok"] is False
    json.dumps([mapped, compared, failed, bare])


def test_skill_cards_are_text_only_and_never_executed(ll):
    payload = ('import os\nos.system("echo pwned")\n'
               'def boom():\n    return "x"\n'
               'quote = "do not run me"')
    created = ll.create_skill_card("text-only", "whenever X", payload,
                                   limitations="none", evidence="10 runs",
                                   checks="one check", provenance="practice")
    assert created["ok"] is True
    stored = ll.skill_card(created["card"]["id"])
    # Verbatim text round-trip: stored as a string, never compiled or run.
    assert stored["procedure"] == payload
    assert isinstance(stored["procedure"], str)
    assert stored["state"] == "draft"
    # No module attribute could execute card content ...
    offenders = [name for name in dir(lab_learning)
                 if FORBIDDEN_EXECUTION.search(name)]
    assert offenders == []
    # ... and the source contains no dynamic-execution call at all.
    with open(lab_learning.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for token in ("exec(", "eval(", "compile("):
        assert token not in source, token
    # Promotion only ever rides a passing validation run.
    goal = _open_goal(ll)
    assert ll.promote_goal(goal["id"])["ok"] is False
    assert ll.skill_card(created["card"]["id"])["state"] == "draft"
    assert ll.create_skill_card("", "when", "how")["ok"] is False
    assert ll.create_skill_card("n", "", "how")["ok"] is False
    assert ll.create_skill_card("n", "when", "  ")["ok"] is False
    json.dumps(stored)


def test_rollback_requires_a_new_validation_run_to_repromote(ll):
    goal = _open_goal(ll, cases=("case-a",))
    card = _make_card(ll, name="rollback-me")
    first = ll.run_validation(goal["id"], card["id"],
                              held_out_cases=["case-a"],
                              baseline_answers=[False],
                              reference_outcomes=[True])
    assert first["promoted"] is True
    assert ll.goal(goal["id"])["state"] == "promoted"
    rolled = ll.rollback(card["id"], goal["id"], reason="regression found")
    assert rolled["ok"] is True
    assert rolled["card"]["state"] == "rolled_back"
    assert rolled["card"]["rolled_back_at"] is not None
    assert rolled["card"]["promoted_at"] is not None      # history is kept
    assert ll.goal(goal["id"])["state"] == "rolled_back"
    # The OLD run can never be reused to re-promote.
    reused = ll.promote_goal(goal["id"])
    assert reused["ok"] is False
    assert "NEW validation run" in reused["reason"]
    assert ll.goal(goal["id"])["state"] == "rolled_back"
    assert ll.skill_card(card["id"])["state"] == "rolled_back"
    # A NEW passing validation run is what re-promotes.
    second = ll.run_validation(goal["id"], card["id"],
                               held_out_cases=["case-a"],
                               baseline_answers=[False],
                               reference_outcomes=[True])
    assert second["promoted"] is True
    assert ll.goal(goal["id"])["state"] == "promoted"
    refreshed = ll.skill_card(card["id"])
    assert refreshed["state"] == "promoted"
    assert refreshed["promoted_at"] >= rolled["card"]["rolled_back_at"]
    assert len(ll.validation_runs(goal_id=goal["id"])) == 2
    # Unknown ids are refused, never raised.
    assert ll.rollback("skl-deadbeef")["ok"] is False
    assert ll.skill_cards(state="promoted")[0]["id"] == card["id"]
    json.dumps(ll.skill_cards())


# ---------------------------------------------------------------------------
# Stage 5: classes, attendance, usage caps, measured benefit
# ---------------------------------------------------------------------------
def test_attendance_refuses_npc_and_manual_chars(ll):
    card = _make_card(ll)
    goal = _open_goal(ll)
    cls = ll.create_class(card["id"], goal["id"], teacher="prof",
                          members=["alice", "bob"])["class"]
    assert re.fullmatch(r"cls-[0-9a-f]{8}", cls["id"])
    ll.set_char_flags("npc-1", "npc")
    ll.set_char_flags("manual-1", ["manual"])
    npc = ll.record_attendance(cls["id"], "npc-1", "Q", "A")
    assert npc["ok"] is False and "npc" in npc["reason"]
    manual = ll.record_attendance(cls["id"], "manual-1", "Q", "A")
    assert manual["ok"] is False and "manual" in manual["reason"]
    # A refused attempt writes NOTHING, so counts stay honest.
    assert ll.attendance_count(cls["id"]) == 0
    assert ll.char_flags("npc-1") == ["npc"]
    accepted = ll.record_attendance(cls["id"], "alice", "what is X",
                                    "X is Y")
    assert accepted["ok"] is True
    assert ll.attendance_count(cls["id"]) == 1
    # Flags injected by the caller (e.g. the authority system) are honoured.
    shared = LabLearning(ll.path, char_flags={"carol": ["manual"]})
    shared.init_schema()
    refused = shared.record_attendance(cls["id"], "carol", "Q", "A")
    assert refused["ok"] is False and "manual" in refused["reason"]
    assert shared.attendance_count(cls["id"]) == 1
    shared.close()
    # Tutoring rows are not classes and take no attendance.
    request = ll.request_tutor("gap", "alice", "bob")["request"]
    assert ll.record_attendance(request["id"], "alice", "Q", "A")["ok"] is False
    assert [item["id"] for item in ll.classes()] == [cls["id"]]
    assert ll.create_class("", "", "", ["a"])["ok"] is False
    assert ll.create_class("skl-deadbeef", "", "prof", ["a"])["ok"] is False
    json.dumps(ll.classes())


def test_counts_recap_availability_and_mastery_are_separate(ll):
    card = _make_card(ll)
    goal = _open_goal(ll, cases=("case-a", "case-b"))
    cls = ll.create_class(card["id"], goal["id"], teacher="prof",
                          members=["alice", "bob"])["class"]
    assert ll.attendance_count(cls["id"]) == 0
    assert ll.late_join_recap(cls["id"]) == []      # no rows, no invention
    ll.record_attendance(cls["id"], "alice", "Q1", "A1")
    ll.record_attendance(cls["id"], "bob", "Q2", "A2")
    assert ll.attendance_count(cls["id"]) == 2
    recap = ll.late_join_recap(cls["id"])
    assert [(row["question"], row["answer"]) for row in recap] == [
        ("Q1", "A1"), ("Q2", "A2")]
    # The recap is exactly the recorded rows - nothing added.
    assert len(recap) == ll.attendance_count(cls["id"])
    for row in recap:
        assert set(row) == {"question", "answer", "attempt", "at"}
        assert row["question"] in ("Q1", "Q2")
        assert row["answer"] in ("A1", "A2")
    json.dumps(recap)

    # Availability (lesson exists) and mastery (this char passed held-out)
    # are tracked separately and CAN differ - both directions below.
    assert ll.lesson_available(cls["id"]) is False          # card is draft
    held = ll.add_exercise(goal["id"], "secret case", "ref", held_out=True)
    score = ll.record_held_out_result(goal["id"], "alice",
                                      held["exercise"]["id"], correct=True)
    assert score["ok"] is True
    assert ll.mastery_demonstrated(cls["id"], "alice") is True
    assert ll.mastery_demonstrated(cls["id"], "bob") is False
    assert ll.lesson_available(cls["id"]) is False          # mastery, no lesson
    promoted = ll.run_validation(goal["id"], card["id"],
                                 held_out_cases=["case-a", "case-b"],
                                 baseline_answers=[False],
                                 reference_outcomes=[True, True])
    assert promoted["promoted"] is True
    assert ll.lesson_available(cls["id"]) is True
    assert ll.mastery_demonstrated(cls["id"], "alice") is True
    assert ll.mastery_demonstrated(cls["id"], "bob") is False  # lesson, no mastery
    # The split cuts both ways: drills are not held-out scores and held-out
    # cases are not drills.
    drill = ll.add_exercise(goal["id"], "drill", "ref", held_out=False)
    bad = ll.record_held_out_result(goal["id"], "bob",
                                    drill["exercise"]["id"], correct=True)
    assert bad["ok"] is False and "held-out" in bad["reason"]
    assert ll.record_practice(goal["id"], "bob", held["exercise"]["id"],
                              "answer", correct=True)["ok"] is False
    assert ll.lesson_available("cls-deadbeef") is False
    assert ll.mastery_demonstrated("cls-deadbeef", "alice") is False


def test_usage_caps_and_unknown_price_is_never_reported_as_zero(ll):
    # Caps default to `cap: null`.
    fresh = ll.check_cap("practice")
    assert fresh["ok"] is True
    assert fresh["cap"] is None
    assert fresh["remaining"] is None
    assert fresh["exceeded"] is False
    # An unknown price must never be reported as 0 / $0.00.
    ll.record_usage("practice", char_id="alice", calls=3, model_calls=3,
                    tokens_in=100, tokens_out=50, cost_estimate=0.0,
                    price_known=False)
    ll.set_cap("practice", "cost", 5.0)
    cost = ll.check_cap("practice")
    assert cost["metric"] == "cost" and cost["cap"] == 5.0
    assert cost["price_known"] is False
    assert cost["remaining"] is None
    assert cost["remaining"] != 0 and cost["remaining"] != 0.0
    assert cost["exceeded"] is False        # unknown is neither free nor over
    totals = ll.usage_totals("practice")
    assert totals["price_known"] is False
    assert totals["cost_estimate"] is None
    assert totals["calls"] == 3
    # One unpriced row makes the WHOLE bucket's cost unknown.
    ll.record_usage("practice", char_id="bob", calls=2, model_calls=2,
                    tokens_in=10, tokens_out=10, cost_estimate=1.25,
                    price_known=True)
    totals = ll.usage_totals("practice")
    assert totals["price_known"] is False and totals["cost_estimate"] is None
    assert totals["calls"] == 5
    # A fully priced bucket reports a real balance.
    ll.record_usage("life", char_id="alice", calls=1, model_calls=1,
                    cost_estimate=0.5, price_known=True)
    ll.set_cap("life", "cost", 1.0)
    life = ll.check_cap("life")
    assert life["price_known"] is True
    assert life["remaining"] == 0.5
    assert life["exceeded"] is False
    # Exceeding a calls cap is visible.
    ll.set_cap("practice", "calls", 4)
    calls = ll.check_cap("practice", "calls")
    assert calls["cap"] == 4 and calls["used"] == 5
    assert calls["exceeded"] is True and calls["remaining"] == -1
    # Unknown buckets and metrics are refused, not invented.
    assert ll.record_usage("nope", calls=1)["ok"] is False
    assert ll.set_cap("practice", "dollars", 1)["ok"] is False
    assert ll.set_cap("nope", "calls", 1)["ok"] is False
    assert ll.check_cap("nope")["ok"] is False
    json.dumps([fresh, cost, totals, life, calls])


def test_measure_benefit_compares_with_and_without(ll):
    card = _make_card(ll, name="benefit-card")
    # Nothing to compare yet.
    assert ll.measure_benefit(card["id"], "fp-none") == {
        "improved": False, "reason": "insufficient data"}
    # Identical performance -> exactly the specified no-difference shape.
    ll.record_outcome("", "fp-equal", ok=True, latency_ms=100, model_calls=1)
    ll.record_outcome(card["id"], "fp-equal", ok=True, latency_ms=100,
                      model_calls=1)
    assert ll.measure_benefit(card["id"], "fp-equal") == {
        "improved": False, "reason": "no measurable difference"}
    # Better success rate WITH the skill.
    ll.record_outcome("", "fp-rate", ok=False, latency_ms=100, model_calls=1)
    ll.record_outcome(card["id"], "fp-rate", ok=True, latency_ms=100,
                      model_calls=1)
    assert ll.measure_benefit(card["id"], "fp-rate") == {
        "improved": True, "reason": "higher success rate"}
    # Same success rate, faster WITH -> still a measurable difference.
    ll.record_outcome("", "fp-speed", ok=True, latency_ms=200, model_calls=2)
    ll.record_outcome("", "fp-speed", ok=True, latency_ms=200, model_calls=2)
    ll.record_outcome(card["id"], "fp-speed", ok=True, latency_ms=50,
                      model_calls=1)
    assert ll.measure_benefit(card["id"], "fp-speed") == {
        "improved": True, "reason": "same success rate, lower latency"}
    # WITH only, no WITHOUT baseline -> insufficient data, never a guess.
    ll.record_outcome(card["id"], "fp-solo", ok=True, latency_ms=10,
                      model_calls=1)
    assert ll.measure_benefit(card["id"], "fp-solo")["reason"] == (
        "insufficient data")
    # Refusals.
    assert ll.record_outcome("skl-deadbeef", "fp", True)["ok"] is False
    assert ll.record_outcome(card["id"], "", True)["ok"] is False
    json.dumps(ll.outcomes())


def test_ids_are_opaque_and_list_outputs_are_capped(ll):
    goal = _open_goal(ll)
    exercise = ll.add_exercise(goal["id"], "drill", "ref")
    assert re.fullmatch(r"ex-[0-9a-f]{8}", exercise["exercise"]["id"])
    assert re.fullmatch(r"run-[0-9a-f]{8}",
                        ll.record_practice(goal["id"], "char-1",
                                           exercise["exercise"]["id"],
                                           "first", correct=True)["run"]["id"])
    for index in range(LIST_CAP + 5):
        ll.record_practice(goal["id"], "char-1", exercise["exercise"]["id"],
                           "answer %d" % index, correct=(index % 2 == 0))
    runs = ll.practice_runs(limit=10 ** 6)
    assert len(runs) == LIST_CAP                 # hard ceiling, not a hint
    assert len(ll.practice_runs(limit=None)) <= LIST_CAP
    assert len(ll.exercises(limit=10 ** 6)) <= LIST_CAP
    assert len(ll.goals(limit=10 ** 6)) <= LIST_CAP
    assert len(ll.skill_cards(limit=10 ** 6)) <= LIST_CAP
    assert len(ll.tutor_requests(limit=10 ** 6)) <= LIST_CAP
    # Everything this module hands back must survive json.dumps.
    payloads = [
        goal, ll.goals(), exercise, ll.exercises(), runs[0], runs,
        ll.tutor_requests(), ll.debates(), ll.validation_runs(),
        ll.skill_cards(), ll.classes(), ll.attendance_count("cls-deadbeef"),
        ll.late_join_recap("cls-deadbeef"), ll.usage_totals("life"),
        ll.check_cap("life"), ll.measure_benefit("skl-deadbeef", "fp"),
        ll.char_flags("nobody"), ll.exercise("ex-deadbeef"),
        ll.goal("goal-deadbeef"), ll.debate("deb-deadbeef"),
        ll.skill_card("skl-deadbeef"), ll.get_class("cls-deadbeef"),
        ll.validation_run("val-deadbeef"), ll.practice_run("run-deadbeef"),
    ]
    for payload in payloads:
        json.dumps(payload)

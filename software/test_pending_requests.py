"""Round ZB-PE: the pending-request store.

Written before the server wiring, so these define the contract rather than
describe whatever the server happens to do. The server adds I/O (voice, Telegram,
n8n callback) on top; everything about identity, ordering, escalation and
persistence is decided here and is testable without any of it.

THE BUG BEING FIXED
-------------------
n8n asked a question only Sir can answer, and FRIDAY answered the workflow with
the question itself - handing back what the workflow already knew and telling it
nothing. The question has to be parked, asked of Sir, and held open across a
local answer, a Telegram one, or neither, and across restarts.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta

from source.server.pending_requests import (  # noqa: E402
    MADRID,
    MAX_HELD,
    MAX_OPEN,
    STAGE_LOCAL,
    STAGE_PENDING,
    STAGE_TELEGRAM,
    STATE_ANSWERED,
    STATE_EXPIRED,
    STATE_IGNORED,
    STATE_OPEN,
    PendingRequest,
    PendingStore,
    resolve_owner_decision,
    now_madrid,
    timestamp,
)

QUESTION = ("Señor, le ha llegado un correo de Saihajdeep Singh Kaur. "
            "¿Desea que le agende una llamada hoy de 16:00 a 16:15?")


def _store(tmp, **kwargs):
    path = os.path.join(tmp, "pending.json")
    kwargs.setdefault("local_wait", 120.0)
    kwargs.setdefault("telegram_wait", 300.0)
    return PendingStore(path=path, environ={"FRIDAY_PENDING_PATH": path}, **kwargs)


class TestIdentity:
    def test_a_request_gets_an_opaque_correlation_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = _store(tmp).add(QUESTION)
            assert request.id.startswith("pr-")
            assert len(request.id) == 11      # "pr-" + 8 hex
            assert QUESTION not in request.id  # never derived from content

    def test_ids_are_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            ids = set()
            for i in range(20):
                request = store.add("question %d" % i)
                assert request is not None, "add refused at %d" % i
                ids.add(request.id)
                store.resolve(request.id, "done")   # keep the open count low
            assert len(ids) == 20

    def test_the_id_is_what_n8n_sees(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = _store(tmp).add(QUESTION)
            view = request.public_view()
            assert view["correlation_id"] == request.id
            assert view["question"] == QUESTION


class TestEscalationWindow:
    def test_a_fresh_request_waits_at_the_pc_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = _store(tmp).add(QUESTION)
            assert request.stage == STAGE_LOCAL
            assert request.state == STATE_OPEN

    def test_local_wait_is_two_minutes_and_telegram_five(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = _store(tmp).add(QUESTION)
            created = datetime.fromisoformat(request.created_at)
            local = datetime.fromisoformat(request.local_deadline)
            telegram = datetime.fromisoformat(request.telegram_deadline)
            assert (local - created).total_seconds() == 120.0
            assert (telegram - local).total_seconds() == 300.0

    def test_overdue_is_false_before_the_deadline_and_true_after(self):
        now = now_madrid()
        request = PendingRequest(QUESTION, now=now)
        assert request.overdue_for(STAGE_LOCAL, now=now) is False
        assert request.overdue_for(STAGE_LOCAL,
                                   now=now + timedelta(seconds=130)) is True

    def test_advance_goes_forward_only(self):
        now = now_madrid()
        request = PendingRequest(QUESTION, now=now)
        assert request.advance_to(STAGE_TELEGRAM) is True
        assert request.stage == STAGE_TELEGRAM
        assert request.advance_to(STAGE_LOCAL) is False, "must not move backwards"
        assert request.advance_to(STAGE_PENDING) is True
        assert request.stage == STAGE_PENDING

    def test_advance_refuses_an_unknown_stage(self):
        request = PendingRequest(QUESTION)
        assert request.advance_to("email") is False


class TestResolution:
    def test_resolve_records_the_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            request = store.add(QUESTION)
            assert store.resolve(request.id, "Yes, schedule it and reply.") is True
            assert request.state == STATE_ANSWERED
            assert request.answer == "Yes, schedule it and reply."

    def test_resolution_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            request = store.add(QUESTION)
            store.resolve(request.id, "first")
            assert store.resolve(request.id, "second") is False
            assert request.answer == "first", "a later answer must not overwrite"

    def test_ignore_removes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            request = store.add(QUESTION)
            assert store.decide(request.id, "", ignore=True) is request
            assert request.state == STATE_IGNORED
            assert store.oldest_open() is None

    def test_expire_is_terminal(self):
        request = PendingRequest(QUESTION)
        request.expire()
        assert request.state == STATE_EXPIRED
        assert request.resolve("too late") is False


class TestLateRepliesHitTheRightRequest:
    """Requirement: a late reply must resolve the correct pending request."""

    def test_a_decision_can_target_the_newer_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            first = store.add("question one")
            second = store.add("question two")
            assert store.resolve(second.id, "yes") is True
            assert first.state == STATE_OPEN
            assert second.answer == "yes"
            assert store.oldest_open() is first

    def test_an_unknown_request_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            assert store.resolve("pr-00000000", "yes") is False
            assert store.get("pr-00000000") is None


class TestPersistence:
    def test_it_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pending.json")
            first = PendingStore(path=path, environ={"FRIDAY_PENDING_PATH": path})
            request = first.add(QUESTION)
            first.advance_oldest(STAGE_TELEGRAM)

            reborn = PendingStore(path=path, environ={"FRIDAY_PENDING_PATH": path})
            survivor = reborn.get(request.id)
            assert survivor is not None, "the pending question did not survive a restart"
            assert survivor.text == QUESTION
            assert survivor.stage == STAGE_TELEGRAM, "escalation stage must survive"
            assert survivor.state == STATE_OPEN
            assert survivor.local_deadline == request.local_deadline
            assert survivor.telegram_deadline == request.telegram_deadline

    def test_the_answer_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pending.json")
            first = PendingStore(path=path, environ={"FRIDAY_PENDING_PATH": path})
            request = first.add(QUESTION)
            first.resolve(request.id, "Yes")
            reborn = PendingStore(path=path, environ={"FRIDAY_PENDING_PATH": path})
            assert reborn.get(request.id).answer == "Yes"

    def test_timestamps_are_europe_madrid(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = _store(tmp).add(QUESTION)
            parsed = datetime.fromisoformat(request.created_at)
            assert parsed.tzinfo is not None
            assert parsed.utcoffset() == MADRID.utcoffset(parsed)
            assert timestamp().endswith("+02:00") or timestamp().endswith("+01:00")

    def test_a_corrupt_file_does_not_stop_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pending.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{ not json at all")
            store = PendingStore(path=path, environ={"FRIDAY_PENDING_PATH": path})
            assert len(store) == 0, "a corrupt log must not raise, just start clean"
            assert os.path.exists(path + ".corrupt"), "the corrupt file must be kept"
            assert store.add(QUESTION) is not None, "the store must still work"

    def test_a_wrongly_typed_file_does_not_stop_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pending.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"requests": "not a list"}, handle)
            assert len(PendingStore(path=path,
                                    environ={"FRIDAY_PENDING_PATH": path})) == 0

    def test_the_file_is_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            store.add(QUESTION)
            with open(store.path, encoding="utf-8") as handle:
                data = json.load(handle)
            assert data["version"] == 1
            assert len(data["requests"]) == 1


class TestBounds:
    def test_open_requests_are_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            added = [store.add("question %d" % i) for i in range(MAX_OPEN + 5)]
            open_count = len(store.open_requests())
            assert open_count == MAX_OPEN, open_count
            assert all(item is None for item in added[MAX_OPEN:]), (
                "adding past the cap must refuse, not silently queue")

    def test_the_held_log_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            for i in range(MAX_HELD + 30):
                request = store.add("question %d" % i)
                if request is not None:
                    store.resolve(request.id, "done")
            assert len(store) <= MAX_HELD

    def test_an_empty_question_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            assert store.add("   ") is None


class TestSweep:
    def test_resolved_entries_are_dropped_after_a_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pending.json")
            store = PendingStore(path=path, environ={"FRIDAY_PENDING_PATH": path})
            request = store.add(QUESTION)
            store.resolve(request.id, "yes")
            # Age the resolution by rewriting it.
            store.get(request.id).resolved_at = timestamp(
                now_madrid() - timedelta(days=2))
            store.save()
            assert store.sweep() == 1
            assert store.get(request.id) is None

    def test_open_requests_are_never_swept(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            request = store.add(QUESTION)
            assert store.sweep() == 0
            assert store.get(request.id) is not None
            assert request.state == STATE_OPEN


class TestProcessWideStore:
    """The brain's `friday_pending()` and the server must share ONE store.

    Two writers on one log is how a parked question gets lost.
    """

    def test_registering_a_store_is_what_everyone_reads(self):
        from source.server.pending_requests import get_pending_store
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shared.json")
            mine = PendingStore(path=path,
                                environ={"FRIDAY_PENDING_PATH": path})
            shared = get_pending_store(mine)
            try:
                assert shared is mine, "the registered store must be the one read"
                assert get_pending_store() is mine, "and it must be reused"
                mine.add(QUESTION)
                assert len(get_pending_store().open_requests()) == 1
            finally:
                from source.server import pending_requests as module
                module._ACTIVE_STORE = None

    def test_it_creates_one_only_when_none_is_registered(self):
        from source.server import pending_requests as module
        from source.server.pending_requests import get_pending_store
        module._ACTIVE_STORE = None
        try:
            first = get_pending_store()
            assert isinstance(first, PendingStore)
            assert get_pending_store() is first, "must not build a second store"
        finally:
            module._ACTIVE_STORE = None

    def test_the_open_view_is_safe_to_hand_to_the_brain(self):
        from source.server.pending_requests import get_pending_store
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "view.json")
            store = PendingStore(path=path,
                                 environ={"FRIDAY_PENDING_PATH": path})
            try:
                get_pending_store(store)
                store.add(QUESTION)
                view = [item.public_view() for item in get_pending_store().open_requests()]
                assert len(view) == 1
                assert view[0]["state"] == "open"
                assert view[0]["question"] == QUESTION
                assert view[0]["correlation_id"].startswith("pr-")
            finally:
                from source.server import pending_requests as module
                module._ACTIVE_STORE = None


class TestAwaitingAndReminding:
    """The three behaviours that decide whether an utterance is consumed.

    These are the ones a mutation would otherwise slip past: an over-eager
    resolver swallowing a new command, a reminder that nags every turn, and a
    parked question that lingers forever.
    """

    def test_the_reminder_fires_once_then_stops(self):
        from source.server.pending_requests import next_unreminded
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.json")
            store = PendingStore(path=path, environ={"FRIDAY_PENDING_PATH": path})
            store.add(QUESTION)
            store.advance_oldest(STAGE_PENDING)
            first = next_unreminded(store)
            assert first is not None, "a parked question must be announced"
            first.reminded = True
            store.save()
            assert next_unreminded(store) is None, (
                "the reminder nagged a second time")

    def test_an_active_wait_is_not_reminded(self):
        from source.server.pending_requests import next_unreminded
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.json")
            store = PendingStore(path=path, environ={"FRIDAY_PENDING_PATH": path})
            store.add(QUESTION)
            assert next_unreminded(store) is None, (
                "a question still being asked for must not be announced again")

    def test_the_reminder_flag_survives_a_restart(self):
        from source.server.pending_requests import next_unreminded
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.json")
            store = PendingStore(path=path, environ={"FRIDAY_PENDING_PATH": path})
            request = store.add(QUESTION)
            store.advance(request.id, STAGE_PENDING)
            next_unreminded(store).reminded = True
            store.save()
            reborn = PendingStore(path=path,
                                  environ={"FRIDAY_PENDING_PATH": path})
            assert next_unreminded(reborn) is None, (
                "a restart must not restart the nagging")

    def test_sweep_expires_a_parked_question_left_over_night(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.json")
            store = PendingStore(path=path, environ={"FRIDAY_PENDING_PATH": path})
            request = store.add(QUESTION)
            request.advance_to(STAGE_PENDING)
            # Parked long ago: past the sweep's one-hour grace on the telegram
            # deadline, which is what makes it "left overnight" rather than
            # "just been asked".
            request.telegram_deadline = timestamp(
                now_madrid() - timedelta(hours=2))
            store.save()
            store.sweep()
            assert request.state == STATE_EXPIRED, (
                "a parked question lingers forever instead of being given up on")
            assert store.oldest_open() is None


class TestOwnerDecision:
    def test_unrelated_ambiguous_wrong_domain_and_wrong_source_do_not_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            first = store.add("question one")
            second = store.add("question two")
            other = store.add("different domain", source="lab")
            callbacks = []

            def notify(*args, **kwargs):
                callbacks.append((args, kwargs))
                return {}

            def decide(text, source="text"):
                return resolve_owner_decision(store, text, source=source, notify=notify)

            for ordinary in ("How is the weather?", "yes", "forget it", "Approve artifact 7"):
                assert decide(ordinary) is None
            question = decide("n8n decision: yes")
            assert "Which n8n request?" in question and first.id in question and second.id in question
            assert other.id not in question
            assert "Use an n8n request ID" in decide("n8n decision lab-1: yes")
            assert "No open n8n request" in decide(f"n8n decision {other.id}: yes")
            assert "Only the owner" in decide(f"n8n decision {first.id}: yes", source="n8n")
            assert "Give an answer" in decide(f"n8n decision {first.id}:")
            assert all(request.is_open() for request in (first, second, other))
            assert callbacks == []

    def test_stale_open_request_is_refused_even_without_prior_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            request = store.add(QUESTION, now=now_madrid() - timedelta(hours=3))
            assert request.stage == STAGE_LOCAL
            assert not store.resolve(request.id, "too late")
            assert store.decision_targets() == []
            assert store.sweep() == 0
            assert _store(tmp).get(request.id).state == STATE_EXPIRED

    def test_failed_persistence_leaves_the_request_open_for_an_explicit_retry(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            request = store.add(QUESTION)
            callbacks = []

            def notify(*args, **kwargs):
                callbacks.append((args, kwargs))
                return {"http_status": 200, "result": {"ok": True}}

            command = f"n8n decision {request.id}: yes"
            with patch.object(store, "_save_locked", side_effect=OSError("synthetic failure")):
                result = resolve_owner_decision(store, command, source="voice", notify=notify)
            assert "couldn't save" in result and callbacks == []
            assert request.is_open() and _store(tmp).get(request.id).is_open()
            result = resolve_owner_decision(store, command, source="voice", notify=notify)
            assert "reports application success" in result and len(callbacks) == 1
            assert _store(tmp).get(request.id).answer == "yes"

    def test_simultaneous_duplicate_callbacks_have_one_persisted_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            request = store.add(QUESTION)
            callbacks = []
            barrier = Barrier(2)

            def notify(item, **kwargs):
                assert _store(tmp).get(item.id).answer == kwargs["answer"]
                callbacks.append(item.id)
                return {"http_status": 200, "result": "accepted at transport"}

            def decide(answer):
                barrier.wait(timeout=5)
                return resolve_owner_decision(
                    store, f"n8n decision {request.id}: {answer}", source="telegram", notify=notify,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                replies = list(pool.map(decide, ("yes", "no"), timeout=10))
            assert callbacks == [request.id]
            assert sum("Decision recorded" in reply for reply in replies) == 1
            assert any("acceptance is unconfirmed" in reply for reply in replies)
            assert any("already resolved" in reply for reply in replies)
            replay = resolve_owner_decision(
                _store(tmp), f"n8n decision {request.id}: retry", source="text", notify=notify,
            )
            assert "already resolved" in replay and callbacks == [request.id]

    def test_callback_result_distinguishes_transport_and_application(self):
        from source.server.pending_requests import pending_callback_receipt

        target = "pr-12345678"
        cases = [
            ({"ok": True, "http_status": 200, "result": {}}, "acceptance is unconfirmed"),
            ({"ok": True, "http_status": 200, "result": {"ok": False}}, "application failure"),
            ({"ok": True, "http_status": 200, "result": {"ok": True}}, "application success"),
            ({"http_status": 200, "result": {"ok": True, "correlation_id": "pr-00000000"}},
             "acceptance is unconfirmed"),
            ({"http_status": 503}, "returned HTTP 503"),
            ({"ok": False, "skipped": True}, "receipt is unconfirmed"),
        ]
        for result, expected in cases:
            receipt = pending_callback_receipt(target, result)
            assert expected in receipt
            assert "Workflow completion is unverified" in receipt

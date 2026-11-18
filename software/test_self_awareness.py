"""Tests for self-awareness: live state, capabilities, and honest limitations."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.getcwd())

from source.server import self_awareness as sa


class SelfAwarenessTests(unittest.TestCase):
    def test_self_state_shape(self):
        state = sa.self_state()
        for key in ("collected_at", "process", "machine", "local_brain", "telegram",
                    "n8n", "server", "skills", "cloud_pools"):
            self.assertIn(key, state)
        self.assertIn(state["process"]["pid"], range(1, 10 ** 9))

    def test_self_state_never_raises_when_services_are_down(self):
        # Every probe must degrade to a reported error, never an exception.
        with mock.patch.object(sa, "_ollama_models", return_value={"ok": False, "error": "down"}):
            with mock.patch.object(sa, "_port_bound", return_value=False):
                state = sa.self_state()
        self.assertFalse(state["local_brain"]["ok"])
        self.assertFalse(state["server"]["listening"])

    def test_who_am_i_is_readable_and_honest(self):
        text = sa.who_am_i()
        self.assertIn("FRIDAY self-report", text)
        self.assertIn("Skills I've saved", text)
        # n8n must be described truthfully, never assumed connected.
        self.assertIn("n8n", text)

    def test_capabilities_lists_every_advertised_family(self):
        caps = sa.capabilities()
        for family in ("computer_health", "task_list", "n8n_workflows", "self_awareness",
                       "self_extension", "windows_control", "web"):
            self.assertIn(family, caps)
        self.assertIn("system_health", caps["computer_health"]["functions"])
        self.assertIn("TaskStore", caps["task_list"]["import"])

    def test_limitations_are_populated_and_honest(self):
        limits = sa.limitations()
        self.assertTrue(limits["cannot"])
        self.assertTrue(limits["degrade_gracefully"])
        self.assertTrue(limits["honesty_rules"])
        joined = " ".join(limits["cannot"]).lower()
        self.assertIn("wmic", joined)
        self.assertIn("kill", joined)

    def test_skill_status_reports_the_latch_caveat(self):
        status = sa.skill_status()
        self.assertIn("skills", status)
        self.assertIn("restart", status["restart_note"])
        self.assertIn("how_to_create", status)

    def test_awareness_brief_is_short_and_states_boundaries(self):
        brief = sa.awareness_brief()
        self.assertIn("=== WHO YOU ARE", brief)
        self.assertIn("self_awareness", brief)
        # The brief must NOT try to embed volatile numbers that could go stale.
        self.assertNotIn("CPU", brief)
        self.assertLess(len(brief), 1200)

    def test_no_secrets_in_state(self):
        # Tokens/keys must never appear in anything the model can read.
        state = repr(sa.self_state()) + sa.who_am_i()
        self.assertNotIn("api_key", state)
        self.assertNotIn("token", state.lower().replace("telegram token", ""))


if __name__ == "__main__":
    unittest.main()
    print("PASS: self awareness")


class SkillCatalogTests(unittest.TestCase):
    """Skills must carry APPLICABILITY, not just be a filename.

    The prompt used to receive `['name()']` - a name with no hint of what it was
    for, which is why the skill library stayed empty in practice.
    """

    def _write(self, body):
        import tempfile as _tf
        root = os.path.join(_tf.mkdtemp(prefix="friday_sk_"))
        with open(os.path.join(root, "move_app.py"), "w", encoding="utf-8") as fh:
            fh.write(body)
        return root


class SkillCatalogTests(unittest.TestCase):
    """Skills must carry APPLICABILITY, not just be a filename.

    The prompt used to receive `['name()']` - a name with no hint of what it was
    for, which is why the skill library stayed empty in practice.
    """

    def _skill_dir(self, body, name="move_app.py"):
        import tempfile as _tf
        root = _tf.mkdtemp(prefix="friday_sk_")
        os.makedirs(os.path.join(root, "skills"), exist_ok=True)
        with open(os.path.join(root, "skills", name), "w", encoding="utf-8") as fh:
            fh.write(body)
        return root  # skill_catalog() takes the PARENT dir

    def test_catalog_exposes_when_steps_and_uses(self):
        from source.server.self_awareness import skill_catalog

        folder = self._skill_dir(
            "# friday-skill: v1\n"
            "# when: Sir asks to move an app between monitors\n"
            "# verify: the window rect spans the target monitor\n"
            "# uses: 3\n"
            "def move_app(step=0):\n"
            "    steps = ['resolve handle', 'SetWindowPos it']\n"
        )
        catalog = skill_catalog(root=folder)
        self.assertEqual(len(catalog), 1)
        entry = catalog[0]
        self.assertEqual(entry["name"], "move_app")
        self.assertIn("move an app", entry["when_to_use"])
        self.assertEqual(entry["uses"], 3)
        self.assertEqual(entry["steps"], ["resolve handle", "SetWindowPos it"])
        self.assertIn("rect spans", entry["verify"])

    def test_catalog_survives_a_malformed_skill_file(self):
        from source.server.self_awareness import skill_catalog

        folder = self._skill_dir("def broken(:\n", name="broken.py")
        catalog = skill_catalog(root=folder)
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["name"], "broken")

    def test_catalog_is_empty_not_broken_for_a_missing_dir(self):
        from source.server.self_awareness import skill_catalog

        import tempfile as _tf
        self.assertEqual(skill_catalog(root=_tf.mkdtemp()), [])


class KernelSkillHelperTests(unittest.TestCase):
    """friday_save_skill / friday_load_skill / friday_use_skill had ZERO tests.

    These run the REAL kernel setup_code in a temp CWD so the generated skill
    file is exercised exactly as FRIDAY would exercise it.
    """

    def _in_folder(self, folder):
        """Exec the REAL kernel setup_code with folder as the CWD, and keep it.

        _friday_skill_dir() is `os.path.abspath('./skills')`, so the CWD must
        still be the temp folder when friday_save_skill() runs. Restoring the
        CWD before the save made these tests write into the REAL project skills
        directory - which is exactly what happened once.
        """
        import os as _os
        from source.server.profiles.default import setup_code

        class _Any:
            """Permissive stub for the OI kernel globals setup_code touches."""

            def __getattr__(self, name):
                return _Any()

            def __setattr__(self, name, value):
                pass

            def __call__(self, *a, **k):
                return _Any()

        scope = {"__name__": "__friday_test__", "computer": _Any(),
                 "interpreter": _Any(), "os": _os}
        _os.chdir(folder)
        try:
            exec(compile(setup_code, "<setup_code>", "exec"), scope)
            return _folder_scope(scope, folder, _os)
        finally:
            pass


class _folder_scope:
    """Context manager wrapper: restores the CWD when the block exits."""

    def __init__(self, scope, folder, os_mod):
        self.scope = scope
        self.folder = folder
        self.os = os_mod
        self.prev = os_mod.getcwd()

    def __enter__(self):
        return self.scope

    def __exit__(self, *exc):
        self.os.chdir(self.prev)
        return False

    def test_save_list_load_and_use_round_trip(self):
        import tempfile as _tf
        folder = _tf.mkdtemp(prefix="friday_kernel_")
        k = self._kernel(folder)
        result = k["friday_save_skill"](
            "Move App To Monitor",
            steps=["resolve the window handle", "SetWindowPos onto the target"],
            when_to_use="Sir asks to move an app to another monitor",
            verify="the rect spans the target monitor",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["saved"], "move_app_to_monitor")
        # Immediately callable - that is the whole point of these helpers.
        self.assertTrue(callable(k.get("move_app_to_monitor")))

        listing = k["friday_skills"]()
        self.assertEqual(listing["count"], 1)
        entry = listing["skills"][0]
        self.assertEqual(entry["name"], "move_app_to_monitor")
        self.assertIn("move an app", entry["when_to_use"])
        self.assertEqual(len(entry["steps"]), 2)
        self.assertIn("move_app_to_monitor", listing["callable_now"])

        self.assertTrue(k["friday_load_skill"]("move_app_to_monitor")["ok"])
        used = k["friday_use_skill"]("move_app_to_monitor")
        self.assertTrue(used["ok"])
        self.assertEqual(used["uses"], 1)
        self.assertEqual(k["friday_skills"]()["skills"][0]["uses"], 1)

    def test_missing_skill_reports_what_is_available(self):
        import tempfile as _tf
        with self._in_folder(_tf.mkdtemp(prefix="friday_kernel_")) as k:
            out = k["friday_load_skill"]("does_not_exist")
            self.assertFalse(out["ok"])
            self.assertIn("available", out)

    def test_save_list_load_and_use_round_trip(self):
        """Save -> list (with applicability) -> load -> record a use."""
        import tempfile as _tf
        folder = _tf.mkdtemp(prefix="friday_kernel_")
        with self._in_folder(folder) as k:
            result = k["friday_save_skill"](
                "Move App To Monitor",
                steps=["resolve the window handle", "SetWindowPos onto the target"],
                when_to_use="Sir asks to move an app to another monitor",
                verify="the rect spans the target monitor",
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["saved"], "move_app_to_monitor")
            # Immediately callable - the whole point of these helpers.
            self.assertTrue(callable(k.get("move_app_to_monitor")))

            listing = k["friday_skills"]()
            self.assertEqual(listing["count"], 1)
            entry = listing["skills"][0]
            self.assertEqual(entry["name"], "move_app_to_monitor")
            self.assertIn("move an app", entry["when_to_use"])
            self.assertEqual(len(entry["steps"]), 2)
            self.assertIn("move_app_to_monitor", listing["callable_now"])

            self.assertTrue(k["friday_load_skill"]("move_app_to_monitor")["ok"])
            used = k["friday_use_skill"]("move_app_to_monitor")
            self.assertTrue(used["ok"])
            self.assertEqual(used["uses"], 1)
            self.assertEqual(k["friday_skills"]()["skills"][0]["uses"], 1)

        # And it must have landed in the TEMP folder, never the real one.
        self.assertTrue(os.path.exists(
            os.path.join(folder, "skills", "move_app_to_monitor.py")))

    def test_saved_skill_file_is_plain_readable_python(self):
        """The skill must be inspectable on disk, not an opaque blob."""
        import tempfile as _tf
        folder = _tf.mkdtemp(prefix="friday_kernel_")
        with self._in_folder(folder) as k:
            result = k["friday_save_skill"]("demo", steps=["one", "two"],
                                            when_to_use="never")
            self.assertTrue(result.get("ok"), result)
            path = result["path"]
            self.assertTrue(os.path.exists(path), path)
            self.assertEqual(os.path.basename(path), "demo.py")
            src = open(path, encoding="utf-8").read()
            compile(src, path, "exec")
            self.assertIn("# when:", src)
            self.assertIn("'one'", src)

    def test_skill_writes_never_touch_the_real_skills_dir(self):
        """Guard: a test must not leave junk in the live project."""
        import tempfile as _tf
        from source.server.self_awareness import skill_catalog

        real_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__import__("source.server.self_awareness",
                                       fromlist=["x"]).__file__))))
        before = set(skill_catalog(root=real_root))
        with self._in_folder(_tf.mkdtemp(prefix="friday_kernel_")) as k:
            k["friday_save_skill"]("temp_only_skill", steps=["x"])
        after = set(skill_catalog(root=real_root))
        self.assertEqual(before, after,
                         "a test leaked a skill into the real skills dir")


class ModelSelfAwarenessTruthTests(unittest.TestCase):
    """Self-awareness must report FACTS. These caught two lies.

    `runtime_models()` reported the LOCAL vision model and said "local vision
    model is tried first". Modal has been the primary vision engine since
    2026-09-30, so the answer was confidently wrong. And STT exposed no
    last_stage at all while TTS did, so she could recite the hearing chain but
    never say which engine actually won.
    """

    def test_vision_reports_modal_as_primary_not_local(self):
        vision = sa.runtime_models()["vision"]
        self.assertIn("Modal", vision["primary"])
        self.assertNotIn("local", vision["primary"].lower())
        self.assertIn("first", vision["note"].lower())
        self.assertIn("fallback", vision["note"].lower())

    def test_vision_still_reports_its_local_fallback(self):
        vision = sa.runtime_models()["vision"]
        self.assertTrue(vision["local_fallback_model"],
                        "she must still know what the offline fallback is")

    def test_vision_fallback_chain_is_read_from_the_real_code(self):
        from source.server import screen_understanding as su
        vision = sa.runtime_models()["vision"]
        self.assertEqual([str(m) for m in su.LOCAL_VISION_CHAIN],
                         vision["local_fallback_chain"])

    def test_model_report_does_not_claim_local_vision_first(self):
        report = sa.model_report()
        self.assertNotIn("local qwen2.5vl:3b first", report)
        self.assertNotIn("tier", report.lower())

    def test_stt_exposes_a_last_stage_key(self):
        self.assertIn("last_stage", sa.runtime_models()["stt"])

    def test_stt_last_stage_is_set_by_a_real_transcription(self):
        from source.server import cloud_stt
        original = cloud_stt.LAST_STAGE
        original_run = cloud_stt.api_pools.run_chain
        try:
            cloud_stt.api_pools.run_chain = lambda *a, **k: (True, "hello", "stt_groq", "")
            self.assertEqual(cloud_stt.transcribe_wav(b"RIFFfake"), ("hello", "stt_groq", ""))
            self.assertEqual(cloud_stt.LAST_STAGE, "stt_groq")
            self.assertEqual(sa.runtime_models()["stt"]["last_stage"], "stt_groq")
            # a failed chain must NOT leave a stale engine claimed
            cloud_stt.api_pools.run_chain = lambda *a, **k: (False, None, "", "all failed")
            cloud_stt.transcribe_wav(b"RIFFfake")
            self.assertIsNone(cloud_stt.LAST_STAGE)
        finally:
            cloud_stt.api_pools.run_chain = original_run
            cloud_stt.LAST_STAGE = original

    def test_runtime_models_never_leaks_a_key(self):
        from source.server import api_pools
        blob = repr(sa.runtime_models()).lower()
        for marker in ("wk-", "ws-", "bearer", "apikey", "api_key"):
            self.assertNotIn(marker, blob, "self-report leaked %r" % marker)
        self.assertTrue(api_pools is not None)


class ProgressiveSkillDisclosureTests(KernelSkillHelperTests):
    """OpenCode Skills progressive disclosure, proven across a RESTART.

    Before: every skill's steps + verify line were pasted into the system prompt
    EVERY turn, so the prompt grew with every skill ever saved and she paid for
    procedures she would never use. Worse, a fresh process could not even read a
    saved skill back - the prompt said "call name(step=0)", but the function did
    not exist until friday_load_skill() had been called by name.
    """

    STEPS = ["resolve the window handle",
             "SetWindowPos it onto the target monitor",
             "log the resulting rect"]
    WHEN = "Sir asks to move/shift an app or window to another monitor"
    VERIFY = "window rect now spans the target monitor"

    def _save(self, kernel):
        return kernel["friday_save_skill"]("move_app_to_other_monitor",
                                           steps=self.STEPS,
                                           when_to_use=self.WHEN,
                                           verify=self.VERIFY)

    def test_prompt_block_is_metadata_only(self):
        import tempfile as _tf
        import os as _os
        folder = _tf.mkdtemp(prefix="friday_skill_prompt_")
        with self._in_folder(folder) as k:
            self._save(k)
            block = sa.skills_for_prompt(root=folder)
        self.assertIn("move_app_to_other_monitor", block)
        self.assertIn(self.WHEN, block)
        # The procedure itself must NOT be in the always-on prompt.
        for step in self.STEPS:
            self.assertNotIn(step, block)
        self.assertNotIn(self.VERIFY, block)

    def test_prompt_block_stays_small_as_the_library_grows(self):
        import tempfile as _tf
        import os as _os
        folder = _tf.mkdtemp(prefix="friday_skill_growth_")
        with self._in_folder(folder) as k:
            for index in range(25):
                k["friday_save_skill"]("skill_number_%d" % index,
                                       steps=["step %d of a long procedure" % s
                                              for s in range(20)],
                                       when_to_use="task type %d occurs" % index)
            block = sa.skills_for_prompt(root=folder)
        self.assertEqual(len(block) <= 1400, True,
                         "prompt block grew past its bound: %d chars" % len(block))

    def test_procedure_is_retrievable_on_demand(self):
        import tempfile as _tf
        with self._in_folder(_tf.mkdtemp(prefix="friday_skill_proc_")) as k:
            self._save(k)
            procedure = k["friday_skill_procedure"]("move_app_to_other_monitor")
        for step in self.STEPS:
            self.assertIn(step, procedure)
        self.assertIn(self.VERIFY, procedure)

    def test_unknown_skill_lookup_is_honest(self):
        import tempfile as _tf
        with self._in_folder(_tf.mkdtemp(prefix="friday_skill_missing_")) as k:
            reply = k["friday_skill_procedure"]("no_such_skill")
        self.assertIn("no saved skill", reply)

    def test_skill_is_discovered_and_callable_after_a_restart(self):
        """The real gap: a NEW process must find the skill and run it."""
        import tempfile as _tf
        import os as _os
        folder = _tf.mkdtemp(prefix="friday_skill_restart_")
        # process 1 saves
        with self._in_folder(folder) as first:
            self.assertTrue(self._save(first)["ok"])
        # process 2 is a completely fresh exec of setup_code, same folder
        with self._in_folder(folder) as second:
            catalog = second["friday_skills"]()
            self.assertIn("move_app_to_other_monitor", catalog["names"])
            # discoverable via metadata, and its steps are readable with no
            # load step and no restart
            self.assertIn(
                "resolve the window handle",
                second["friday_skill_procedure"]("move_app_to_other_monitor"),
            )
            self.assertTrue(second["friday_load_skill"](
                "move_app_to_other_monitor")["ok"])
            self.assertTrue(callable(second.get("move_app_to_other_monitor")),
                            "skill was not callable after loading")
            # A saved skill is step-wise: it prints the step and returns None,
            # so the proof is what it emitted, not a return value.
            import contextlib as _cl
            import io as _io
            buf = _io.StringIO()
            with _cl.redirect_stdout(buf):
                second["move_app_to_other_monitor"](step=0)
                second["move_app_to_other_monitor"](step=1)
            spoken = buf.getvalue()
            self.assertIn("STEP 1 of 3", spoken)
            self.assertIn("STEP 2 of 3", spoken)
            self.assertIn("resolve the window handle", spoken)
            self.assertEqual(second["friday_use_skill"](
                "move_app_to_other_monitor")["uses"], 1)

    def test_usage_count_persists_across_processes(self):
        import tempfile as _tf
        folder = _tf.mkdtemp(prefix="friday_skill_uses_")
        with self._in_folder(folder) as first:
            self._save(first)
        for expected in (1, 2):
            with self._in_folder(folder) as k:
                self.assertEqual(
                    k["friday_use_skill"]("move_app_to_other_monitor")["uses"],
                    expected)


class ConversationHistoryBoundTests(unittest.TestCase):
    # 2026-10-01: `interpreter.messages` was UNBOUNDED (measured at 66 and
    # climbing, reset only by a process restart). The sole bound was tokentrim's
    # ~95,879-token budget deep inside the LLM call - not a message count, and
    # not ours. ChatBuffer(100) is the UI transcript and never reached the model.
    # Sir asked for at least the last 100 messages; 200 keeps a safety margin.

    class _FakeInterpreter:
        def __init__(self, messages):
            self.messages = messages

    @staticmethod
    def _chat(n, period=5):
        return [{"role": "user" if i % period == 0 else "assistant",
                 "content": "m%d" % i} for i in range(n)]

    def test_history_is_trimmed_to_two_hundred(self):
        interpreter = self._FakeInterpreter(self._chat(500))
        dropped, kept = sa.trim_conversation_history(interpreter, keep=200)
        self.assertEqual(len(interpreter.messages), 200)
        self.assertEqual((dropped, kept), (300, 200))

    def test_short_history_is_untouched(self):
        messages = self._chat(12)
        interpreter = self._FakeInterpreter(messages)
        self.assertEqual(sa.trim_conversation_history(interpreter, keep=200), (0, 12))
        self.assertEqual(len(interpreter.messages), 12)

    def test_window_never_starts_on_an_orphaned_tool_result(self):
        messages = [{"role": "user", "content": "u%d" % i} if i % 3 == 0
                    else {"role": "tool", "content": "t%d" % i} for i in range(300)]
        interpreter = self._FakeInterpreter(messages)
        sa.trim_conversation_history(interpreter, keep=100)
        self.assertEqual(interpreter.messages[0]["role"], "user")

    def test_newest_message_is_always_retained(self):
        interpreter = self._FakeInterpreter(self._chat(451))
        sa.trim_conversation_history(interpreter, keep=200)
        self.assertEqual(interpreter.messages[-1]["content"], "m450")

    def test_keep_is_clamped_to_a_usable_floor(self):
        interpreter = self._FakeInterpreter(self._chat(300))
        sa.trim_conversation_history(interpreter, keep=1)
        self.assertGreaterEqual(len(interpreter.messages), 20)

    def test_missing_or_odd_history_does_not_raise(self):
        class Empty:
            messages = None
        self.assertEqual(sa.trim_conversation_history(Empty(), keep=200)[0], 0)
        self.assertEqual(sa.trim_conversation_history(object(), keep=200)[0], 0)

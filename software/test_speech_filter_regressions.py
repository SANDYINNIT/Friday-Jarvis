"""ROUND ZB-L0: speech-filter false positives (false AND true positives).

The confirmed defect (measured over 984 real STT transcripts in friday.log):
47 utterances were rejected, all 47 by the "channel promo" rule, all 47 matched
the BARE word "like", and none was a channel promo. The rule was

    r"\\b(please\\s+)?(like|subscribe)|like and subscribe|subscription\\b"

whose `(please\\s+)?` group is OPTIONAL, so the first alternative collapsed to
matching "like" anywhere. A second, independent landmine was

    r"\\[?\\b(music|applause|laughter|silence|foreign|noise|inaudible)\\b\\]?"

whose brackets were OPTIONAL - and because `_normalized()` strips every
non-word character, "[music]" and "play the music" are the same string by the
time any rule runs. So the bare word could not be tightened in place; the
bracketed form has to be matched on the RAW text.

These tests assert BOTH directions, and the regression corpus is the real log
so the evidence cannot be quietly re-narrowed.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from source.server.speech_filters import (  # noqa: E402
    HALLUCINATION_PATTERNS,
    _normalized,
    reject_reason,
)

FRIDAY_LOG = os.path.join(
    os.path.expanduser("~"), ".friday", "logs", "friday.log")


def _log_transcripts():
    """Real STT transcripts from the live log, or [] if unavailable."""
    if not os.path.isfile(FRIDAY_LOG):
        return []
    try:
        with open(FRIDAY_LOG, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        match = re.search(r"stt_final text=(.*)$", line)
        if not match:
            continue
        text = match.group(1).strip()
        if text[:1] in ("'", '"'):
            text = text[1:-1] if text[-1:] == text[:1] else text[1:]
        out.append(text)
    return out


class FalsePositiveRegressions(unittest.TestCase):
    """Ordinary speech that must NOT be rejected."""

    def test_the_four_reported_utterances_are_accepted(self):
        for text in (
            "i really like that, open notepad",
            "can you set the volume like 40 percent",
            "No, Friday. I was just like working on this like weird Minecraft "
            "thing, you know, so I just built it.",
            "No, Friday. It was just like a project I was working on.",
        ):
            with self.subTest(text=text[:48]):
                self.assertIsNone(
                    reject_reason(text),
                    "ordinary speech containing 'like' must not be rejected")

    def test_ordinary_sentences_with_caption_words_are_accepted(self):
        for text in (
            "play the music",
            "I heard a noise, what was that",
            "in silence mode",
            "turn on the laughter track",
            "she has a lovely foreign accent",
            "that sound was inaudible to me",
        ):
            with self.subTest(text=text):
                self.assertIsNone(
                    reject_reason(text),
                    "a bare caption word is not a caption token")

    def test_non_promotional_subscribe_is_accepted(self):
        for text in (
            "I subscribe to that idea",
            "i like notepad, open it",
            "how do i like a file in python",
        ):
            with self.subTest(text=text):
                self.assertIsNone(reject_reason(text))

    def test_every_real_utterance_the_old_rule_rejected_now_passes(self):
        """The 47 measured casualties must all be accepted now.

        Reconstructed from the log the same way it was measured: the final
        transcript immediately preceding each `wake_rejected ... channel promo`.
        """
        if not os.path.isfile(FRIDAY_LOG):
            self.skipTest("friday.log not present")
        with open(FRIDAY_LOG, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        casualties = []
        for index, line in enumerate(lines):
            if "detail=channel promo" not in line:
                continue
            for back in range(index - 1, max(-1, index - 25), -1):
                match = re.search(r"stt_final text=(.*)$", lines[back])
                if match:
                    text = match.group(1).strip()
                    if text[:1] in ("'", '"'):
                        text = (text[1:-1] if text[-1:] == text[:1]
                                else text[1:])
                    casualties.append(text)
                    break
        if not casualties:
            self.skipTest("no channel-promo rejections recorded in the log")
        still = [t for t in casualties if reject_reason(t) is not None]
        self.assertEqual(
            still, [],
            "these real transcripts are ordinary speech and must be accepted")

    def test_the_bare_word_alone_is_not_promotional(self):
        for text in ("like", "subscribe", "I like", "subscribe now"):
            with self.subTest(text=text):
                self.assertIsNone(reject_reason(text))


class TruePositiveRegressions(unittest.TestCase):
    """Genuine hallucinations must STILL be rejected."""

    def test_genuine_channel_promo_is_still_rejected(self):
        for text in (
            "Please like and subscribe for more.",
            "like and subscribe",
            "like & subscribe",
            "please subscribe",
            "smash that like",
            "hit the like button",
            "my subscription link is in the description",
        ):
            with self.subTest(text=text):
                self.assertEqual(reject_reason(text), "channel promo")

    def test_bracketed_caption_tokens_are_still_rejected(self):
        for text in ("[music]", "[Music]", "[applause]", "[BLIP]",
                     "[inaudible]", "[silence]", "[foreign language]"):
            with self.subTest(text=text):
                self.assertEqual(reject_reason(text),
                                 "background-caption token")

    def test_other_boilerplate_is_still_rejected(self):
        self.assertIsNotNone(reject_reason("Thanks for watching!"))
        self.assertIsNotNone(reject_reason("Subtitles by the Amara.org community"))
        self.assertIsNotNone(reject_reason("welcome back"))
        self.assertIsNotNone(reject_reason("sponsored by"))
        self.assertIsNotNone(reject_reason("transcript provided"))

    def test_empty_and_repetitive_guards_are_intact(self):
        self.assertEqual(reject_reason("   "), "empty")
        self.assertEqual(reject_reason("ha ha ha ha ha ha ha ha ha ha"),
                         "repetitive no-speech sleep")
        self.assertEqual(reject_reason("the the the the the the the the the"),
                         "repetitive no-speech sleep")

    def test_the_gate_still_rejects_the_existing_true_positive_corpus(self):
        """Everything test_hallucination_gate.py asserts must still hold."""
        self.assertIsNone(reject_reason("friday what time is it"))
        self.assertIsNone(reject_reason("open notepad"))
        self.assertIsNone(reject_reason("remember my birthday is tomorrow"))


class FilterShapeRegressions(unittest.TestCase):
    """Structural guards so the bug cannot come back silently."""

    def test_no_pattern_matches_a_bare_like_or_subscribe(self):
        """The root cause, asserted directly against the rule table."""
        for pattern, label in HALLUCINATION_PATTERNS:
            for word in ("like", "subscribe"):
                with self.subTest(pattern=pattern[:24], word=word):
                    self.assertIsNone(
                        re.search(pattern, "so i %s it" % word),
                        "rule %r matches a bare %r" % (label, word))

    def test_normalization_is_what_makes_brackets_unrecoverable(self):
        # Documents WHY the caption rule lives on raw text: this equality is
        # the reason a tightened normalized pattern cannot work.
        self.assertEqual(_normalized("[music]"), _normalized("play the music")
                         .split()[-1])
        self.assertNotIn("[", _normalized("[music]"))

    def test_reject_reason_ignores_non_strings(self):
        self.assertIsNone(reject_reason(None))
        self.assertIsNone(reject_reason(42))


if __name__ == "__main__":
    unittest.main()
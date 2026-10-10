import unittest

from baby_video.captions import (CAPTION_MAX_CHARS, match_timestamps, normalize,
                                 segment_script)


def token(text, start, end, p=.9):
    return {"tokens": [{"text": text, "offsets": {"from": start, "to": end}, "p": p}]}


class CaptionTests(unittest.TestCase):
    def test_exact_text_uses_audio_times_and_keeps_punctuation(self):
        result = match_timestamps("おはよう。元気です。", [token("おはよう", 200, 1200),
                                 token("元気", 1800, 2400), token("です", 2400, 3000)], 4)
        self.assertEqual(result["status"], "aligned")
        self.assertEqual(result["proposed_captions"], [{"text": "おはよう。", "from": .2, "to": 1.2},
                                                       {"text": "元気です。", "from": 1.8, "to": 3}])
        self.assertFalse(result["perceptual_sync_verified"])

    def test_hallucination_or_missing_words_never_approved(self):
        result = match_timestamps("おはよう。", [token("こんにちは", 100, 1000)], 2)
        self.assertEqual(result["status"], "needs_review")
        self.assertFalse(result["exact_normalized_text_match"])

    def test_zero_time_low_confidence_and_overlap_need_review(self):
        cases = [[token("あ", 100, 100)], [token("あ", 100, 800, .2)],
                 [token("あ", 100, 900), token("い", 800, 1500)]]
        for segments in cases:
            with self.subTest(segments=segments):
                text = "あい" if len(segments) == 2 else "あ"
                self.assertEqual(match_timestamps(text, segments, 2)["status"], "needs_review")

    def test_special_tokens_not_part_of_spoken_script(self):
        result = match_timestamps("こんにちは。", [token("[_BEG_]", 0, 0), token("こんにちは", 100, 1500)], 2)
        self.assertEqual(result["status"], "aligned")

    def test_valid_segment_span_usable_when_final_token_offset_is_zero(self):
        segment = {"text": "おはよう", "offsets": {"from": 100, "to": 1500},
                   "tokens": [{"text": "おはよう", "offsets": {"from": 100, "to": 100}, "p": .95},
                              {"text": "[_EOT_]", "offsets": {"from": 1500, "to": 1500}, "p": .01}]}
        result = match_timestamps("おはよう。", [segment], 2)
        self.assertEqual(result["status"], "aligned")
        self.assertEqual(result["proposed_captions"][0]["to"], 1.5)

    def test_dtw_acoustic_endpoint_fixes_model_overshoot_without_scaling(self):
        s = token("おはよう", 100, 6500)
        s["tokens"][0]["t_dtw"] = 140
        result = match_timestamps("おはよう。", [s], 2, activity=[{"from": .1, "to": 1.6}], require_dtw=True)
        self.assertEqual(result["status"], "aligned")
        self.assertEqual(result["proposed_captions"], [{"text": "おはよう。", "from": .1, "to": 1.6}])
        self.assertFalse(result["proportional_time_scaling"])

    def test_dtw_out_of_range_and_nonmonotonic_stop(self):
        for times in ((10, 220), (120, 100), (-1, 100)):
            a, b = token("あ", 100, 900), token("い", 1000, 1600)
            a["tokens"][0]["t_dtw"], b["tokens"][0]["t_dtw"] = times
            result = match_timestamps("あい。", [a, b], 2, activity=[{"from": 0, "to": 1.8}], require_dtw=True)
            self.assertEqual(result["status"], "needs_review")

    def test_missing_spoken_word_and_long_silence_stop(self):
        incomplete = match_timestamps("あいう。", [token("あい", 100, 1000)], 3)
        self.assertIn("caption_text_coverage_incomplete", incomplete["problems"])
        result = match_timestamps("あい。", [token("あ", 100, 800), token("い", 2000, 2500)], 3,
                                  activity=[{"from": .1, "to": .8}, {"from": 2, "to": 2.5}])
        self.assertIn("caption_spans_long_silence", result["problems"])

    def test_caption_boundary_inside_one_token_does_not_drop_text(self):
        result = match_timestamps("あ。い。", [token("あい", 100, 1500)], 2)
        self.assertIn("caption_boundary_requires_review", result["problems"])
        self.assertEqual("".join(c["text"] for c in result["proposed_captions"]), "あ。い。")


SCRIPT = ("お願い、ティッシュ出さないで！って思うよね。"
          "でも僕には、「引っ張ったらまた出てくる」が超おもしろいの。大発見中です。")


class SegmentScriptTests(unittest.TestCase):
    def test_script_becomes_short_single_phrase_captions(self):
        self.assertEqual(segment_script(SCRIPT),
                         ["お願い、ティッシュ", "出さないで！", "って思うよね。", "でも僕には、",
                          "「引っ張ったらまた", "出てくる」", "が超おもしろいの。", "大発見中です。"])

    def test_no_caption_exceeds_the_character_limit(self):
        for caption in segment_script(SCRIPT):
            with self.subTest(caption=caption):
                self.assertLessEqual(len(normalize(caption)), CAPTION_MAX_CHARS)

    def test_every_character_is_kept_exactly_once(self):
        for text in (SCRIPT, "おはよう。元気です。", "あ。い。", "ああ、いい、うう、ええ、おお。",
                     "ステップ1寝転ぶステップ2腕を上げるステップ3腕を右に倒す。", "、。！", "ん"):
            with self.subTest(text=text):
                self.assertEqual("".join(segment_script(text)), text)

    def test_short_pieces_merge_only_within_a_sentence(self):
        self.assertEqual(segment_script("あ。い。"), ["あ。", "い。"])
        self.assertEqual(segment_script("あ、い。"), ["あ、い。"])

    def test_long_run_without_punctuation_splits_at_word_starts(self):
        self.assertEqual(segment_script("ティッシュ出さないで"), ["ティッシュ", "出さないで"])

    def test_limit_is_configurable(self):
        tight = segment_script(SCRIPT, max_chars=5)
        self.assertGreater(len(tight), len(segment_script(SCRIPT)))
        self.assertEqual("".join(tight), SCRIPT)

    def test_kana_run_without_a_safe_break_stays_whole_instead_of_splitting_a_word(self):
        self.assertEqual(segment_script("きょうはね、いっぱいわらったんだ。"),
                         ["きょうはね、", "いっぱいわらったんだ。"])
        self.assertEqual(segment_script("ママがこっちを見てくれるからなんだ。"),
                         ["ママがこっちを", "見てくれるからなんだ。"])

    def test_a_cut_never_leaves_a_one_character_caption(self):
        for caption in segment_script("ぼくがリモコンを隠すのは、いじわるじゃないんだよ。"):
            with self.subTest(caption=caption):
                self.assertGreaterEqual(len(normalize(caption)), 3)


class FineCaptionTimingTests(unittest.TestCase):
    def test_each_short_caption_takes_the_times_of_its_own_characters(self):
        result = match_timestamps("ティッシュ出さないで！",
                                  [token("ティッシュ", 100, 900), token("出さないで", 900, 1800)], 3)
        self.assertEqual(result["status"], "aligned")
        self.assertEqual(result["proposed_captions"],
                         [{"text": "ティッシュ", "from": .1, "to": .9},
                          {"text": "出さないで！", "from": .9, "to": 1.8}])


if __name__ == "__main__":
    unittest.main()

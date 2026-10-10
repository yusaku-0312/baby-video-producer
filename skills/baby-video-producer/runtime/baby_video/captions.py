"""Local speech-derived subtitle timestamps; uncertain recognition stops rendering."""
from __future__ import annotations

import json
import array
import math
import shutil
import subprocess
import tempfile
import unicodedata
import wave
from pathlib import Path

from .media import probe
from .state import PipelineError, digest_file


def normalize(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text)
                   if unicodedata.category(c)[0] in {"L", "N"}).casefold()


CAPTION_MAX_CHARS = 9
CAPTION_TARGET_CHARS = 7
CAPTION_MIN_CHARS = 3
SENTENCE_END = "。！？!?\n"
SOFT_BREAK = "、，,；;：:"
OPEN_BRACKET = "「『（(“"
CLOSE_BRACKET = "」』）)”"


def _letters(text: str) -> int:
    return len(normalize(text))


def _script(char: str) -> str | None:
    code = ord(char)
    if 0x3040 <= code <= 0x309F:
        return "hiragana"
    if 0x30A0 <= code <= 0x30FF:
        return "katakana"
    if 0x3400 <= code <= 0x9FFF or char == "々":
        return "kanji"
    return "latin" if char.isalnum() else None


def _punctuation_spans(text: str) -> list[tuple[int, int, bool]]:
    """Cut at punctuation, keeping it with the phrase it closes. True marks a sentence end."""
    spans: list[tuple[int, int, bool]] = []
    start = 0
    for index, char in enumerate(text):
        if char in SENTENCE_END:
            spans.append((start, index + 1, True))
            start = index + 1
        elif char in SOFT_BREAK or char in CLOSE_BRACKET:
            spans.append((start, index + 1, False))
            start = index + 1
        elif char in OPEN_BRACKET and index > start:
            spans.append((start, index, False))
            start = index
    if start < len(text):
        spans.append((start, len(text), True))
    return spans


def _absorb_punctuation_only(text, spans):
    """Keep stray punctuation attached to a phrase so no character is dropped."""
    kept: list[tuple[int, int, bool]] = []
    for start, end, sentence_end in spans:
        if kept and not _letters(text[kept[-1][0]:kept[-1][1]]):
            kept[-1] = (kept[-1][0], end, sentence_end)
        elif kept and not _letters(text[start:end]):
            kept[-1] = (kept[-1][0], end, kept[-1][2] or sentence_end)
        else:
            kept.append((start, end, sentence_end))
    return kept


def _best_cut(text, start, end, max_chars):
    """Pick where to break an over-long phrase, or None when no break looks safe.

    Only script changes are offered, because Japanese has no spaces to break on.
    Cutting a run of kana at an arbitrary character would split a word, so a phrase
    with no such boundary is left long and the renderer shrinks it to fit instead.
    """
    total = _letters(text[start:end])
    parts = max(2, -(-total // CAPTION_TARGET_CHARS))
    want = round(total / parts)
    limit = end
    while limit > start + 1 and _letters(text[start:limit]) > max_chars:
        limit -= 1
    scored = []
    for index in range(start + 1, limit + 1):
        before, after = _script(text[index - 1]), _script(text[index])
        if before is None or after is None or before == after:
            continue
        left, right = _letters(text[start:index]), _letters(text[index:end])
        if left < CAPTION_MIN_CHARS or right < CAPTION_MIN_CHARS:
            continue
        # Kana running into kanji, katakana or latin is where a word usually starts;
        # kanji running into kana is usually an inflection inside one word.
        word_start = before != "kanji" and after != "hiragana"
        scored.append((abs(left - want), 0 if word_start else 1, -index, index))
    return min(scored)[3] if scored else None


def _cut_to_limit(text, start, end, max_chars):
    pieces, cut = [], start
    while _letters(text[cut:end]) > max_chars:
        index = _best_cut(text, cut, end, max_chars)
        if index is None:
            break
        pieces.append((cut, index))
        cut = index
    pieces.append((cut, end))
    return pieces


def segment_script(text: str, *, max_chars: int = CAPTION_MAX_CHARS) -> list[str]:
    """Cut a script into one short phrase per caption, keeping every character in order.

    Captions switch on reading units, not sentences: punctuation and brackets break the
    text, over-long phrases split at a likely word start, and neighbouring fragments
    merge back together while they stay inside one sentence and under the limit.
    """
    pieces: list[tuple[int, int, bool]] = []
    for start, end, sentence_end in _absorb_punctuation_only(text, _punctuation_spans(text)):
        cuts = _cut_to_limit(text, start, end, max_chars)
        pieces += [(piece_start, piece_end, sentence_end and index == len(cuts) - 1)
                   for index, (piece_start, piece_end) in enumerate(cuts)]
    captions: list[tuple[int, int, bool]] = []
    for start, end, sentence_end in pieces:
        if captions and not captions[-1][2] and _letters(text[captions[-1][0]:end]) <= max_chars:
            captions[-1] = (captions[-1][0], end, sentence_end)
        else:
            captions.append((start, end, sentence_end))
    return [text[start:end] for start, end, _ in captions]


def audio_activity(wav: Path) -> list[dict]:
    """Measure 20ms RMS activity in mono PCM; this is not a semantic VAD model."""
    with wave.open(str(wav)) as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (1, 2, 16000):
            raise PipelineError("Alignment WAV must be mono 16kHz signed 16-bit PCM")
        samples = array.array("h", stream.readframes(stream.getnframes()))
    hop = 320
    rms = [math.sqrt(sum(x*x for x in samples[i:i+hop]) / len(samples[i:i+hop])) / 32768
           for i in range(0, len(samples), hop)]
    threshold = max(max(rms, default=0) * .015, .0005)
    intervals = []
    for index, level in enumerate(rms):
        if level < threshold:
            continue
        start, end = index * .02, min((index + 1) * .02, len(samples) / 16000)
        if intervals and start - intervals[-1]["to"] <= .20:
            intervals[-1]["to"] = end
        else:
            intervals.append({"from": start, "to": end})
    return intervals


def match_timestamps(text: str, transcription: list, duration: float, *, activity=None, require_dtw=False) -> dict:
    """Match exact normalized text to ASR segment times without fabricated times.

    Confidence is an uncalibrated ASR heuristic, not proof of perceptual sync.
    Production uses DTW attention boundaries and measured acoustic activity.
    Legacy segment-only matching is retained for explicit timestamp inputs.
    Neither path divides time by character count or rescales model endpoints.
    """
    tokens = []
    problems = []
    anchors = []
    for segment in transcription:
        lexical = [t for t in segment.get("tokens", [])
                   if not t.get("text", "").startswith(("[_", "<|")) and normalize(t.get("text", ""))]
        if "offsets" in segment and normalize(segment.get("text", "")):
            pieces = [{"text": segment["text"], "offsets": segment["offsets"],
                       "p": min((t.get("p", 0) for t in lexical), default=0),
                       "t_dtw": min((t.get("t_dtw", -1) for t in lexical), default=-1)}]
        else:
            pieces = lexical
        for token in pieces:
            raw = token.get("text", "")
            if raw.startswith("[_") or raw.startswith("<|"):
                continue
            word = normalize(raw)
            if not word:
                continue
            offsets = token.get("offsets", {})
            try:
                start, end = float(offsets["from"]) / 1000, float(offsets["to"]) / 1000
                confidence = float(token["p"])
            except (KeyError, TypeError, ValueError):
                problems.append("missing_token_time_or_confidence")
                continue
            dtw = float(token.get("t_dtw", -1)) / 100
            region = None
            if require_dtw:
                previous_anchor = anchors[-1] if anchors else None
                if not math.isfinite(dtw) or not 0 <= dtw < duration:
                    problems.append("dtw_timestamp_missing_or_out_of_range")
                elif anchors and dtw < anchors[-1]:
                    problems.append("dtw_timestamps_not_monotonic")
                anchors.append(dtw)
                nearby = [r for r in (activity or []) if r["from"] - .12 <= dtw <= r["to"] + .12]
                if nearby:
                    region = min(nearby, key=lambda r: max(r["from"]-dtw, dtw-r["to"], 0))
                    # DTW points are attention-alignment boundaries. Use them
                    # in order, with acoustic onset/offset at utterance ends.
                    # Legacy model endpoints can extend into padded context.
                    start = max(region["from"], previous_anchor if previous_anchor is not None else region["from"])
                    end = min(region["to"], dtw)
                    if tokens and tokens[-1].get("dtw_seconds") == dtw:
                        tokens[-1]["text"] += word
                        tokens[-1]["p"] = min(tokens[-1]["p"], confidence)
                        continue
                else:
                    problems.append("dtw_anchor_not_supported_by_audio_activity")
            if not all(math.isfinite(x) for x in (start, end, confidence)) or not 0 <= start < end <= duration + .05:
                problems.append("invalid_or_zero_token_time")
            if tokens and start < tokens[-1]["to"] - .02:
                problems.append("overlapping_token_times")
            if not 0.5 <= confidence <= 1:
                problems.append("low_token_confidence")
            tokens.append({"text": word, "from": start, "to": end, "p": confidence,
                           **({"dtw_seconds": dtw, "activity_region": region} if require_dtw else {})})
    if require_dtw and tokens and tokens[-1].get("activity_region"):
        tokens[-1]["to"] = tokens[-1]["activity_region"]["to"]
    recognized = "".join(t["text"] for t in tokens)
    expected = normalize(text)
    exact = bool(expected) and recognized == expected
    if not exact:
        problems.append("recognized_text_differs_from_script")
    captions = []
    if exact:
        char_tokens = [token for token in tokens for _ in token["text"]]
        offset = 0
        for clause in segment_script(text):
            count = len(normalize(clause))
            if not count:
                continue
            matched = char_tokens[offset:offset + count]
            start, end = matched[0]["from"], matched[-1]["to"]
            if require_dtw and matched[-1].get("activity_region"):
                following = char_tokens[offset + count:offset + count + 1]
                if not following or following[0].get("activity_region") != matched[-1]["activity_region"]:
                    end = matched[-1]["activity_region"]["to"]
            if end <= start or (captions and start < captions[-1]["to"]):
                problems.append("caption_boundary_requires_review")
            captions.append({"text": clause.strip(), "from": round(start, 3), "to": round(end, 3)})
            offset += count
    if activity is not None:
        if not activity:
            problems.append("no_audio_activity")
        for previous, following in zip(activity, activity[1:]):
            if following["from"] - previous["to"] >= .6 and any(
                c["from"] < previous["to"] and c["to"] > following["from"] for c in captions):
                problems.append("caption_spans_long_silence")
    if normalize("".join(c["text"] for c in captions)) != expected:
        problems.append("caption_text_coverage_incomplete")
    return {"status": "needs_review" if problems else "aligned",
            "method": "whisper_cpp_dtw_and_measured_audio_activity" if require_dtw else "whisper_cpp_segment_timestamps_exact_script_match",
            "exact_normalized_text_match": exact,
            "recognized_normalized_text": recognized,
            "minimum_token_confidence": min((t["p"] for t in tokens), default=0),
            "problems": sorted(set(problems)), "proposed_captions": captions,
            "tokens": tokens, "duration_seconds": duration,
            "audio_activity": activity, "proportional_time_scaling": False,
            "perceptual_sync_verified": False,
            "note": "ASR times and confidence are estimates; inspect real speech and subtitle switches for final QA."}


def align_captions(audio: Path, text: str, model: Path, output: Path, *, whisper_cli="whisper-cli", dtw_model="small") -> dict:
    output = output.resolve()
    if output.exists():
        raise PipelineError("Captions already exist; review or use a new version before replacing them")
    output.parent.mkdir(parents=True, exist_ok=True)
    report_file = output.parent / "caption-alignment.json"
    if not model.is_file() or not shutil.which(whisper_cli):
        report = {"status": "needs_review", "problems": ["local_whisper_binary_or_model_missing"],
                  "perceptual_sync_verified": False, "live_api_called": False}
        report_file.write_text(json.dumps(report, indent=2))
        return report
    duration = probe(audio)["duration_seconds"]
    if not 0 < duration <= 10:
        raise PipelineError("Speech alignment expects positive audio no longer than ten seconds")
    with tempfile.TemporaryDirectory(prefix=".alignment-", dir=output.parent) as tmp:
        wav = Path(tmp) / "voice.wav"
        prefix = Path(tmp) / "speech"
        try:
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(audio),
                            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                           capture_output=True, check=True, timeout=30)
            subprocess.run([whisper_cli, "-ng", "-m", str(model), "-f", str(wav), "-l", "ja",
                            "-nfa", "-dtw", dtw_model, "-bs", "1", "-ojf", "-ml", "1", "-of", str(prefix), "-np"],
                           capture_output=True, check=True, timeout=180)
            transcription = json.loads(prefix.with_suffix(".json").read_text())["transcription"]
            activity = audio_activity(wav)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            report = {"status": "needs_review", "problems": ["local_speech_recognition_failed"],
                      "perceptual_sync_verified": False, "live_api_called": False}
            report_file.write_text(json.dumps(report, indent=2))
            return report
    report = match_timestamps(text, transcription, duration, activity=activity, require_dtw=True)
    report.update({"audio_sha256": digest_file(audio), "model_name": model.name,
                   "dtw_preset": dtw_model, "flash_attention": False, "beam_size": 1,
                   "pcm_sample_rate": 16000, "json_offset_unit": "milliseconds", "dtw_unit": "centiseconds",
                   "live_api_called": False})
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] == "aligned":
        output.write_text(json.dumps(report["proposed_captions"], ensure_ascii=False, indent=2))
    return report

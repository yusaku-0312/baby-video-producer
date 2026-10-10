import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baby_video.pipeline import build_spec, load_job, resume, import_image, revise_job, validate_job
from baby_video.offline import fixture_decision
from baby_video.providers import H3_MODEL, KLING_MODEL
from baby_video.state import Run, PipelineError


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job_file = self.root / "job.json"
        for name in ("source.md", "image-prompt.txt", "video-prompt.txt", "bgm.mp3"):
            (self.root / name).write_bytes(b"MOCK ONLY")
        (self.root / "dialogue.json").write_text(json.dumps({"candidates": [{"text": "MOCK"} for _ in range(10)]}))
        self.job = {"procedure_confirmed": True, "article_url": "https://example.invalid/mock",
            "article_file": "source.md", "dialogue_candidates_file": "dialogue.json", "selected_candidate": 0,
            "selection_mode": "agent", "selection_reason": "MOCK explicit agent choice",
            "source_facts_reviewed": True, "reference_pixels_reviewed": True, "voice_id": "mock",
            "voice_confirmed": True, "voice_license_confirmed": True, "video_duration_seconds": 9,
            "video_resolution": "480P", "lipsync_model": KLING_MODEL, "image_prompt_file": "image-prompt.txt",
            "video_prompt_file": "video-prompt.txt", "budget_usd": "1",
            "cost_caps_usd": {"audio": ".01", "video": ".3", "lipsync": ".15"}, "bgm_file": "bgm.mp3"}
        self.job_file.write_text(json.dumps(self.job))

    def test_short_speech_blocks_downstream_even_after_cached_audio_approval(self):
        voice = self.root / 'short-padded.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=440:sample_rate=16000:duration=5.3',
                        '-af', 'apad=whole_dur=9', str(voice)], check=True, capture_output=True)

        class Services:
            calls = 0

            def tts(service, _, __, destination, **___):
                service.calls += 1
                shutil.copyfile(voice, destination)

        services = Services()
        directory = self.root / 'run'
        for stage in ('source', 'dialogue'):
            result = resume(self.job_file, directory, mode='mock', services=services)
            self.assertEqual(result['stage'], stage)
            fixture_decision(Run(directory), stage)
        result = resume(self.job_file, directory, mode='mock', services=services)
        self.assertEqual(result['status'], 'waiting')
        self.assertIn('8.5', result['message'])
        fixture_decision(Run(directory), 'audio')
        result = resume(self.job_file, directory, mode='mock', services=services)
        self.assertEqual(result['status'], 'waiting')
        self.assertIn('8.5', result['message'])
        self.assertEqual(services.calls, 1)
        self.assertFalse((directory / 'inputs/image-gen-request.json').exists())
        report = json.loads((directory / 'inputs/speech-duration.json').read_text())
        self.assertFalse(report['passed'])
        self.assertLess(report['speech_span_seconds'], 8.5)

    def test_authorized_fictional_face_does_not_claim_reference_review(self):
        from baby_video.pipeline import validate_job
        job = {**self.job, "reference_pixels_reviewed": False, "reference_files": [],
               "image_reference_mode": "original_fictional", "new_fictional_face_authorized": True}
        message = "Reference image pixels need review before image generation"
        self.assertNotIn(message, validate_job(job, self.job_file))
        self.assertFalse(job["reference_pixels_reviewed"])
        self.assertIn(message, validate_job({**job, "new_fictional_face_authorized": False}, self.job_file))
        self.assertIn(message, validate_job({**job, "reference_files": ["image-prompt.txt"]}, self.job_file))

    def test_timing_revision_regenerates_audio_and_measures_before_downstream(self):
        voices = []
        for index, seconds in enumerate((5.3, 8.7)):
            voice = self.root / f'timing-fixture-{index}.wav'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            f'sine=frequency=440:sample_rate=16000:duration={seconds}', str(voice)],
                           check=True, capture_output=True)
            voices.append(voice)

        class Services:
            calls = 0

            def tts(service, _, __, destination, **___):
                shutil.copyfile(voices[service.calls], destination)
                service.calls += 1

        self.job.update(review_policy='script_only', timing_revision_authorized=True, fish_speed=1.0)
        self.job_file.write_text(json.dumps(self.job))
        services = Services()
        directory = self.root / 'timing-run'
        for stage, actor in (('source', 'codex_agent'), ('dialogue', 'human')):
            result = resume(self.job_file, directory, mode='mock', services=services)
            self.assertEqual(result['stage'], stage)
            Run(directory).decide(stage, 'next', {
                'summary': 'Read isolated source or approved initial fixture script', 'checks': ['text_read'],
                'evidence_files': [result['artifact']], 'unverified': [],
            }, actor=actor)
        result = resume(self.job_file, directory, mode='mock', services=services)
        self.assertEqual(result['status'], 'waiting')
        report = json.loads((directory / 'inputs/speech-duration.json').read_text())
        self.assertFalse(report['passed'])
        self.assertFalse((directory / 'inputs/image-gen-request.json').exists())
        candidates = json.loads((self.root / 'dialogue.json').read_text())
        candidates['candidates'][0]['text'] = 'MOCK same source, longer natural wording'
        (self.root / 'dialogue.json').write_text(json.dumps(candidates))
        self.job['fish_speed'] = 1.25
        self.job_file.write_text(json.dumps(self.job))
        change = revise_job(self.job_file, directory, '尺調整: 発話5.3秒のため同じ意味で長さを調整')
        self.assertEqual(change['invalidated'], ['dialogue', 'audio', 'lipsync', 'edit', 'description'])
        archived = directory / 'history/audio-inputs/revision-0'
        old_request = json.loads((archived / 'fish-tts-request.json').read_text())
        old_duration = json.loads((archived / 'speech-duration.json').read_text())
        self.assertEqual(old_request['body']['prosody']['speed'], 1.0)
        self.assertEqual(old_request['audio_sha256'], old_duration['audio_sha256'])
        result = resume(self.job_file, directory, mode='mock', services=services)
        self.assertEqual(result['stage'], 'dialogue')
        Run(directory).decide('dialogue', 'next', {
            'summary': 'Read timing-only wording revision preserving the source and meaning',
            'checks': ['text_read'], 'evidence_files': [result['artifact']], 'unverified': [],
        }, actor='codex_agent')
        result = resume(self.job_file, directory, mode='mock', services=services)
        self.assertEqual(result['stage'], 'audio')
        report = json.loads((directory / 'inputs/speech-duration.json').read_text())
        self.assertTrue(report['passed'])
        self.assertEqual(report['fish_speed'], 1.25)
        self.assertGreaterEqual(report['speech_span_seconds'], 8.5)
        self.assertEqual(services.calls, 2)
        stages = {s['id']: s for s in Run(directory).status()['stages']}
        self.assertEqual(stages['source']['revision'], 0)
        self.assertEqual(stages['dialogue']['revision'], 1)

    def test_timing_authorization_can_be_recorded_without_invalidating_media(self):
        directory = self.root / 'run'
        resume(self.job_file, directory, mode='mock', services=object())
        self.job['timing_revision_authorized'] = True
        self.job_file.write_text(json.dumps(self.job))
        result = revise_job(self.job_file, directory, 'User authorizes timing-only revisions')
        self.assertEqual(result['invalidated'], [])

    def test_timing_authorization_must_be_boolean(self):
        self.job['timing_revision_authorized'] = 'yes'
        self.job_file.write_text(json.dumps(self.job))
        _, _, errors = load_job(self.job_file)
        self.assertTrue(any('explicit boolean' in message for message in errors))

    def test_live_missing_approval_never_constructs_provider(self):
        with patch("baby_video.pipeline.Fal") as provider:
            result = resume(self.job_file, self.root / "run")
        self.assertEqual(result["status"], "blocked")
        provider.assert_not_called()
        self.assertFalse((self.root / "run/state.sqlite3").exists())

    def test_job_cannot_contain_credentials(self):
        self.job["api_key"] = "never-log-this"
        self.job_file.write_text(json.dumps(self.job))
        with self.assertRaises(PipelineError) as error:
            load_job(self.job_file)
        self.assertNotIn("never-log", str(error.exception))

    def test_agent_choice_uses_observation_not_numeric_score(self):
        candidates = [{"text": f"MOCK{i}", "score": 70, "estimated_seconds": 8,
                       "selection_reason": "MOCK selection rationale"} for i in range(10)]
        candidates[2]["score"] = 95
        candidates[4].update(score=95, estimated_seconds=7)
        (self.root / "dialogue.json").write_text(json.dumps({"candidates": candidates}))
        self.job.update(selection_mode="agent", selected_candidate=1, selection_reason="MOCK: natural sentence fits the evidence")
        self.job_file.write_text(json.dumps(self.job))
        _, selected, errors = load_job(self.job_file)
        self.assertEqual(errors, [])
        self.assertEqual(selected["selected_candidate"], 1)
        self.assertEqual(selected["selection_mode"], "agent")

    def test_manual_selection_ignores_ranking(self):
        self.job.update(selection_mode="manual", selected_candidate=6)
        self.job_file.write_text(json.dumps(self.job))
        _, selected, errors = load_job(self.job_file)
        self.assertEqual(errors, [])
        self.assertEqual(selected["selected_candidate"], 6)

    def test_automatic_numeric_selection_is_retired(self):
        self.job.update(selection_mode="auto", selected_candidate=None)
        self.job_file.write_text(json.dumps(self.job))
        with patch("baby_video.pipeline.Fal") as provider:
            result = resume(self.job_file, self.root / "run")
        self.assertEqual(result["status"], "blocked")
        provider.assert_not_called()

    def test_actual_overshoot_blocks_kling_without_retrimming(self):
        events = []
        class FakeFal:
            def __init__(self, model):
                self.model = model
            def upload(self, *_):
                events.append((self.model, "upload"))
                return "mock://image"
            def submit(self, *_):
                events.append((self.model, "submit"))
                return "mock-h3-job"
            def status(self, *_):
                return {"status": "COMPLETED", "failed": False}
            def download_result(self, _, destination):
                destination.write_bytes(b"MOCK OVERSHOOT")
        class Services:
            def tts(self, _, __, destination, **___):
                destination.write_bytes(b"MOCK AUDIO")
            def fal(self, model):
                return FakeFal(model)
        run = self.root / "run"
        assets = run / "artifacts"
        assets.mkdir(parents=True)
        image = assets / "image.png"
        image.write_bytes(b"MOCK IMAGE")
        from baby_video.state import digest_file
        (assets / "image-provenance.json").write_text(json.dumps({"generated_by": "offline_mock", "file": "image.png",
            "sha256": digest_file(image), "prompt_sha256": digest_file(self.root / "image-prompt.txt")}))
        def probe(path):
            return {"duration_seconds": 10.7 if "h3-raw" in str(path) else 7.8}
        with patch("baby_video.pipeline.probe", side_effect=probe), patch("baby_video.pipeline.image_dimensions"), \
                patch("baby_video.pipeline.check_speech_duration", return_value={"passed": True}):
            for _ in range(10):
                first = resume(self.job_file, run, mode="mock", services=Services())
                if first["status"] == "review_required":
                    fixture_decision(Run(run), first["stage"])
                else:
                    break
            second = resume(self.job_file, run, mode="mock", services=Services())
            self.assertEqual(second["status"], "review_required")
            # Even an explicit fixture acceptance cannot circumvent length checks.
            fixture_decision(Run(run), "video")
            third = resume(self.job_file, run, mode="mock", services=Services())
        self.assertIn("exceeds", first["message"])
        self.assertIn("exceeds", third["message"])
        self.assertEqual(events.count((H3_MODEL, "submit")), 1)
        self.assertFalse(any(model == KLING_MODEL for model, _ in events))
        self.assertFalse((assets / "portrait.mp4").exists())

    def test_resume_returns_to_agent_and_waits_without_new_attempt(self):
        services = object()
        run = self.root / "run"
        first = resume(self.job_file, run, mode="mock", services=services)
        second = resume(self.job_file, run, mode="mock", services=services)
        self.assertEqual((first["status"], first["stage"]), ("review_required", "source"))
        self.assertEqual(second["stage"], "source")
        self.assertEqual(len(Run(run).status()["attempts"]), 1)
        fixture_decision(Run(run), "source")
        self.assertEqual(resume(self.job_file, run, mode="mock", services=services)["stage"], "dialogue")

    def test_changed_voice_revision_retains_image_lineage_and_budget(self):
        run = self.root / "run"
        resume(self.job_file, run, mode="mock", services=object())
        self.job["voice_id"] = "different-mock-voice"
        self.job_file.write_text(json.dumps(self.job))
        result = revise_job(self.job_file, run, "MOCK pronunciation needs a new voice")
        self.assertEqual(result["invalidated"], ["audio", "lipsync", "edit"])
        self.assertIn("image", result["retained"])
        self.assertEqual(Run(run).status()["charged_or_reserved_usd"], "0")

    def test_new_jobs_default_to_speed_110_and_reject_invalid_speeds(self):
        job, _, errors = load_job(self.job_file)
        self.assertFalse(errors)
        self.assertEqual(job.get("fish_speed"), 1.1)
        for speed in (None, True, "1.25", 0.49, 2.01, float("nan"), float("inf")):
            with self.subTest(speed=speed):
                self.assertTrue(any("speed" in error.lower() for error in validate_job(
                    {**self.job, "fish_speed": speed}, self.job_file)))

    def test_pipeline_passes_generation_speed_and_records_it_with_audio_hash(self):
        for speed in (None, 1.25, 1.3, 1.2):
            with self.subTest(speed=speed):
                if speed is not None:
                    self.job["fish_speed"] = speed
                else:
                    self.job.pop("fish_speed", None)
                self.job_file.write_text(json.dumps(self.job))
                run = self.root / f"speed-{speed}"
                calls = []

                class Services:
                    def tts(service, text, voice_id, destination, **settings):
                        calls.append(settings)
                        destination.write_bytes(b"MOCK AUDIO")

                with patch("baby_video.pipeline.probe", return_value={"duration_seconds": 8.8}), \
                        patch("baby_video.pipeline.check_speech_duration", return_value={"passed": True}):
                    for stage in ("source", "dialogue"):
                        self.assertEqual(resume(self.job_file, run, mode="mock", services=Services())["stage"], stage)
                        fixture_decision(Run(run), stage)
                    result = resume(self.job_file, run, mode="mock", services=Services())
                expected = 1.1 if speed is None else speed
                self.assertEqual(result["stage"], "audio")
                self.assertEqual(calls[0].get("speed"), expected)
                request = json.loads((run / "inputs/fish-tts-request.json").read_text())
                self.assertEqual(request["body"]["prosody"]["speed"], expected)
                report = json.loads((run / "inputs/speech-duration.json").read_text())
                self.assertEqual(request["audio_sha256"], report["audio_sha256"])
                self.assertEqual(report["fish_speed"], expected)

    def test_changed_speed_invalidates_audio_and_sync_but_retains_paid_visual_lineage(self):
        self.job["fish_speed"] = 1.3
        self.job_file.write_text(json.dumps(self.job))
        run = self.root / "speed-revision"
        resume(self.job_file, run, mode="mock", services=object())
        self.job["fish_speed"] = 1.25
        self.job_file.write_text(json.dumps(self.job))
        result = revise_job(self.job_file, run, "Generation speed 1.3 to 1.25")
        self.assertEqual(result["invalidated"], ["audio", "lipsync", "edit"])
        self.assertTrue({"image", "video", "upscale"}.issubset(result["retained"]))

    def test_legacy_unspecified_speed_is_not_silently_relabelled_110(self):
        spec = build_spec(self.job_file, self.job)
        spec["job"].pop("fish_speed", None)
        spec = {**spec, "budget_usd": "0", "stages": [{**stage, "max_cost_usd": "0"} for stage in spec["stages"]]}
        run = self.root / "legacy-speed"
        Run.create(run, spec, mode="mock")
        with self.assertRaisesRegex(PipelineError, "inputs or mode changed"):
            resume(self.job_file, run, mode="mock", services=object())
        self.assertEqual(Run(run).status()["attempts"], [])

    def test_auto_approval_or_larger_budget_blocks_live_before_provider(self):
        self.job.update(auto_approve=True, budget_usd="2.01")
        self.job_file.write_text(json.dumps(self.job))
        with patch("baby_video.pipeline.Fal") as provider:
            result = resume(self.job_file, self.root / "run")
        provider.assert_not_called()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("2.00" in message for message in result["blockers"]))

    def test_standing_spending_replaces_routine_billing_prompt_but_preserves_live_gates(self):
        job = {**self.job, "standing_spending_authorized": True,
               "budget_usd": "2.01", "billing_approved": False, "pricing_verified": True,
               "media_policy": "public_expiring", "media_policy_confirmed": True,
               "fish_data_policy_confirmed": True}
        with patch.dict("os.environ", {"FISH_API_KEY": "fixture", "FAL_KEY": "fixture"}):
            self.assertEqual(validate_job(job, self.job_file, live=True), [])
            self.assertTrue(any("pricing" in message for message in validate_job(
                {**job, "pricing_verified": False}, self.job_file, live=True)))
            self.assertTrue(any("media" in message for message in validate_job(
                {**job, "media_policy_confirmed": False}, self.job_file, live=True)))
            self.assertTrue(any("data-use" in message for message in validate_job(
                {**job, "fish_data_policy_confirmed": False}, self.job_file, live=True)))
            self.assertTrue(any("license" in message for message in validate_job(
                {**job, "voice_license_confirmed": False}, self.job_file, live=True)))
            self.assertTrue(any("2.00" in message for message in validate_job(
                {**job, "standing_spending_authorized": False}, self.job_file, live=True)))
        invalid = validate_job({**job, "standing_spending_authorized": "true"}, self.job_file)
        self.assertTrue(any("explicit boolean" in message for message in invalid))

    def test_standing_spending_still_requires_verified_request_caps(self):
        with self.assertRaisesRegex(PipelineError, "per-request cost cap"):
            build_spec(self.job_file, {**self.job, "standing_spending_authorized": True,
                                      "cost_caps_usd": {"audio": None, "video": ".24", "lipsync": ".15"}})

    def test_mock_requires_offline_provider_set(self):
        with self.assertRaises(PipelineError):
            resume(self.job_file, self.root / "run", mode="mock")

    def test_reconcile_preserves_attempt_and_reservation(self):
        spec = {"budget_usd": "1", "stages": [{"id": "video", "provider": H3_MODEL, "max_cost_usd": ".3"}]}
        run = Run.create(self.root / "run", spec)
        attempt = run.claim("video")
        run.fail(attempt, acceptance_unknown=True)
        run.submitted(attempt, "verified-existing-job")
        status = run.status()
        self.assertEqual(len(status["attempts"]), 1)
        self.assertEqual(status["charged_or_reserved_usd"], "0.3")
        self.assertEqual(status["stages"][0]["status"], "submitted")

    def test_cost_settlement_does_not_reset_job(self):
        spec = {"budget_usd": "1", "stages": [{"id": "video", "provider": H3_MODEL, "max_cost_usd": ".3"}]}
        run = Run.create(self.root / "run", spec)
        attempt = run.claim("video")
        p = run.directory / "video.mp4"
        p.write_bytes(b"MOCK")
        run.complete(attempt, p)
        run.settle_cost(attempt, ".225")
        self.assertEqual(run.status()["charged_or_reserved_usd"], "0.225")
        self.assertEqual(run.status()["stages"][0]["status"], "complete")


if __name__ == "__main__":
    unittest.main()

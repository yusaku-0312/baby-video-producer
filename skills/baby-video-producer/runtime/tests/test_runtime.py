import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from baby_video.media import LipSyncContract, preflight_lipsync
from baby_video.state import PipelineError, Run, micros
from baby_video.transport import RemoteJob, poll_with_retry, submit_once


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = {"procedure_confirmed": True, "budget_usd": "1.00", "stages": [
            {"id": "video", "provider": "mock", "max_cost_usd": "0.6"},
            {"id": "lipsync", "provider": "mock", "max_cost_usd": "0.4"},
        ]}
        self.run = Run.create(self.root, self.spec)

    def artifact(self, name="artifact.bin"):
        p = self.root / name
        p.write_bytes(b"test artifact")
        return p

    def test_stage_order(self):
        with self.assertRaises(PipelineError):
            self.run.claim("lipsync")

    def test_duplicate_submit_blocked_after_restart(self):
        attempt = self.run.claim("video")
        self.run.submitted(attempt, "request-123")
        restarted = Run(self.root)
        with self.assertRaises(PipelineError):
            restarted.claim("video")
        self.assertEqual(restarted.status()["attempts"][0]["request_id"], "request-123")

    def test_crash_during_submit_blocks_retry(self):
        self.run.claim("video")
        with self.assertRaises(PipelineError):
            Run(self.root).claim("video")
        self.assertEqual(self.run.status()["charged_or_reserved_usd"], "0.6")

    def test_concurrent_claim_only_one_wins(self):
        def claim():
            try:
                return self.run.claim("video")
            except PipelineError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(x is not None for x in results), 1)

    def test_unknown_acceptance_keeps_reserve(self):
        attempt = self.run.claim("video")
        self.run.fail(attempt, acceptance_unknown=True)
        with self.assertRaises(PipelineError):
            self.run.retry_confirmed_failure("video")
        self.assertEqual(self.run.status()["charged_or_reserved_usd"], "0.6")

    def test_paid_failed_attempt_remains_in_budget(self):
        attempt = self.run.claim("video")
        self.run.fail(attempt, acceptance_unknown=False, actual_usd="0.5")
        self.run.retry_confirmed_failure("video")
        with self.assertRaises(PipelineError):
            self.run.claim("video")
        self.assertEqual(len(self.run.status()["attempts"]), 1)

    def test_confirmed_unaccepted_attempt_can_retry(self):
        attempt = self.run.claim("video")
        self.run.fail(attempt, acceptance_unknown=False, actual_usd="0")
        self.run.retry_confirmed_failure("video")
        self.assertEqual(self.run.claim("video"), 2)

    def test_cost_unknown_reported_as_upper_bound(self):
        attempt = self.run.claim("video")
        self.run.complete(attempt, self.artifact())
        self.assertEqual(self.run.status()["charged_or_reserved_usd"], "0.6")
        self.assertEqual(self.run.status()["confirmed_actual_usd"], "0")

    def test_overspend_recorded_and_next_stage_blocked(self):
        attempt = self.run.claim("video")
        self.run.complete(attempt, self.artifact(), actual_usd="1.2")
        self.assertTrue(self.run.status()["budget_exceeded"])
        with self.assertRaises(PipelineError):
            self.run.claim("lipsync")

    def test_input_change_and_mode_change_rejected(self):
        changed = {**self.spec, "budget_usd": "2"}
        with self.assertRaises(PipelineError):
            Run.create(self.root, changed)
        with self.assertRaises(PipelineError):
            Run.create(self.root, self.spec, mode="live")

    def test_unconfirmed_workflow_cannot_enable_live(self):
        with self.assertRaises(PipelineError):
            Run.create(self.root / "partial", {**self.spec, "procedure_confirmed": False}, "live")

    def test_artifact_tampering_blocks_next_stage(self):
        attempt = self.run.claim("video")
        artifact = self.artifact()
        self.run.complete(attempt, artifact, actual_usd="0.1")
        artifact.write_bytes(b"changed")
        self.assertFalse(self.run.status()["stages"][0]["artifact_valid"])
        with self.assertRaises(PipelineError):
            self.run.claim("lipsync")

    def test_signed_url_not_stored_as_request_id(self):
        attempt = self.run.claim("video")
        with self.assertRaises(PipelineError):
            self.run.submitted(attempt, "https://example.invalid?token=secret")

    def test_budget_precision(self):
        self.assertEqual(micros("0.123456"), 123456)
        for invalid in ("NaN", "Infinity", "-1", "0.0000001"):
            with self.assertRaises(PipelineError):
                micros(invalid)

    def test_submit_timeout_is_never_retried_or_logged(self):
        class BrokenProvider:
            calls = 0
            def submit(self):
                self.calls += 1
                raise TimeoutError("Authorization: very-secret-key")
        provider = BrokenProvider()
        with self.assertRaises(PipelineError) as error:
            submit_once(self.run, "video", provider)
        self.assertNotIn("very-secret", str(error.exception))
        with self.assertRaises(PipelineError):
            submit_once(self.run, "video", provider)
        self.assertEqual(provider.calls, 1)
        self.assertNotIn("very-secret", json.dumps(self.run.status()))

    def test_poll_transient_reads_use_bounded_backoff(self):
        class Provider:
            calls = 0
            def poll(self, request_id):
                self.calls += 1
                if self.calls < 3:
                    raise ConnectionError()
                return RemoteJob("complete", request_id)
        waits = []
        self.assertEqual(poll_with_retry(Provider(), "job-1", delay=waits.append).status, "complete")
        self.assertEqual(waits, [1, 2])

    def test_12_second_clip_rejected_before_kling_submit(self):
        video = {"duration_seconds": 12, "size_bytes": 10, "streams": [{"codec_type": "video"}]}
        audio = {"duration_seconds": 9, "size_bytes": 10, "streams": [{"codec_type": "audio"}]}
        with patch("baby_video.media.probe", side_effect=[video, audio]):
            with self.assertRaises(PipelineError):
                preflight_lipsync(Path("video"), Path("audio"), LipSyncContract("Kling", 2, 10))

    def test_longer_audio_rejected(self):
        video = {"duration_seconds": 10, "size_bytes": 10, "streams": [{"codec_type": "video"}]}
        audio = {"duration_seconds": 11, "size_bytes": 10, "streams": [{"codec_type": "audio"}]}
        with patch("baby_video.media.probe", side_effect=[video, audio]):
            with self.assertRaises(PipelineError):
                preflight_lipsync(Path("video"), Path("audio"), LipSyncContract("Kling", 2, 10))


if __name__ == "__main__":
    unittest.main()

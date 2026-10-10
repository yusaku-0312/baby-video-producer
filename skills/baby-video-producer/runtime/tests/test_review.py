import json
import tempfile
import unittest
from pathlib import Path

from baby_video.pipeline import STAGES
from baby_video.review import REQUIRED_CHECKS
from baby_video.state import Run, PipelineError, digest_file


def material(run, stage, text="MOCK material"):
    folder = run.directory / "artifacts"
    folder.mkdir(exist_ok=True)
    file = folder / f"{stage}.txt"
    file.write_text(text)
    attempt = run.claim(stage)
    run.complete(attempt, file, actual_usd="0.1" if stage in {"audio", "video", "lipsync"} else "0")
    return file


def evidence(file, checks, summary="MOCK observation only", unverified=None):
    return {"summary": summary, "checks": list(checks), "evidence_files": ["artifacts/" + file.name],
            "unverified": unverified or []}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = {"procedure_confirmed": True, "review_required": True, "budget_usd": "2",
            "stages": [{"id": s, "provider": p, "max_cost_usd": ".1" if s in {"audio", "video", "lipsync"} else "0"}
                       for s, p in STAGES]}
        self.run = Run.create(self.root / "run", self.spec)

    def finish(self, stage):
        file = material(self.run, stage)
        self.run.decide(stage, "next", evidence(file, REQUIRED_CHECKS[stage]), actor="mock_test")
        return file

    def test_revision_reassessment_and_next_are_distinct(self):
        self.finish("source")
        file = material(self.run, "dialogue", "MOCK awkward sentence")
        self.run.decide("dialogue", "revise", evidence(file, ["text_read"], "MOCK sentence is too long"))
        with self.assertRaises(PipelineError):
            self.run.claim("audio")
        self.run.invalidate(["dialogue"], "MOCK shorten the wording")
        file = material(self.run, "dialogue", "MOCK shorter natural sentence")
        status = self.run.status()
        dialogue = next(s for s in status["stages"] if s["id"] == "dialogue")
        self.assertEqual(dialogue["revision"], 1)
        self.assertFalse(dialogue["review_approved"])
        with self.assertRaises(PipelineError):
            self.run.claim("audio")
        self.run.decide("dialogue", "next", evidence(file, ["text_read"], "MOCK revised sentence re-read"))
        self.assertIsInstance(self.run.claim("audio"), int)
        self.assertTrue((self.run.directory / "history/dialogue/r0/dialogue.txt").is_file())

    def test_audio_change_invalidates_only_necessary_descendants(self):
        for stage, _ in STAGES:
            self.finish(stage)
        before = self.run.status()
        hashes = {s["id"]: s["artifact_hash"] for s in before["stages"]}
        captions = self.run.directory / "inputs/captions.json"
        captions.parent.mkdir()
        captions.write_text("MOCK stale timing")
        self.run.decide("audio", "regenerate", evidence(self.run.directory / "artifacts/audio.txt", ["audio_listened"],
            "MOCK pronunciation needs adjustment"))
        result = self.run.invalidate(["audio"], "MOCK change spoken nuance")
        self.assertEqual(result["invalidated"], ["audio", "lipsync", "edit"])
        self.assertFalse(captions.exists())
        for s in self.run.status()["stages"]:
            if s["id"] in result["retained"]:
                self.assertEqual(s["artifact_hash"], hashes[s["id"]])
                self.assertTrue(s["review_approved"])
        self.assertEqual(self.run.status()["charged_or_reserved_usd"], before["charged_or_reserved_usd"])
        self.assertEqual(len(self.run.status()["attempts"]), 9)
        for stage in result["invalidated"]:
            self.finish(stage)
        attempts = self.run.status()["attempts"]
        counts = {stage: sum(a["stage"] == stage for a in attempts) for stage, _ in STAGES}
        self.assertEqual(counts["video"], 1)
        self.assertEqual((counts["audio"], counts["lipsync"], counts["edit"]), (2, 2, 2))
        self.assertEqual(self.run.status()["charged_or_reserved_usd"], "0.5")

    def test_unheard_audio_or_unwatched_video_cannot_pass(self):
        for stage in ("source", "dialogue"):
            self.finish(stage)
        file = material(self.run, "audio")
        with self.assertRaises(PipelineError):
            self.run.decide("audio", "next", evidence(file, ["technical_checked"]))
        self.run.decide("audio", "needs_human", evidence(file, [], unverified=["Actual listening unavailable"]))
        with self.assertRaises(PipelineError):
            self.run.claim("image")
        self.run.decide("audio", "next", evidence(file, ["audio_listened"], "MOCK owner reviewed fixture"), actor="human")
        self.finish("image")
        file = material(self.run, "video")
        with self.assertRaises(PipelineError):
            self.run.decide("video", "next", evidence(file, ["image_viewed"]))
        with self.assertRaises(PipelineError):
            self.run.decide("video", "next", evidence(file, ["video_watched_full"], unverified=["Opening motion unknown"]))

    def test_modified_secondary_evidence_revokes_approval(self):
        file = material(self.run, "source")
        secondary = self.run.directory / "artifacts/fact-review.json"
        secondary.write_text("MOCK reviewed claim")
        record = evidence(file, ["text_read"])
        record["evidence_files"].append("artifacts/fact-review.json")
        self.run.decide("source", "next", record)
        secondary.write_text("MOCK different claim")
        self.assertFalse(self.run.status()["stages"][0]["review_approved"])
        with self.assertRaises(PipelineError):
            self.run.claim("dialogue")

    def test_go_back_requires_earlier_target(self):
        self.finish("source")
        file = material(self.run, "dialogue")
        with self.assertRaises(PipelineError):
            self.run.decide("dialogue", "go_back", evidence(file, ["text_read"]), target="audio")
        self.run.decide("dialogue", "go_back", evidence(file, ["text_read"]), target="source")
        result = self.run.invalidate(["source"], "MOCK original claim changed")
        self.assertEqual(result["invalidated"], [s for s, _ in STAGES])

    def test_in_flight_revision_cannot_release_or_duplicate_charge(self):
        self.finish("source")
        self.finish("dialogue")
        attempt = self.run.claim("audio")
        self.run.fail(attempt, acceptance_unknown=True)
        with self.assertRaises(PipelineError):
            self.run.invalidate(["dialogue"], "MOCK revise while outcome unknown")
        self.assertEqual(self.run.status()["charged_or_reserved_usd"], "0.1")
        self.assertTrue(self.run.status()["stages"][0]["review_approved"])

    def test_shared_two_dollar_limit_survives_new_run_and_revision(self):
        spec = {"procedure_confirmed": True, "budget_usd": "2", "shared_budget_usd": "2",
            "budget_ledger_path": str(self.root / "production-budget.sqlite3"),
            "stages": [{"id": "audio", "provider": "MOCK ledger only", "max_cost_usd": "1.2"}]}
        # live MODE exercises accounting only; material() never contacts a provider.
        run = Run.create(self.root / "live-ledger-1", spec, mode="live")
        file = material(run, "audio")
        run.settle_cost(1, "1.2")
        with self.assertRaises(PipelineError):
            run.decide("audio", "next", evidence(file, ["audio_listened"]), actor="mock_test")
        run.invalidate(["audio"], "MOCK accounting-only regeneration")
        with self.assertRaises(PipelineError):
            run.claim("audio")
        other = Run.create(self.root / "live-ledger-2", spec, mode="live")
        with self.assertRaises(PipelineError):
            other.claim("audio")
        self.assertEqual(other.status()["shared_charged_or_reserved_usd"], "1.2")
        self.assertEqual(len(other.status()["attempts"]), 0)
        changed = {**spec, "budget_ledger_path": str(self.root / "different.sqlite3")}
        with self.assertRaises(PipelineError):
            run.invalidate(["audio"], "MOCK cannot reset pool", new_spec=changed)

    def test_pending_import_is_removed_when_image_ancestors_change(self):
        assets = self.run.directory / "artifacts"
        assets.mkdir()
        (assets / "image.png").write_bytes(b"MOCK old import")
        (assets / "image-provenance.json").write_text("MOCK old provenance")
        self.run.invalidate(["source"], "MOCK source changed before image stage")
        self.assertFalse((assets / "image.png").exists())
        self.assertFalse((assets / "image-provenance.json").exists())

    def test_approval_disable_and_budget_increase_are_rejected(self):
        for changed in ({**self.spec, "budget_usd": "3"}, {**self.spec, "review_required": False}):
            with self.assertRaises(PipelineError):
                self.run.invalidate(["audio"], "MOCK unauthorized change", new_spec=changed)


if __name__ == "__main__":
    unittest.main()

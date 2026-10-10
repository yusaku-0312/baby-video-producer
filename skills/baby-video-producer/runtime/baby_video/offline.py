"""Explicit mock providers for the same orchestration used in live execution."""
import json
import shutil
import subprocess
from pathlib import Path

from .pipeline import resume, write_json, revise_job, STAGES
from .providers import H3_MODEL
from .state import PipelineError, digest_file, Run
from .review import REQUIRED_CHECKS


def fixture_decision(run, stage):
    """Explicit MOCK state-machine attestation; never a perceptual quality claim."""
    row = next(s for s in run.status()["stages"] if s["id"] == stage)
    return run.decide(stage, "next", {
        "summary": "MOCK state fixture only. Required check names simulate a decision; actual listening/full viewing quality is unverified.",
        "checks": sorted(REQUIRED_CHECKS.get(stage, {"technical_checked"})),
        "evidence_files": [row["artifact"]], "unverified": [],
    }, actor="mock_test")


def ffmpeg(*args):
    try:
        subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-n", *map(str, args)],
                       capture_output=True, check=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        raise PipelineError("Offline fixture generation failed") from None


class OfflineFal:
    def __init__(self, model, fixture_root, run_root, defer=True):
        self.model, self.fixtures, self.run, self.defer = model, fixture_root, run_root, defer
        self.kind = "h3" if model == H3_MODEL else "kling"

    def upload(self, file, content_type):
        if not file.is_file():
            raise PipelineError("Missing mock upload input")
        return "mock://" + file.name

    def submit(self, payload):
        counter = self.run / f"MOCK-{self.kind}-submit-count.txt"
        count = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(count + 1))
        return f"mock-{self.kind}-request-{count + 1}"

    def status(self, request_id):
        marker = self.run / f"MOCK-{self.kind}-polled.txt"
        if self.defer and not marker.exists():
            marker.write_text(request_id)
            return {"request_id": request_id, "status": "IN_PROGRESS", "failed": False}
        return {"request_id": request_id, "status": "COMPLETED", "failed": False}

    def download_result(self, request_id, destination):
        if self.kind == "h3":
            shutil.copyfile(self.fixtures / "MOCK-video.mp4", destination)
        else:
            ffmpeg("-i", self.run / "artifacts/portrait.mp4", "-i", self.run / "artifacts/voice.mp3",
                   "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-af", "apad",
                   "-c:a", "aac", "-shortest", destination)


class OfflineServices:
    def __init__(self, fixture_root, run_root, defer=True):
        self.fixtures, self.run, self.defer = fixture_root, run_root, defer

    def tts(self, text, voice_id, destination, *, model, speed):
        count = self.run / "MOCK-tts-count.txt"
        value = int(count.read_text()) if count.exists() else 0
        count.write_text(str(value + 1))
        shutil.copyfile(self.fixtures / "MOCK-voice.mp3", destination)

    def fal(self, model):
        return OfflineFal(model, self.fixtures, self.run, self.defer)


def create_fixture(root: Path):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    required = ["MOCK-video.mp4", "MOCK-voice.mp3", "MOCK-bgm.wav", "MOCK-image.png", "job.json"]
    if all((root / f).exists() for f in required):
        return
    if any((root / f).exists() for f in required):
        raise PipelineError("Partial mock fixture exists; use a fresh directory")
    ffmpeg("-f", "lavfi", "-i", "color=c=0x223344:s=480x854:r=24:d=9",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", root / "MOCK-video.mp4")
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=660:sample_rate=44100:duration=8.8",
           "-c:a", "libmp3lame", "-b:a", "128k", root / "MOCK-voice.mp3")
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100:duration=12",
           root / "MOCK-bgm.wav")
    ffmpeg("-f", "lavfi", "-i", "color=c=0x223344:s=480x854", "-frames:v", "1", root / "MOCK-image.png")
    (root / "source.md").write_text("# MOCK SOURCE\nOffline fixture only. No parenting or medical claim is made.\n")
    (root / "image-prompt.txt").write_text("MOCK IMAGE; no child or identity reference.\n")
    (root / "video-prompt.txt").write_text("MOCK VIDEO; do not submit this fixture to an API.\n")
    write_json(root / "dialogue-candidates.json", {"candidates": [
        {"text": "モック素材で字幕と音量を確認します。", "mock": True,
         "estimated_seconds": 8.8, "evidence": []} for i in range(10)]})
    template = json.loads((Path(__file__).resolve().parent.parent / "examples/job.live-template.json").read_text())
    template.update({"procedure_confirmed": True, "review_policy": "strict",
        "voice_id": "mock-voice-no-real-model", "voice_confirmed": True,
        "article_url": "https://example.invalid/offline-fixture", "article_file": "source.md",
        "source_facts_reviewed": True, "voice_license_confirmed": True, "reference_pixels_reviewed": True,
        "selection_mode": "agent", "selected_candidate": 0,
        "selection_reason": "MOCK ONLY: explicit fixture index, not score-based selection",
        "bgm_file": "MOCK-bgm.wav", "budget_usd": "1",
        "cost_caps_usd": {"audio": "0.01", "video": "0.3", "lipsync": "0.15"},
        "notes": "MOCK ONLY. Approval and review flags describe synthetic test fixtures and authorize no real API call."})
    write_json(root / "job.json", template)


def integrated_mock(root: Path) -> dict:
    root = root.resolve()
    fixtures = root / "fixtures"
    create_fixture(fixtures)
    run = root / "run"
    assets, inputs = run / "artifacts", run / "inputs"
    assets.mkdir(parents=True, exist_ok=True)
    inputs.mkdir(exist_ok=True)
    image = assets / "image.png"
    if not image.exists():
        shutil.copyfile(fixtures / "MOCK-image.png", image)
        write_json(assets / "image-provenance.json", {"generated_by": "offline_mock", "file": "image.png",
            "sha256": digest_file(image), "prompt_sha256": digest_file(fixtures / "image-prompt.txt")})
        (inputs / "description.md").write_text("MOCK ONLY — no generation API was called.\n")
    if not (inputs / "captions.json").exists():
        # A revision gets a fresh synthetic timing fixture, not perceptual QA.
        write_json(inputs / "captions.json", [
            {"text": "モック素材\n字幕・音量の確認", "from": 0.5, "to": 3.5},
            {"text": "実際の赤ちゃん動画ではありません", "from": 4, "to": 7.5},
        ])
    services = OfflineServices(fixtures, run)
    # Reenter the exact same orchestration through pending and submitted states.
    result = None
    for _ in range(30):
        result = resume(fixtures / "job.json", run, mode="mock", services=services)
        if result["status"] == "review_required":
            fixture_decision(Run(run), result["stage"])
        if result["status"] == "complete":
            break
    if result["status"] != "complete":
        raise PipelineError("Integrated mock did not reach completion")
    result["submit_counts"] = {kind: int((run / f"MOCK-{kind}-submit-count.txt").read_text()) for kind in ("h3", "kling")}
    result["tts_count"] = int((run / "MOCK-tts-count.txt").read_text())
    result["mock_only"] = True
    result["review_actor"] = "mock_test"
    result["perceptual_quality_verified"] = False
    write_json(root / "verification.json", result)
    return result


def agent_mock_scenarios(root: Path) -> dict:
    """Exercise agent returns and minimal regeneration with explicit MOCK actors."""
    root = root.resolve()
    if (root / "verification.json").exists():
        return json.loads((root / "verification.json").read_text())
    fixtures, directory = root / "revision/fixtures", root / "revision/run"
    create_fixture(fixtures)
    services = OfflineServices(fixtures, directory)
    first = resume(fixtures / "job.json", directory, mode="mock", services=services)
    run = Run(directory)
    fixture_decision(run, first["stage"])
    initial = resume(fixtures / "job.json", directory, mode="mock", services=services)
    run.decide("dialogue", "revise", {"summary": "MOCK: initial wording needs a shorter revision.",
        "checks": ["text_read"], "evidence_files": [initial["artifact"]], "unverified": []}, actor="mock_test")
    value = json.loads((fixtures / "dialogue-candidates.json").read_text())
    value["candidates"][0]["text"] = "モック字幕の改訂を確認します。"
    write_json(fixtures / "dialogue-candidates.json", value)
    revised = revise_job(fixtures / "job.json", directory, "MOCK: shorten wording, retain source and image lineage")
    reassessed = resume(fixtures / "job.json", directory, mode="mock", services=services)
    before_next = run.status()
    assert reassessed["stage"] == "dialogue" and reassessed["revision"] == 1
    assert not next(s for s in before_next["stages"] if s["id"] == "dialogue")["review_approved"]
    fixture_decision(run, "dialogue")
    assert next(s for s in run.status()["stages"] if s["id"] == "dialogue")["review_approved"]
    revision = {"decisions": [d["choice"] for d in run.status()["decisions"] if d["stage"] == "dialogue"],
        "revision": reassessed["revision"], "invalidated": revised["invalidated"],
        "new_audio_generated_before_reassessment": False, "passed": True}

    integrated_mock(root / "reuse")
    run = Run(root / "reuse/run")
    before = run.status()
    hashes = {s["id"]: s["artifact_hash"] for s in before["stages"]}
    audio = next(s for s in before["stages"] if s["id"] == "audio")
    run.decide("audio", "regenerate", {"summary": "MOCK: simulate changing speech nuance.",
        "checks": ["audio_listened"], "evidence_files": [audio["artifact"]], "unverified": []}, actor="mock_test")
    job_file = root / "reuse/fixtures/job.json"
    job = json.loads(job_file.read_text())
    job["fish_model"] = "MOCK-revised-engine"
    write_json(job_file, job)
    revised = revise_job(job_file, run.directory, "MOCK: voice change only")
    assert revised["invalidated"] == ["audio", "lipsync", "edit"]
    after = integrated_mock(root / "reuse")
    status = run.status()
    assert all(s["artifact_hash"] == hashes[s["id"]] and s["review_approved"]
               for s in status["stages"] if s["id"] in revised["retained"])
    assert after["tts_count"] == 2 and after["submit_counts"] == {"h3": 1, "kling": 2}
    counts = {stage: sum(a["stage"] == stage for a in status["attempts"]) for stage, _ in STAGES}
    assert counts["edit"] == 2 and len(status["attempts"]) == len(before["attempts"]) + 3
    result = {"mock_only": True, "review_actor": "mock_test", "live_api_called": False,
        "perceptual_quality_verified": False, "revision_reassessment_next": revision,
        "upstream_change_minimal_regeneration": {"passed": True, **revised, "attempt_counts": counts,
            "old_attempts_retained": True, "retained_artifact_hashes_unchanged": True,
            "mock_charged_or_reserved_usd": status["charged_or_reserved_usd"]},
        "budget_protection": "Unit tests cover paid cost, unknown reservations and shared USD 2 across live-mode ledgers; no providers called"}
    write_json(root / "verification.json", result)
    return result

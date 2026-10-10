from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

from .media import LipSyncContract, check_vertical_output, preflight_lipsync
from .state import PipelineError, Run
from .pipeline import import_image, preview_job, resume, revise_job
from .offline import integrated_mock, agent_mock_scenarios
from .captions import align_captions
from .credentials import activate_credentials


def doctor() -> dict:
    return {"python": sys.version.split()[0],
            "executables": {x: shutil.which(x) for x in ("node", "ffmpeg", "ffprobe", "codex", "whisper-cli")},
            "credential_environment_present": {x: bool(os.environ.get(x)) for x in
                                                ("FISH_API_KEY", "FISH_AUDIO_API_KEY", "FAL_KEY")},
            "credential_source": str(Path(__file__).resolve().parent.parent / ".env.local"),
            "legacy_credential_fallback": False,
            "image_gen": "Agent-mediated Codex tool handoff; CLI alone cannot call the tool; no OpenAI API key required",
            "live_api_called": False}


def mock_artifact(run: Run, stage: str) -> Path:
    """Offline artifacts are explicitly mock; no synthetic output is called image_gen."""
    folder = run.directory / "artifacts"
    folder.mkdir(exist_ok=True)
    if stage == "audio":
        artifact = folder / "MOCK-audio.wav"
        with wave.open(str(artifact), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(bytes(2 * 24000 * 2))
    elif stage in {"video", "lipsync", "edit"}:
        artifact = folder / f"MOCK-{stage}.mp4"
        if stage == "video":
            try:
                subprocess.run([
                    shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=0x223344:s=1080x1920:r=30:d=2",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(artifact),
                ], capture_output=True, check=True, timeout=60)
            except (OSError, subprocess.SubprocessError):
                raise PipelineError("Offline mock video render failed") from None
        else:
            source = folder / ("MOCK-video.mp4" if stage == "lipsync" else "MOCK-lipsync.mp4")
            shutil.copyfile(source, artifact)
    else:
        artifact = folder / f"MOCK-{stage}.json"
        artifact.write_text(json.dumps({"mode": "mock", "stage": stage,
                                       "generated_by": "offline_mock", "live_api_called": False}, indent=2))
    return artifact


def main(argv=None) -> int:
    try:
        activate_credentials(Path(__file__).resolve().parent.parent)
    except (PipelineError, OSError):
        print(json.dumps({"error": "Local environment loading failed; no values were logged", "live_api_called": False}))
        return 2
    parser = argparse.ArgumentParser(description="Baby-video pipeline preparation and offline verification")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    align = sub.add_parser("align-captions")
    align.add_argument("--audio", type=Path, required=True)
    align.add_argument("--text-file", type=Path, required=True)
    align.add_argument("--model", type=Path, required=True)
    align.add_argument("--output", type=Path, required=True)
    align.add_argument("--whisper-cli", default="whisper-cli")
    align.add_argument("--dtw-model", default="small")
    preview = sub.add_parser("preview-job")
    preview.add_argument("--job", type=Path, required=True)
    live = sub.add_parser("resume")
    live.add_argument("--job", type=Path, required=True)
    live.add_argument("--run", type=Path, required=True)
    integrated = sub.add_parser("mock-integration")
    integrated.add_argument("--run", type=Path, required=True)
    scenarios = sub.add_parser("mock-agent-scenarios")
    scenarios.add_argument("--run", type=Path, required=True)
    decide = sub.add_parser("decide")
    decide.add_argument("--run", type=Path, required=True)
    decide.add_argument("--stage", required=True)
    decide.add_argument("--choice", choices=["next", "draft_usable", "revise", "regenerate", "go_back", "needs_human"], required=True)
    decide.add_argument("--evidence", type=Path, required=True)
    decide.add_argument("--target")
    decide.add_argument("--actor", choices=["codex_agent", "human"], default="codex_agent")
    revise = sub.add_parser("revise")
    revise.add_argument("--run", type=Path, required=True)
    revise.add_argument("--job", type=Path, required=True)
    revise.add_argument("--reason", required=True)
    revise.add_argument("--stage", action="append", default=[])
    image = sub.add_parser("import-image")
    image.add_argument("--run", type=Path, required=True)
    image.add_argument("--image", type=Path, required=True)
    image.add_argument("--prompt", type=Path, required=True)
    image.add_argument("--tool-generated", action="store_true", required=True,
                       help="Attest that Codex actually used the built-in image_gen tool and inspected its output")
    reconcile = sub.add_parser("reconcile-request")
    reconcile.add_argument("--run", type=Path, required=True)
    reconcile.add_argument("--attempt", type=int, required=True)
    reconcile.add_argument("--request-id", required=True,
                           help="Verified existing provider request ID; this command creates no job")
    retry = sub.add_parser("retry-failed")
    retry.add_argument("--run", type=Path, required=True)
    retry.add_argument("--stage", required=True)
    confirm = sub.add_parser("confirm-failed")
    confirm.add_argument("--run", type=Path, required=True)
    confirm.add_argument("--attempt", type=int, required=True)
    confirm.add_argument("--actual-usd", required=True)
    confirm.add_argument("--confirmed-terminal", action="store_true", required=True,
                         help="Provider outcome and billing have been checked; the attempt cannot still complete")
    settle = sub.add_parser("settle-cost")
    settle.add_argument("--run", type=Path, required=True)
    settle.add_argument("--attempt", type=int, required=True)
    settle.add_argument("--actual-usd", required=True, help="Actual amount verified in provider billing")
    dry = sub.add_parser("dry-run")
    dry.add_argument("--spec", type=Path, required=True)
    mock = sub.add_parser("mock")
    mock.add_argument("--spec", type=Path, required=True)
    mock.add_argument("--run", type=Path, required=True)
    status = sub.add_parser("status")
    status.add_argument("--run", type=Path, required=True)
    media = sub.add_parser("preflight-lipsync")
    media.add_argument("--video", type=Path, required=True)
    media.add_argument("--audio", type=Path, required=True)
    media.add_argument("--model", required=True)
    media.add_argument("--min-seconds", type=float, required=True)
    media.add_argument("--max-seconds", type=float, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor()
        elif args.command == "align-captions":
            result = align_captions(args.audio, args.text_file.read_text().strip(), args.model, args.output,
                                    whisper_cli=args.whisper_cli, dtw_model=args.dtw_model)
        elif args.command == "preview-job":
            result = preview_job(args.job.resolve())
        elif args.command == "resume":
            result = resume(args.job.resolve(), args.run)
        elif args.command == "mock-integration":
            result = integrated_mock(args.run)
        elif args.command == "mock-agent-scenarios":
            result = agent_mock_scenarios(args.run)
        elif args.command == "decide":
            result = Run(args.run).decide(args.stage, args.choice, json.loads(args.evidence.read_text()), target=args.target, actor=args.actor)
        elif args.command == "revise":
            result = revise_job(args.job.resolve(), args.run, args.reason, extra_roots=args.stage)
        elif args.command == "import-image":
            import_image(args.run, args.image, args.prompt)
            result = {"status": "image_imported", "producer": "image_gen", "run": str(args.run.resolve()),
                      "generation_api_called_by_cli": False}
        elif args.command == "reconcile-request":
            run = Run(args.run)
            run.submitted(args.attempt, args.request_id)
            result = {"status": "request_reconciled", "live_api_called": False}
        elif args.command == "retry-failed":
            Run(args.run).retry_confirmed_failure(args.stage)
            result = {"status": "confirmed_failed_stage_reset", "live_api_called": False}
        elif args.command == "confirm-failed":
            Run(args.run).fail(args.attempt, acceptance_unknown=False, actual_usd=args.actual_usd)
            result = {"status": "terminal_failure_recorded", "live_api_called": False}
        elif args.command == "settle-cost":
            run = Run(args.run)
            run.settle_cost(args.attempt, args.actual_usd)
            result = run.status()
        elif args.command == "dry-run":
            spec = json.loads(args.spec.read_text())
            result = {"procedure_confirmed": spec.get("procedure_confirmed", False),
                      "stage_order": [s["id"] for s in spec["stages"]],
                      "providers": [s["provider"] for s in spec["stages"]],
                      "budget_usd": spec["budget_usd"], "live_api_called": False,
                      "status": "Preparation only; production inputs and provider contracts must be confirmed"}
        elif args.command == "mock":
            spec = json.loads(args.spec.read_text())
            # Mock expenses must always be zero, even for a priced production spec.
            spec = {**spec, "budget_usd": "0", "stages": [
                {**s, "max_cost_usd": "0"} for s in spec["stages"]]}
            run = Run.create(args.run, spec, mode="mock")
            for stage in run.status()["stages"]:
                if stage["status"] == "complete":
                    if not stage.get("artifact_valid"):
                        raise PipelineError("Cached mock artifact changed; use a new run directory")
                    continue
                attempt = run.claim(stage["id"])
                artifact = mock_artifact(run, stage["id"])
                run.complete(attempt, artifact, actual_usd="0")
                # Legacy mock also returns to the agent after one material.
                break
            result = run.status()
            result["live_generation_verified"] = False
            edit = args.run / "artifacts" / "MOCK-edit.mp4"
            if edit.exists():
                result["media_check"] = check_vertical_output(edit)
        elif args.command == "status":
            result = Run(args.run).status()
        else:
            result = preflight_lipsync(args.video, args.audio, LipSyncContract(
                args.model, args.min_seconds, args.max_seconds))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PipelineError, OSError, ValueError, KeyError):
        # Print only our fixed safe errors, not raw config/provider/system data.
        exception = sys.exception()
        message = str(exception) if isinstance(exception, PipelineError) else "Invalid input or local file operation failed"
        print(json.dumps({"error": message, "live_api_called": "consult run state" if args.command == "resume" else False},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""One-clip CLI orchestration. Every resume polls existing jobs before submit."""
from __future__ import annotations

import json
import os
import shutil
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from .media import LipSyncContract, probe, preflight_lipsync, upscale_portrait, check_vertical_output, image_dimensions, check_speech_duration
from .providers import (Fal, H3_MODEL, KLING_MODEL, DEFAULT_FISH_SPEED,
                        fish_tts, fish_tts_payload, validate_fish_speed)
from .render import render
from .state import PipelineError, Run, digest_file, micros, usd
from .captions import align_captions
from .review import required_checks

APPROVED_TOTAL_USD = "2.00"
SHARED_LEDGER = Path(__file__).resolve().parent.parent / "runs/production-budget.sqlite3"

KLING_CONTRACT = LipSyncContract(KLING_MODEL, 2, 10, 5_000_000, 100_000_000,
                                min_video_side=720, max_video_side=1920)
STAGES = [("source", "local"), ("dialogue", "codex"), ("audio", "fish_audio"),
          ("image", "codex_image_gen"), ("video", H3_MODEL), ("upscale", "local"),
          ("lipsync", KLING_MODEL), ("edit", "local_remotion"), ("description", "codex")]

VIDEO_SMILE_DIRECTION = (
    "Mandatory expression requirement; this takes precedence over earlier expression directions: "
    "The baby must have a clearly visible, natural, happy smile from the very first frame through the final frame. "
    "Keep cheeks gently lifted and the expression cheerful throughout, including blinks and head movements. "
    "Keep the mouth fully visible and free to move naturally for later speech lip-sync; do not freeze or force the lips shut. "
    "Do not drift into a neutral, serious, sad or frowning expression at any time."
)


def write_json(path: Path, value: dict):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def resolve_input(job_file: Path, value: str) -> Path:
    p = Path(value)
    return p.resolve() if p.is_absolute() else (job_file.parent / p).resolve()


def estimate(text: str, duration: float) -> dict:
    """Provisional published rates only; these never authorize spending."""
    fish = Decimal(len(text.encode("utf-8"))) * Decimal("15") / 1_000_000
    h3 = Decimal(str(duration)) * Decimal("0.025")
    kling = (Decimal(str(duration)) / 5).to_integral_value(rounding=ROUND_CEILING) * 5 * Decimal("0.014")
    return {"fish_usd": str(fish), "h3_480p_usd": str(h3), "kling_usd": str(kling),
            "total_usd": str(fish + h3 + kling), "provisional": True,
            "excludes": "image_gen usage, taxes, retries; verify account pricing before live calls"}


def validate_job(job: dict, job_file: Path, *, live=False) -> list[str]:
    errors = []
    try:
        validate_fish_speed(job.get("fish_speed", DEFAULT_FISH_SPEED))
    except PipelineError as error:
        errors.append(str(error))
    if job.get("review_policy", "strict") not in {"strict", "script_only"}:
        errors.append("Unknown review policy; use strict or script_only")
    if "timing_revision_authorized" in job and not isinstance(job["timing_revision_authorized"], bool):
        errors.append("Timing revision authorization must be an explicit boolean")
    if "standing_spending_authorized" in job and not isinstance(job["standing_spending_authorized"], bool):
        errors.append("Standing spending authorization must be an explicit boolean")
    if job.get("auto_approve") or job.get("review_required") is False:
        errors.append("Production requires explicit agent decisions; automatic approval is disabled")
    if not job.get("article_url") or not job.get("article_file"):
        errors.append("An article URL and saved article text are required")
    for field in ("article_file", "dialogue_candidates_file", "image_prompt_file", "video_prompt_file"):
        if not job.get(field) or not resolve_input(job_file, job[field]).is_file():
            errors.append(f"Missing local input: {field}")
    if not job.get("source_facts_reviewed"):
        errors.append("Article facts need review")
    original_fictional = (job.get("image_reference_mode") == "original_fictional" and
                          job.get("new_fictional_face_authorized") is True and not job.get("reference_files"))
    if not original_fictional and not job.get("reference_pixels_reviewed"):
        errors.append("Reference image pixels need review before image generation")
    if not job.get("voice_id") or not job.get("voice_confirmed"):
        errors.append("Fish voice identity needs confirmation")
    if not job.get("voice_license_confirmed"):
        errors.append("Fish voice usage license needs confirmation")
    duration = job.get("video_duration_seconds", 9)
    if not isinstance(duration, (float, int)) or not 2 <= duration <= 9.3:
        errors.append("H3 duration must leave at least 0.7 seconds within the approved 10-second limit")
    if job.get("video_resolution") != "480P":
        errors.append("Current priced adapter is for 480P; verify pricing before changing resolution")
    if job.get("lipsync_model") != KLING_MODEL:
        errors.append("Lip-sync model has no confirmed adapter")
    if not job.get("procedure_confirmed"):
        errors.append("Production workflow needs confirmation")
    if live:
        if (not job.get("billing_approved") and job.get("standing_spending_authorized") is not True) or not job.get("pricing_verified"):
            errors.append("Approved spending limit and verified account pricing are required")
        if job.get("budget_usd") is None:
            errors.append("Missing approved USD budget")
        elif (job.get("standing_spending_authorized") is not True
              and micros(job["budget_usd"]) > micros(APPROVED_TOTAL_USD)):
            errors.append("Total authorization is USD 2.00; ask the owner before increasing it")
        if job.get("media_policy") != "public_expiring" or not job.get("media_policy_confirmed"):
            errors.append("Confirm expiring fal CDN media access before uploading")
        if not job.get("fish_data_policy_confirmed"):
            errors.append("Fish data-use policy needs review")
        if not os.environ.get("FISH_API_KEY") or not os.environ.get("FAL_KEY"):
            errors.append("Fish and fal environment credentials are required")
    return errors


def load_job(job_file: Path) -> tuple[dict, dict, list[str]]:
    job = json.loads(job_file.read_text())
    # Materialize the new generation default in the fingerprint. A legacy run
    # with unspecified (1x) speed must be explicitly revised before reuse.
    job.setdefault("fish_speed", DEFAULT_FISH_SPEED)
    # Reject key values in configuration. Only environment credentials are accepted.
    def has_secrets(value):
        if isinstance(value, dict):
            return any(k.lower() in {"api_key", "fal_key", "fish_api_key", "credentials", "authorization", "secret"}
                       or has_secrets(v) for k, v in value.items())
        if isinstance(value, list):
            return any(has_secrets(v) for v in value)
        return False
    if has_secrets(job):
        raise PipelineError("Credentials must not be stored in job files")
    errors = validate_job(job, job_file)
    candidates = {}
    if job.get("dialogue_candidates_file") and resolve_input(job_file, job["dialogue_candidates_file"]).is_file():
        value = json.loads(resolve_input(job_file, job["dialogue_candidates_file"]).read_text())
        items = value.get("candidates", [])
        selected = job.get("selected_candidate")
        selection_mode = job.get("selection_mode", "agent")
        if len(items) != 10 or any(not isinstance(item, dict) for item in items):
            errors.append("Supply exactly ten dialogue candidates")
        elif selection_mode not in {"agent", "manual"}:
            errors.append("Use agent or manual selection; numeric scoring cannot select or approve a script")
            selected = None
        if isinstance(selected, bool) or not isinstance(selected, int) or not 0 <= selected < len(items):
            errors.append("Codex must review all ten candidates and select an index from 0 to 9")
        elif not isinstance(items[selected].get("text"), str) or not items[selected]["text"].strip():
            errors.append("Selected dialogue is empty")
        else:
            reason = job.get("selection_reason") or items[selected].get("selection_reason")
            if selection_mode == "agent" and (not isinstance(reason, str) or not reason.strip()):
                errors.append("Agent selection needs a concise content-based observation")
            candidates = {"selected_candidate": selected, "selection_mode": selection_mode,
                          "selection_reason": reason or "User-specified candidate",
                          "text": items[selected]["text"].strip()}
            if job.get("laugh_suffix_enabled"):
                candidates["text"] += " へへへ、ハハハ、ぶー"
    return job, candidates, errors


def duration_target(job):
    value = job.get("video_duration_seconds", 9)
    return value if isinstance(value, (int, float)) else 9


def preview_job(job_file: Path) -> dict:
    job, selected, errors = load_job(job_file)
    return {"stage_order": [s[0] for s in STAGES], "blockers": errors,
            "dialogue_selection": selected,
            "fish_speed": job["fish_speed"], "speech_duration_range_seconds": [8.5, 10],
            "cost_estimate": estimate(selected.get("text", ""), job.get("video_duration_seconds", 9)),
            "billing_approved": bool(job.get("billing_approved") or job.get("standing_spending_authorized") is True),
            "live_api_called": False}


def build_spec(job_file: Path, job: dict) -> dict:
    hashes = {field: digest_file(resolve_input(job_file, job[field])) for field in
              ("article_file", "dialogue_candidates_file", "image_prompt_file", "video_prompt_file")}
    if job.get("bgm_file") and resolve_input(job_file, job["bgm_file"]).is_file():
        hashes["bgm_file"] = digest_file(resolve_input(job_file, job["bgm_file"]))
    hashes["reference_files"] = [digest_file(resolve_input(job_file, p)) for p in job.get("reference_files", [])]
    caps = job["cost_caps_usd"]
    if any(caps.get(s) is None or micros(caps[s]) <= 0 for s in ("audio", "video", "lipsync")):
        raise PipelineError("Set a positive, verified per-request cost cap for each API stage")
    return {"procedure_confirmed": True, "budget_usd": job["budget_usd"], "review_required": True,
            "budget_ledger_path": str(SHARED_LEDGER), "shared_budget_usd": APPROVED_TOTAL_USD,
            "job": job, "input_hashes": hashes,
            "stages": [{"id": id_, "provider": provider, "max_cost_usd": caps.get(id_, "0")}
                       for id_, provider in STAGES]}


def import_image(run_path: Path, image: Path, prompt: Path):
    """Called by Codex after a real built-in image_gen tool result is inspected."""
    if not image.is_file() or image.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise PipelineError("Expected a generated raster image")
    dimensions = image_dimensions(image)
    assets = run_path / "artifacts"
    assets.mkdir(parents=True, exist_ok=True)
    target = assets / ("image" + image.suffix.lower())
    provenance = assets / "image-provenance.json"
    if target.exists() or provenance.exists():
        raise PipelineError("Image already imported; use a new version for replacement")
    shutil.copyfile(image, target)
    write_json(provenance, {"generated_by": "image_gen", "file": target.name,
                           "sha256": digest_file(target), "prompt_sha256": digest_file(prompt),
                           **dimensions})


def resume(job_file: Path, run_path: Path, *, mode="live", services=None) -> dict:
    job, selected, errors = load_job(job_file)
    errors += validate_job(job, job_file, live=mode == "live")
    if errors:
        return {"status": "blocked", "blockers": list(dict.fromkeys(errors)), "live_api_called": False}
    if mode == "mock" and services is None:
        raise PipelineError("Mock execution requires an explicit offline provider set")
    spec = build_spec(job_file, job)
    if mode == "mock":
        spec = {**spec, "budget_usd": "0", "stages": [{**s, "max_cost_usd": "0"} for s in spec["stages"]]}
    run = Run.create(run_path, spec, mode=mode)
    assets = run.directory / "artifacts"
    inputs = run.directory / "inputs"
    assets.mkdir(exist_ok=True)
    inputs.mkdir(exist_ok=True)
    live_called = False

    def done(stage, file, attempt=None, paid=False):
        attempt = run.claim(stage) if attempt is None else attempt
        run.complete(attempt, file, actual_usd=None if paid else "0")

    def blocked(message):
        return {"status": "waiting", "message": message, "run": str(run.directory),
                "charged_or_reserved_usd": run.status()["charged_or_reserved_usd"],
                "live_api_called": live_called}

    def speech_duration_problem():
        file = assets / "voice.mp3"
        report = check_speech_duration(file)
        report["audio_sha256"] = digest_file(file)
        request_file = inputs / "fish-tts-request.json"
        if request_file.is_file():
            request = json.loads(request_file.read_text())
            if request.get("audio_sha256") == report["audio_sha256"]:
                report.update(fish_speed=request["body"]["prosody"]["speed"],
                              fish_model=request["model"], voice_id=request["body"]["reference_id"])
        write_json(inputs / "speech-duration.json", report)
        if not report["passed"]:
            revision_instruction = (
                "Revise wording within the approved source and meaning, record a 尺調整: revision, "
                "and regenerate without requesting script approval again. "
                if job.get("timing_revision_authorized") is True else
                "Revise the dialogue and obtain script approval before generating a new version. ")
            return (f"Speech generated at Fish speed {job['fish_speed']} must last 8.5–10 seconds, targeting about 9 seconds; "
                    "leading/trailing silence does not count. " + revision_instruction +
                    "Do not pad silence or stretch short speech.")
        return None

    def review_required(stage):
        current = run.status()
        row = next(s for s in current["stages"] if s["id"] == stage)
        return {"status": "review_required", "stage": stage, "artifact": row["artifact"],
                "artifact_sha256": row["artifact_hash"], "revision": row["revision"],
                "required_checks": sorted(required_checks(stage, job.get("review_policy", "strict"))),
                "choices": ["next", "revise", "regenerate", "go_back", "needs_human"],
                "message": "Codex must observe the material and record a concise decision before continuing",
                "run": str(run.directory), "charged_or_reserved_usd": current["charged_or_reserved_usd"],
                "shared_charged_or_reserved_usd": current.get("shared_charged_or_reserved_usd"),
                "live_api_called": live_called}

    for stage in run.status()["stages"]:
        name = stage["id"]
        if stage["status"] == "complete":
            if not stage.get("artifact_valid"):
                raise PipelineError("Saved artifact changed; restore it before proceeding")
            if name == "audio":
                problem = speech_duration_problem()
                if problem:
                    return blocked(problem)
            # A produced file is not a quality decision. Never skip unreviewed material.
            if not stage["progress_allowed"]:
                return review_required(name)
            continue
        if stage["status"] in {"submitting", "needs_reconciliation", "failed"}:
            return blocked(f"Stage {name} requires reconciliation; no automatic resubmission")
        if name == "source":
            file = assets / "source.md"
            shutil.copyfile(resolve_input(job_file, job["article_file"]), file)
            done(name, file)
        elif name == "dialogue":
            file = assets / "selected-dialogue.json"
            shutil.copyfile(resolve_input(job_file, job["dialogue_candidates_file"]), assets / "dialogue-candidates.json")
            write_json(file, selected)
            done(name, file)
        elif name == "audio":
            file = assets / "voice.mp3"
            attempt = run.claim(name)
            live_called = mode == "live"
            model, speed = job.get("fish_model", "s2.1-pro"), job["fish_speed"]
            request = {"model": model, "body": fish_tts_payload(selected["text"], job["voice_id"], speed=speed),
                       "speed_control": "generation_parameter", "mode": mode}
            write_json(inputs / "fish-tts-request.json", request)
            try:
                tts = services.tts if services else fish_tts
                tts(selected["text"], job["voice_id"], file, model=model, speed=speed)
            except PipelineError:
                run.fail(attempt, acceptance_unknown=True)
                return blocked("Fish outcome needs review; do not automatically submit TTS again")
            request["audio_sha256"] = digest_file(file)
            write_json(inputs / "fish-tts-request.json", request)
            done(name, file, attempt, paid=True)
            problem = speech_duration_problem()
            if problem:
                return blocked(problem)
            duration = probe(file)["duration_seconds"]
            if not 2 <= duration <= job["video_duration_seconds"]:
                return blocked("Audio does not fit the approved clip. Shorten the dialogue and create a reviewed new version; do not cut or accelerate speech")
        elif name == "image":
            if not 2 <= probe(assets / "voice.mp3")["duration_seconds"] <= job["video_duration_seconds"]:
                return blocked("Audio does not fit this version; revise the dialogue before generating an image")
            provenance = assets / "image-provenance.json"
            prompt = resolve_input(job_file, job["image_prompt_file"])
            if not provenance.exists():
                write_json(inputs / "image-gen-request.json", {"tool": "image_gen.imagegen",
                    "prompt": prompt.read_text(), "transparent_background": False,
                    "reference_files": job.get("reference_files", []),
                    "instructions": "Inspect reference pixels, call the built-in Codex tool, inspect the generated image, then use import-image. Do not substitute an Images API."})
                return blocked("Codex image_gen request is ready in inputs/image-gen-request.json")
            evidence = json.loads(provenance.read_text())
            if Path(evidence["file"]).name != evidence["file"] or not evidence["file"].startswith("image."):
                raise PipelineError("Image provenance must name a local run artifact")
            file = assets / evidence["file"]
            producer = "offline_mock" if mode == "mock" else "image_gen"
            if evidence.get("generated_by") != producer or evidence.get("prompt_sha256") != digest_file(prompt):
                raise PipelineError("Image generation provenance does not match this job")
            if not file.is_file() or digest_file(file) != evidence.get("sha256"):
                raise PipelineError("Imported image bytes do not match the recorded tool artifact")
            done(name, file)
        elif name in {"video", "lipsync"}:
            model = H3_MODEL if name == "video" else KLING_MODEL
            fal = services.fal(model) if services else Fal(model, job.get("media_expiration_seconds", 3600))
            file = assets / ("h3-raw.mp4" if name == "video" else "lipsync.mp4")
            if stage["status"] == "pending":
                # Reserve before uploading or submitting. An upload-only failure
                # is a confirmed unsubmitted inference attempt and is safe to retry.
                attempt = run.claim(name)
                try:
                    payload = make_payload(name, fal, assets, job, job_file)
                    if name == "video":
                        (inputs / "video-generation-prompt.txt").write_text(payload["prompt"])
                except Exception:
                    run.fail(attempt, acceptance_unknown=False, actual_usd="0")
                    raise PipelineError("Input preflight or upload failed before inference submission") from None
                live_called = mode == "live"
                try:
                    request_id = fal.submit(payload)
                    run.submitted(attempt, request_id)
                except PipelineError:
                    run.fail(attempt, acceptance_unknown=True)
                    return blocked(f"{name} submission outcome unknown; reconcile before any retry")
            else:
                saved = [a for a in run.status()["attempts"] if a["stage"] == name][-1]
                attempt, request_id = saved["id"], saved["request_id"]
            remote = fal.status(request_id)
            if remote["failed"]:
                run.fail(attempt, acceptance_unknown=False)
                return blocked(f"{name} provider failed; its reserved cost is retained until billing is known")
            if remote["status"] != "COMPLETED":
                return blocked(f"{name} is {remote['status']}; run resume to poll this saved request")
            fal.download_result(request_id, file)
            done(name, file, attempt, paid=True)
            actual = probe(file)["duration_seconds"]
            if actual > 10:
                return blocked("Actual output exceeds 10 seconds. No clip truncation or lip-sync submission was performed")
        elif name == "upscale":
            if probe(assets / "h3-raw.mp4")["duration_seconds"] > 10:
                return blocked("H3 output exceeds 10 seconds; resolve before continuing")
            file = assets / "portrait.mp4"
            report = upscale_portrait(assets / "h3-raw.mp4", file)
            write_json(assets / "portrait-verification.json", report)
            done(name, file)
        elif name == "edit":
            if probe(assets / "lipsync.mp4")["duration_seconds"] > 10:
                return blocked("Lip-sync output exceeds the approved 10 seconds; resolve before rendering")
            caption_file = inputs / "captions.json"
            if not caption_file.exists():
                if job.get("whisper_model"):
                    alignment = align_captions(assets / "voice.mp3", selected["text"],
                                              resolve_input(job_file, job["whisper_model"]), caption_file,
                                              whisper_cli=job.get("whisper_cli", "whisper-cli"),
                                              dtw_model=job.get("whisper_dtw_model", "small"))
                    if alignment["status"] != "aligned":
                        return blocked("Local speech alignment needs review; inspect inputs/caption-alignment.json before rendering")
                else:
                    write_json(inputs / "captions-request.json", {"audio": "artifacts/voice.mp3",
                        "selected_dialogue": selected["text"], "instructions":
                        "Codex: run local align-captions or review measured speech timestamps and save captions.json. Do not ask the user to create routine files."})
                    return blocked("Codex should align captions or set an existing local whisper_model")
            if not job.get("bgm_file") or not resolve_input(job_file, job["bgm_file"]).is_file():
                return blocked("Set bgm_file to a local music file with verified usage permission")
            file = assets / "final.mp4"
            report = render(assets / "lipsync.mp4", resolve_input(job_file, job["bgm_file"]),
                            json.loads(caption_file.read_text()), file, job.get("render_settings"))
            write_json(assets / "final-verification.json", report)
            done(name, file)
        else:
            file = inputs / "description.md"
            if not file.exists():
                (inputs / "description-request.md").write_text(
                    "最後のdescription工程としてInstagram投稿キャプションを作成してください。\n"
                    "references/instagram-caption.md のオリジナル文体例を読み、"
                    "完成台本とartifacts/source.mdの確認済み事実を使う。\n"
                    "利用者の投稿先・語調・必要な表記を優先し、短い改行と少量の絵文字・タグを使う。"
                    "記事にない医学的断定や制作工程の説明を加えない。\n"
                    "本文だけをinputs/description.md、使用プロンプトと検査記録をinputsへ保存。"
                    "完成動画とキャプション全文を一緒に提示する。投稿処理はありません。\n")
                return blocked("Create the source-grounded inputs/description.md with Codex")
            done(name, file)
        # Each invocation produces or polls one stage, then returns control to Codex.
        return review_required(name)
    status = run.status()
    verification = check_vertical_output(assets / "final.mp4")
    if verification["duration_seconds"] > 10:
        raise PipelineError("Final output exceeds the approved 10-second limit")
    return {"status": "draft_complete" if job.get("production_mode") == "private_draft" else "complete", "run": str(run.directory),
            "final_video": str(assets / "final.mp4"), "mode": mode,
            "media_check": verification,
            "charged_or_reserved_usd": status["charged_or_reserved_usd"],
            "confirmed_actual_usd": status["confirmed_actual_usd"],
            "live_generation_verified": mode == "live" and status["materials_complete"],
            "perceptual_quality_verified": status["perceptual_quality_verified"], "public_ready": False if job.get("production_mode") == "private_draft" else None}


def revise_job(job_file: Path, run_path: Path, summary: str, *, extra_roots=()) -> dict:
    """Apply observed input changes in-place; preserve paid attempts and reusable assets."""
    job, _, errors = load_job(job_file)
    if errors:
        raise PipelineError("Resolve job content blockers before revising the run")
    run = Run(run_path)
    new = build_spec(job_file, job)
    if run.status()["mode"] == "mock":
        new = {**new, "budget_usd": "0", "stages": [{**s, "max_cost_usd": "0"} for s in new["stages"]]}
    with run.transaction() as db:
        old = json.loads(db.execute("SELECT spec_json FROM run_options").fetchone()[0])
    fields = {
        "source": {"article_url", "article_file"},
        "dialogue": {"dialogue_candidates_file", "selected_candidate", "selection_mode", "selection_reason", "laugh_suffix_enabled"},
        "audio": {"voice_id", "fish_model", "fish_speed"}, "image": {"image_prompt_file", "reference_files"},
        "video": {"video_prompt_file", "video_duration_seconds", "video_resolution"},
        "lipsync": {"lipsync_model"},
        "edit": {"bgm_file", "render_settings", "whisper_model", "whisper_cli", "whisper_dtw_model"},
    }
    configuration = {"procedure_confirmed", "source_facts_reviewed", "reference_pixels_reviewed", "voice_confirmed",
        "voice_license_confirmed", "fish_data_policy_confirmed", "voice_candidate_id", "voice_candidate_url",
        "budget_usd", "cost_caps_usd", "pricing_verified", "billing_approved", "media_policy", "media_policy_confirmed",
        "media_expiration_seconds", "notes", "review_required", "auto_approve", "review_policy", "production_mode", "private_draft_authorized",
        "timing_revision_authorized", "standing_spending_authorized"}
    changed = {k for k in set(old["job"]) | set(job) if old["job"].get(k) != job.get(k)}
    unknown = changed - set().union(*fields.values()) - configuration
    if unknown:
        raise PipelineError("Unmapped job changes need an explicit implementation review before reuse")
    hashes_changed = {k for k in set(old["input_hashes"]) | set(new["input_hashes"])
                      if old["input_hashes"].get(k) != new["input_hashes"].get(k)}
    roots = set(extra_roots) | {stage for stage, keys in fields.items() if keys & (changed | hashes_changed)}
    # A script timing revision already invalidates audio. Keep its root as
    # dialogue when only the generation speed changes alongside the wording,
    # so the existing same-run script authorization remains applicable.
    if (summary.startswith("尺調整:") and {"dialogue", "audio"} <= roots
            and changed & fields["audio"] == {"fish_speed"}):
        roots.remove("audio")
    return run.invalidate([id_ for id_, _ in STAGES if id_ in roots], summary, new_spec=new)


def make_payload(name, fal, assets, job, job_file):
    if name == "video":
        audio = probe(assets / "voice.mp3")
        if not 2 <= audio["duration_seconds"] <= job["video_duration_seconds"]:
            raise PipelineError("Audio does not fit the planned clip")
        evidence = json.loads((assets / "image-provenance.json").read_text())
        image = assets / evidence["file"]
        image_dimensions(image)
        content_type = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}[image.suffix]
        image_url = fal.upload(image, content_type)
        prompt = resolve_input(job_file, job["video_prompt_file"]).read_text().rstrip()
        prompt += "\n\n" + VIDEO_SMILE_DIRECTION
        return {"image_url": image_url, "prompt": prompt,
                "prompt_expansion_mode": "disabled", "duration": job["video_duration_seconds"],
                "resolution": "480P", "enable_safety_checker": True}
    preflight_lipsync(assets / "portrait.mp4", assets / "voice.mp3", KLING_CONTRACT)
    return {"video_url": fal.upload(assets / "portrait.mp4", "video/mp4"),
            "audio_url": fal.upload(assets / "voice.mp3", "audio/mpeg")}

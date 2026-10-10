"""Agent decisions and artifact lineage. Records observations, never private reasoning."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .state import PipelineError, digest_file, fingerprint, micros

DEPENDENCIES = {
    "source": [], "dialogue": ["source"], "audio": ["dialogue"],
    "image": ["source"], "video": ["image"], "upscale": ["video"],
    "lipsync": ["audio", "upscale"], "edit": ["audio", "lipsync"],
    "description": ["source", "dialogue"],
}
REQUIRED_CHECKS = {
    "source": {"text_read"}, "dialogue": {"text_read"}, "audio": {"audio_listened"},
    "image": {"image_viewed"}, "video": {"video_watched_full"},
    "upscale": {"technical_checked"},
    "lipsync": {"audio_listened", "video_watched_full"},
    "edit": {"text_read", "audio_listened", "video_watched_full"},
    "description": {"text_read"},
}
SCRIPT_ONLY_CHECKS = {
    **REQUIRED_CHECKS,
    "audio": {"technical_checked"},
    "video": {"technical_checked", "image_viewed"},
    "lipsync": {"technical_checked", "image_viewed"},
    "edit": {"text_read", "technical_checked", "image_viewed"},
}
PERCEPTUAL_STAGES = {"audio", "video", "lipsync", "edit"}
DRAFT_REQUIRED_CHECKS = {"audio": {"text_read", "technical_checked"}, "video": {"continuous_frames_viewed", "technical_checked"}, "lipsync": {"continuous_frames_viewed", "technical_checked"}, "edit": {"text_read", "continuous_frames_viewed", "technical_checked"}}
CHOICES = {"next", "draft_usable", "revise", "regenerate", "go_back", "needs_human"}
COMPANIONS = {"dialogue": ["dialogue-candidates.json"], "image": ["image-provenance.json"], "upscale": ["portrait-verification.json"],
              "edit": ["final-verification.json", "render-props.json"]}


def required_checks(stage, policy="strict"):
    requirements = SCRIPT_ONLY_CHECKS if policy == "script_only" else REQUIRED_CHECKS
    return requirements.get(stage, {"technical_checked"})


def review_policy(db):
    spec = json.loads(db.execute("SELECT spec_json FROM run_options WHERE id=1").fetchone()[0])
    return spec.get("job", {}).get("review_policy", "strict")


def timing_revision_allowed(db, revision, directory):
    """Inherit only this run's original script approval for recorded timing edits."""
    if db is None or directory is None or revision < 1:
        return False
    spec = json.loads(db.execute("SELECT spec_json FROM run_options WHERE id=1").fetchone()[0])
    if spec.get("job", {}).get("timing_revision_authorized") is not True:
        return False
    source = db.execute("SELECT s.*,m.revision FROM stages s JOIN stage_meta m ON m.stage=s.id WHERE s.id='source'").fetchone()
    if not source or source["revision"] != 0 or source["status"] != "complete":
        return False
    file = directory / source["artifact"]
    if not file.is_file() or digest_file(file) != source["artifact_hash"]:
        return False
    original = db.execute("""SELECT h.artifact,h.artifact_hash FROM decisions d JOIN artifact_history h
        ON h.stage=d.stage AND h.revision=d.revision AND h.artifact_hash=d.artifact_hash
        WHERE d.stage='dialogue' AND d.revision=0 AND d.choice='next' AND d.actor='human'
        ORDER BY d.id DESC LIMIT 1""").fetchone()
    if not original:
        return False
    file = directory / original["artifact"]
    if not file.is_file() or digest_file(file) != original["artifact_hash"]:
        return False
    edits = [row for row in db.execute("SELECT roots_json,affected_json,summary FROM invalidations ORDER BY id")
             if "dialogue" in json.loads(row["affected_json"])]
    return len(edits) == revision and all(
        json.loads(row["roots_json"]) == ["dialogue"] and row["summary"].startswith("尺調整:")
        for row in edits)


def can_proceed(stage, evidence, actor, policy, *, db=None, revision=0, directory=None):
    if policy == "script_only" and stage == "dialogue" and actor != "human":
        if actor != "codex_agent" or not timing_revision_allowed(db, revision, directory):
            return False
    if not required_checks(stage, policy) <= set(evidence.get("checks", [])):
        return False
    return not evidence.get("unverified") or (policy == "script_only" and stage in PERCEPTUAL_STAGES)


def descendants(roots, stages):
    affected = set(roots)
    if affected - set(stages):
        raise PipelineError("Revision must name existing stages")
    while True:
        added = {stage for stage in stages if set(DEPENDENCIES.get(stage, [])) & affected} - affected
        if not added:
            return [stage for stage in stages if stage in affected]
        affected |= added


def draft_authorized(db):
    row = db.execute("SELECT spec_json FROM run_options").fetchone()
    job = json.loads(row[0]).get("job", {}) if row else {}
    return job.get("production_mode") == "private_draft" and job.get("private_draft_authorized") is True


def valid_decision(db, stage, artifact_hash, revision, directory, choice):
    latest = db.execute("SELECT choice,evidence_json,actor FROM decisions WHERE stage=? AND revision=? AND artifact_hash=? ORDER BY id DESC LIMIT 1",
                        (stage, revision, artifact_hash)).fetchone()
    if not latest or latest[0] != choice:
        return False
    payload = json.loads(latest[1])
    if choice == "next" and not can_proceed(stage, payload, latest[2], review_policy(db),
                                           db=db, revision=revision, directory=directory):
        return False
    if directory:
        for evidence in payload["evidence"]:
            file = directory / evidence["file"]
            if not file.is_file() or digest_file(file) != evidence["sha256"]:
                return False
    return True


def approved(db, stage, artifact_hash, revision, directory=None):
    return valid_decision(db, stage, artifact_hash, revision, directory, "next")


def draft_usable(db, stage, artifact_hash, revision, directory=None):
    return draft_authorized(db) and valid_decision(db, stage, artifact_hash, revision, directory, "draft_usable")


def progress_allowed(db, stage, artifact_hash, revision, directory=None):
    return approved(db, stage, artifact_hash, revision, directory) or draft_usable(db, stage, artifact_hash, revision, directory)


def decision(run, stage, choice, evidence, *, actor="codex_agent", target=None):
    if choice not in CHOICES or actor not in {"codex_agent", "human", "mock_test"}:
        raise PipelineError("Unsupported decision or actor")
    if not isinstance(evidence, dict) or set(evidence) - {"summary", "checks", "evidence_files", "unverified"}:
        raise PipelineError("Supply only a concise observation summary, checks, evidence files and unverified items")
    summary = evidence.get("summary", "")
    checks = evidence.get("checks", [])
    unverified = evidence.get("unverified", [])
    files = evidence.get("evidence_files", [])
    if not isinstance(summary, str) or not 1 <= len(summary) <= 1000 or not isinstance(checks, list) or not all(
        isinstance(x, str) and x in set().union(*REQUIRED_CHECKS.values(), *DRAFT_REQUIRED_CHECKS.values()) for x in checks):
        raise PipelineError("Decision needs a concise observation and supported completed checks")
    if not isinstance(unverified, list) or not all(isinstance(x, str) and len(x) <= 200 for x in unverified):
        raise PipelineError("Unverified items must be short descriptions")
    if not isinstance(files, list) or not files or len(files) > 10:
        raise PipelineError("Attach local evidence files for the observation")
    references = []
    for value in files:
        if not isinstance(value, str):
            raise PipelineError("Evidence references must be local run paths")
        file = (run.directory / value).resolve()
        try:
            relative = file.relative_to(run.directory)
        except ValueError:
            raise PipelineError("Evidence must stay inside the run") from None
        if not file.is_file() or any(part.startswith((".credentials", ".private-library")) for part in relative.parts) or file.name.startswith(".env"):
            raise PipelineError("Evidence is unavailable or private")
        references.append({"file": relative.as_posix(), "sha256": digest_file(file)})
    with run.transaction() as db:
        if actor == "mock_test" and db.execute("SELECT mode FROM run").fetchone()[0] != "mock":
            raise PipelineError("Mock review fixtures cannot approve live production")
        row = db.execute("SELECT s.*,m.revision FROM stages s JOIN stage_meta m ON m.stage=s.id WHERE s.id=?", (stage,)).fetchone()
        if not row or row["status"] != "complete":
            raise PipelineError("Review requires a produced stage artifact")
        artifact = run.directory / row["artifact"]
        if not artifact.is_file() or digest_file(artifact) != row["artifact_hash"]:
            raise PipelineError("Review artifact changed; revise its lineage first")
        if choice == "next" and not can_proceed(stage, evidence, actor, review_policy(db),
                                               db=db, revision=row["revision"], directory=run.directory):
            raise PipelineError("Required checks or script approval are missing for the configured review policy")
        if choice == "draft_usable" and (not draft_authorized(db) or stage not in DRAFT_REQUIRED_CHECKS or not DRAFT_REQUIRED_CHECKS[stage] <= set(checks) or not unverified):
            raise PipelineError("Private draft use requires explicit authorization, observed technical/visual checks and retained unverified items")
        if choice == "go_back":
            target_row = db.execute("SELECT ordinal FROM stages WHERE id=?", (target,)).fetchone()
            if not target_row or target_row[0] >= row["ordinal"]:
                raise PipelineError("Go back requires an earlier target stage")
        elif target is not None:
            raise PipelineError("Only go_back uses a target stage")
        payload = {"summary": summary, "checks": checks, "evidence": references, "unverified": unverified}
        if choice == "draft_usable":
            payload.update(draft_usable=True, formal_quality_approved=False, listening_unverified="audio_listened" not in checks, full_playback_unverified="video_watched_full" not in checks)
        cursor = db.execute("INSERT INTO decisions(stage,revision,artifact_hash,choice,actor,target,evidence_json) VALUES(?,?,?,?,?,?,?)",
                            (stage, row["revision"], row["artifact_hash"], choice, actor, target, json.dumps(payload, ensure_ascii=False)))
    return {"decision_id": cursor.lastrowid, "stage": stage, "choice": choice, "target": target, "live_api_called": False}


def invalidate(run, roots, summary, *, new_spec=None):
    if not isinstance(summary, str) or not 1 <= len(summary) <= 1000:
        raise PipelineError("Revision needs a concise observable reason")
    moved = []
    if not roots and new_spec is None:
        raise PipelineError("Revision must name an affected stage")
    try:
        with run.transaction() as db:
            stages = [dict(s) for s in db.execute("SELECT s.*,m.revision FROM stages s JOIN stage_meta m ON m.stage=s.id ORDER BY ordinal")]
            affected = descendants(roots, [s["id"] for s in stages])
            if any(s["id"] in affected and s["status"] in {"submitting", "submitted", "needs_reconciliation"} for s in stages):
                raise PipelineError("Reconcile every in-flight affected request before revising; reservations are retained")
            if new_spec is not None:
                current = json.loads(db.execute("SELECT spec_json FROM run_options").fetchone()[0])
                if micros(new_spec["budget_usd"]) != db.execute("SELECT budget FROM run").fetchone()[0]:
                    raise PipelineError("Revision cannot reset or increase the approved budget")
                if new_spec.get("budget_ledger_path") != current.get("budget_ledger_path"):
                    raise PipelineError("Revision cannot switch the shared spending ledger")
                if new_spec.get("shared_budget_usd") != current.get("shared_budget_usd"):
                    raise PipelineError("Revision cannot change total spending authorization")
                if not new_spec.get("review_required", True):
                    raise PipelineError("Agent review cannot be disabled")
                if [s["id"] for s in new_spec["stages"]] != [s["id"] for s in stages]:
                    raise PipelineError("Revision cannot change the stage topology")
            for stage in stages:
                if stage["id"] not in affected:
                    continue
                source = run.directory / stage["artifact"] if stage.get("artifact") else None
                if source:
                    if not source.is_file() or digest_file(source) != stage["artifact_hash"]:
                        raise PipelineError("Restore the expected old artifact before archiving a revision")
                    archive = run.directory / "history" / stage["id"] / f"r{stage['revision']}"
                    archive.mkdir(parents=True, exist_ok=True)
                    paths = [source] + [source.parent / name for name in COMPANIONS.get(stage["id"], [])]
                    for item in paths:
                        if not item.exists():
                            continue
                        destination = archive / item.name
                        if destination.exists():
                            raise PipelineError("Revision history already exists; restore the interrupted revision first")
                        shutil.move(item, destination)
                        moved.append((item, destination))
                    db.execute("INSERT INTO artifact_history(stage,revision,artifact,artifact_hash) VALUES(?,?,?,?)",
                               (stage["id"], stage["revision"], (archive / source.name).relative_to(run.directory).as_posix(), stage["artifact_hash"]))
                elif stage["id"] == "image":
                    # image_gen can be imported before its stage is committed. Never
                    # retain such pre-imported bytes after changing their ancestors.
                    archive = run.directory / "history" / "image" / f"r{stage['revision']}"
                    for item in (run.directory / "artifacts").glob("image.*"):
                        archive.mkdir(parents=True, exist_ok=True)
                        destination = archive / item.name
                        if destination.exists():
                            raise PipelineError("Untracked image revision history collision")
                        shutil.move(item, destination)
                        moved.append((item, destination))
                    item = run.directory / "artifacts/image-provenance.json"
                    if item.exists():
                        archive.mkdir(parents=True, exist_ok=True)
                        destination = archive / item.name
                        if destination.exists():
                            raise PipelineError("Untracked image provenance history collision")
                        shutil.move(item, destination)
                        moved.append((item, destination))
                db.execute("UPDATE stages SET status='pending',artifact=NULL,artifact_hash=NULL WHERE id=?", (stage["id"],))
                db.execute("UPDATE stage_meta SET revision=revision+1 WHERE stage=?", (stage["id"],))
            if "audio" in affected:
                for name in ("captions.json", "caption-alignment.json", "captions-request.json",
                             "fish-tts-request.json", "speech-duration.json"):
                    item = run.directory / "inputs" / name
                    if item.exists():
                        group = "audio-inputs" if name in {"fish-tts-request.json", "speech-duration.json"} else "caption-inputs"
                        archive = run.directory / "history" / group / f"revision-{db.execute('SELECT COUNT(*) FROM invalidations').fetchone()[0]}"
                        archive.mkdir(parents=True, exist_ok=True)
                        destination = archive / name
                        if destination.exists():
                            raise PipelineError("Audio or caption history collision")
                        shutil.move(item, destination)
                        moved.append((item, destination))
            db.execute("INSERT INTO invalidations(roots_json,affected_json,summary) VALUES(?,?,?)",
                       (json.dumps(roots), json.dumps(affected), summary))
            if new_spec is not None:
                db.execute("UPDATE run SET fingerprint=?", (fingerprint(new_spec),))
                db.execute("UPDATE run_options SET spec_json=?", (json.dumps(new_spec, ensure_ascii=False),))
                for s in new_spec["stages"]:
                    db.execute("UPDATE stages SET provider=?,max_cost=? WHERE id=?", (s["provider"], micros(s["max_cost_usd"]), s["id"]))
    except BaseException:
        for original, archive in reversed(moved):
            if archive.exists() and not original.exists():
                shutil.move(archive, original)
        raise
    return {"invalidated": affected, "retained": [s["id"] for s in stages if s["id"] not in affected],
            "spending_retained": True, "live_api_called": False}

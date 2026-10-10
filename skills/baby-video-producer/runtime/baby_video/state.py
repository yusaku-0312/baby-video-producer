"""Durable stage state and conservative USD budget accounting (stdlib only).

A submission that times out, or a process that dies during submission, cannot
be submitted again automatically. Reconcile it with its provider first.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager, closing
from decimal import Decimal, InvalidOperation
from pathlib import Path


class PipelineError(Exception):
    """A safe message that contains no provider response or credentials."""


def micros(value: str | int | float) -> int:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise PipelineError("Invalid USD amount") from None
    if not amount.is_finite() or amount < 0:
        raise PipelineError("USD amount must be finite and nonnegative")
    units = amount * 1_000_000
    if units != units.to_integral_value():
        raise PipelineError("USD precision is limited to six decimal places")
    return int(units)


def usd(value: int) -> str:
    return str(Decimal(value) / 1_000_000)


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
  id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL,
  mode TEXT NOT NULL, budget INTEGER NOT NULL CHECK(budget>=0)
);
CREATE TABLE IF NOT EXISTS stages (
  id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL UNIQUE,
  provider TEXT NOT NULL, max_cost INTEGER NOT NULL CHECK(max_cost>=0),
  status TEXT NOT NULL DEFAULT 'pending', artifact TEXT, artifact_hash TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY, stage TEXT NOT NULL REFERENCES stages(id),
  reserved INTEGER NOT NULL CHECK(reserved>=0), actual INTEGER,
  state TEXT NOT NULL, request_id TEXT, error_code TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS run_options (
  id INTEGER PRIMARY KEY CHECK(id=1), spec_json TEXT NOT NULL,
  review_required INTEGER NOT NULL DEFAULT 1, budget_pool_path TEXT
);
CREATE TABLE IF NOT EXISTS stage_meta (stage TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, stage TEXT NOT NULL, revision INTEGER NOT NULL,
  artifact_hash TEXT NOT NULL, choice TEXT NOT NULL, actor TEXT NOT NULL,
  target TEXT, evidence_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS artifact_history (
  id INTEGER PRIMARY KEY, stage TEXT NOT NULL, revision INTEGER NOT NULL,
  artifact TEXT NOT NULL, artifact_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invalidations (
  id INTEGER PRIMARY KEY, roots_json TEXT NOT NULL, affected_json TEXT NOT NULL,
  summary TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


class Run:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.path = self.directory / "state.sqlite3"
        if not self.path.is_file():
            raise PipelineError("Run is not initialized")

    @classmethod
    def create(cls, directory: Path, spec: dict, mode: str = "mock") -> "Run":
        if mode not in {"mock", "live"}:
            raise PipelineError("Unknown execution mode")
        stages = spec.get("stages", [])
        ids = [stage.get("id", "") for stage in stages]
        if not ids or len(set(ids)) != len(ids) or any(
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", x) for x in ids
        ):
            raise PipelineError("Stage IDs must be unique simple identifiers")
        budget = micros(spec["budget_usd"])
        costs = [micros(s["max_cost_usd"]) for s in stages]
        if mode == "live" and not spec.get("procedure_confirmed"):
            raise PipelineError("Production workflow must be confirmed before live setup")
        if not spec.get("review_required", True):
            raise PipelineError("Explicit agent review cannot be disabled")
        standing_spending = spec.get("job", {}).get("standing_spending_authorized") is True
        if mode == "live" and not standing_spending and budget > micros("2.00"):
            raise PipelineError("Approved total is USD 2.00; a larger limit requires owner approval")
        if mode == "live" and not spec.get("budget_ledger_path"):
            raise PipelineError("Live runs require the shared spending ledger")
        if mode == "live" and sum(costs) > budget:
            raise PipelineError("Plan exceeds the approved budget")
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "state.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint,mode,budget FROM run").fetchone()
            expected = (fingerprint(spec), mode, budget)
            if row and row != expected:
                raise PipelineError("Run inputs or mode changed; revise the existing lineage before resuming")
            if not row:
                db.execute("INSERT INTO run VALUES(1,?,?,?)", expected)
                db.executemany(
                    "INSERT INTO stages(id,ordinal,provider,max_cost) VALUES(?,?,?,?)",
                    [(s["id"], i, s["provider"], costs[i]) for i, s in enumerate(stages)],
                )
            db.execute("INSERT OR IGNORE INTO run_options VALUES(1,?,?,?)",
                       (json.dumps(spec, ensure_ascii=False), bool(spec.get("review_required", True)),
                        spec.get("budget_ledger_path") if mode == "live" else None))
            db.executemany("INSERT OR IGNORE INTO stage_meta(stage) VALUES(?)", [(id_,) for id_ in ids])
        if mode == "live" and spec.get("budget_ledger_path"):
            initialize_pool(Path(spec["budget_ledger_path"]), spec.get("shared_budget_usd", spec["budget_usd"]),
                            run_path=path, standing_budget_usd=spec["budget_usd"] if standing_spending else None)
        return cls(directory)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            options_exist = db.execute("SELECT 1 FROM sqlite_master WHERE name='run_options'").fetchone()
            options = db.execute("SELECT budget_pool_path FROM run_options").fetchone() if options_exist else None
            if options and options[0]:
                db.execute("ATTACH DATABASE ? AS spending", (options[0],))
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def claim(self, stage: str) -> int:
        """Reserve before calling a provider; one claim succeeds across processes."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM stages WHERE id=?", (stage,)).fetchone()
            if not row or row["status"] != "pending":
                raise PipelineError("Stage is not pending; resume or reconcile the existing attempt")
            unfinished = db.execute(
                "SELECT id FROM stages WHERE ordinal<? AND status!='complete'", (row["ordinal"],)
            ).fetchone()
            if unfinished:
                raise PipelineError("Previous stage is not complete")
            for previous in db.execute(
                "SELECT artifact,artifact_hash FROM stages WHERE ordinal<?", (row["ordinal"],)
            ):
                artifact = self.directory / previous["artifact"]
                if not artifact.is_file() or digest_file(artifact) != previous["artifact_hash"]:
                    raise PipelineError("Previous stage artifact changed; create or repair the run before continuing")
            from .review import approved, draft_usable, progress_allowed
            review_required = db.execute("SELECT review_required FROM run_options").fetchone()[0]
            if review_required:
                for previous in db.execute("SELECT s.id,s.artifact_hash,m.revision FROM stages s JOIN stage_meta m ON m.stage=s.id WHERE ordinal<?", (row["ordinal"],)):
                    if not progress_allowed(db, previous["id"], previous["artifact_hash"], previous["revision"], self.directory):
                        raise PipelineError("Previous material needs an explicit agent decision based on actual observation")
            held = db.execute("SELECT COALESCE(SUM(reserved),0) FROM attempts").fetchone()[0]
            budget = db.execute("SELECT budget FROM run").fetchone()[0]
            if held + row["max_cost"] > budget:
                raise PipelineError("Budget exhausted; no provider call was made")
            if self._pool_attached(db):
                shared_held = db.execute("SELECT COALESCE(SUM(reserved),0) FROM spending.reservations").fetchone()[0]
                shared_limit = db.execute("SELECT budget FROM spending.limits").fetchone()[0]
                if shared_held + row["max_cost"] > shared_limit:
                    raise PipelineError("Shared approved budget exhausted; ask the owner before additional spending")
                # The standing authorization adds funds for new routine runs;
                # it never expands the historical test's authorization.
                allocation_table = db.execute("SELECT 1 FROM spending.sqlite_master WHERE name='allocations'").fetchone()
                if allocation_table and not db.execute("SELECT 1 FROM spending.allocations WHERE run_path=?",
                                                        (str(self.path),)).fetchone():
                    legacy_held = db.execute("""SELECT COALESCE(SUM(r.reserved),0) FROM spending.reservations r
                        WHERE NOT EXISTS(SELECT 1 FROM spending.allocations a WHERE a.run_path=r.run_path)""").fetchone()[0]
                    legacy_limit = db.execute("SELECT budget FROM spending.base_limit").fetchone()[0]
                    if legacy_held + row["max_cost"] > legacy_limit:
                        raise PipelineError("Historical test budget exhausted; no provider call was made")
            cursor = db.execute(
                "INSERT INTO attempts(stage,reserved,state) VALUES(?,?,'submitting')",
                (stage, row["max_cost"]),
            )
            db.execute("UPDATE stages SET status='submitting' WHERE id=?", (stage,))
            if self._pool_attached(db):
                db.execute("INSERT INTO spending.reservations VALUES(?,?,?,NULL)",
                           (str(self.path), cursor.lastrowid, row["max_cost"]))
            return cursor.lastrowid

    def submitted(self, attempt: int, request_id: str) -> None:
        # Never store signed asset URLs, headers or an arbitrary provider payload.
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", request_id):
            raise PipelineError("Provider request ID is not a safe identifier")
        with self.transaction() as db:
            row = self._attempt(db, attempt)
            if row["state"] not in {"submitting", "needs_reconciliation"}:
                raise PipelineError("Attempt is not awaiting submission acknowledgement")
            db.execute("UPDATE attempts SET state='submitted',error_code=NULL,request_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (request_id, attempt))
            db.execute("UPDATE stages SET status='submitted' WHERE id=?", (row["stage"],))

    @staticmethod
    def _attempt(db, attempt):
        row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
        if not row:
            raise PipelineError("Attempt not found")
        return row

    @staticmethod
    def _pool_attached(db):
        return any(row[1] == "spending" for row in db.execute("PRAGMA database_list"))

    def _pool_update(self, db, attempt, reserved, actual):
        if self._pool_attached(db):
            db.execute("UPDATE spending.reservations SET reserved=?,actual=? WHERE run_path=? AND attempt=?",
                       (reserved, actual, str(self.path), attempt))

    def complete(self, attempt: int, artifact: Path, actual_usd=None) -> None:
        artifact = artifact.resolve()
        try:
            relative = artifact.relative_to(self.directory)
        except ValueError:
            raise PipelineError("Copy the artifact inside the run directory first") from None
        if not artifact.is_file() or not artifact.stat().st_size:
            raise PipelineError("Artifact is missing or empty")
        checksum = digest_file(artifact)
        actual = None if actual_usd is None else micros(actual_usd)
        with self.transaction() as db:
            row = self._attempt(db, attempt)
            if row["state"] not in {"submitting", "submitted", "needs_reconciliation"}:
                raise PipelineError("Attempt cannot be completed")
            # Account for actual overspend too. Future claims stop at the budget.
            reserved = row["reserved"] if actual is None else actual
            db.execute("UPDATE attempts SET state='complete',actual=?,reserved=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (actual, reserved, attempt))
            db.execute("UPDATE stages SET status='complete',artifact=?,artifact_hash=? WHERE id=?",
                       (str(relative), checksum, row["stage"]))
            self._pool_update(db, attempt, reserved, actual)

    def fail(self, attempt: int, *, acceptance_unknown: bool, actual_usd=None) -> None:
        """Unknown acceptance holds funds. Confirmed failures retain known cost."""
        actual = None if actual_usd is None else micros(actual_usd)
        with self.transaction() as db:
            row = self._attempt(db, attempt)
            if row["state"] not in {"submitting", "submitted", "needs_reconciliation"}:
                raise PipelineError("Attempt is already terminal")
            state = "needs_reconciliation" if acceptance_unknown else "failed"
            # None means billing is also unknown: keep the upper-bound reserve.
            reserved = row["reserved"] if actual is None or acceptance_unknown else actual
            db.execute("UPDATE attempts SET state=?,error_code=?,actual=?,reserved=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (state, "acceptance_unknown" if acceptance_unknown else "provider_failed",
                        actual, reserved, attempt))
            db.execute("UPDATE stages SET status=? WHERE id=?", (state, row["stage"]))
            self._pool_update(db, attempt, reserved, actual)

    def retry_confirmed_failure(self, stage: str) -> None:
        """Explicit retry only; previous paid attempts remain in the budget."""
        with self.transaction() as db:
            row = db.execute("SELECT status FROM stages WHERE id=?", (stage,)).fetchone()
            if not row or row[0] != "failed":
                raise PipelineError("Retry requires a confirmed terminal provider failure")
            db.execute("UPDATE stages SET status='pending' WHERE id=?", (stage,))

    def settle_cost(self, attempt: int, actual_usd) -> None:
        """Record provider-confirmed billing for a terminal attempt, never infer it."""
        actual = micros(actual_usd)
        with self.transaction() as db:
            row = self._attempt(db, attempt)
            if row["state"] not in {"complete", "failed"}:
                raise PipelineError("Billing settlement requires a terminal attempt")
            db.execute("UPDATE attempts SET actual=?,reserved=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (actual, actual, attempt))
            self._pool_update(db, attempt, actual, actual)

    def decide(self, stage, choice, evidence, *, actor="codex_agent", target=None):
        from .review import decision
        return decision(self, stage, choice, evidence, actor=actor, target=target)

    def invalidate(self, roots, summary, *, new_spec=None):
        from .review import invalidate
        return invalidate(self, roots, summary, new_spec=new_spec)

    def status(self) -> dict:
        with self.transaction() as db:
            run = db.execute("SELECT * FROM run").fetchone()
            stages = [dict(r) for r in db.execute("SELECT * FROM stages ORDER BY ordinal")]
            attempts = [dict(r) for r in db.execute("SELECT * FROM attempts ORDER BY id")]
            options_exist = db.execute("SELECT 1 FROM sqlite_master WHERE name='run_options'").fetchone()
            required = db.execute("SELECT review_required FROM run_options").fetchone()[0] if options_exist else True
            decisions = [dict(r) for r in db.execute("SELECT * FROM decisions ORDER BY id")] if options_exist else []
            metadata = dict(db.execute("SELECT stage,revision FROM stage_meta")) if options_exist else {}
            from .review import approved, draft_usable, progress_allowed
            for stage in stages:
                stage["revision"] = metadata.get(stage["id"], 0)
                stage["review_approved"] = approved(db, stage["id"], stage["artifact_hash"], stage["revision"], self.directory) if options_exist else False
                stage["draft_usable"] = draft_usable(db, stage["id"], stage["artifact_hash"], stage["revision"], self.directory) if options_exist else False
                stage["progress_allowed"] = stage["review_approved"] or stage["draft_usable"]
            shared = {}
            if self._pool_attached(db):
                shared_limit = db.execute("SELECT budget FROM spending.limits").fetchone()[0]
                shared_held, shared_actual = db.execute("SELECT COALESCE(SUM(reserved),0),COALESCE(SUM(actual),0) FROM spending.reservations").fetchone()
                shared = {"shared_budget_usd": usd(shared_limit), "shared_charged_or_reserved_usd": usd(shared_held),
                          "shared_confirmed_actual_usd": usd(shared_actual), "shared_remaining_usd": usd(max(0, shared_limit-shared_held))}
        for stage in stages:
            if stage["status"] == "complete":
                p = self.directory / stage["artifact"]
                stage["artifact_valid"] = p.is_file() and digest_file(p) == stage["artifact_hash"]
        held = sum(a["reserved"] for a in attempts)
        actual = sum(a["actual"] or 0 for a in attempts)
        from .review import PERCEPTUAL_STAGES, REQUIRED_CHECKS
        latest = {d["stage"]: json.loads(d["evidence_json"]) for d in decisions}
        media = [s for s in stages if s["id"] in PERCEPTUAL_STAGES]
        perceptual = run["mode"] == "live" and bool(media) and all(
            s["status"] == "complete" and s.get("artifact_valid") and s["review_approved"]
            and REQUIRED_CHECKS[s["id"]] <= set(latest.get(s["id"], {}).get("checks", []))
            and not latest.get(s["id"], {}).get("unverified") for s in media)
        return {
            "mode": run["mode"], "input_fingerprint": run["fingerprint"],
            "budget_usd": usd(run["budget"]), "charged_or_reserved_usd": usd(held),
            "confirmed_actual_usd": usd(actual),
            "remaining_usd": usd(max(0, run["budget"] - held)),
            "budget_exceeded": held > run["budget"],
            "materials_complete": all(s["status"] == "complete" and s.get("artifact_valid") for s in stages),
            "perceptual_quality_verified": perceptual,
            "complete": all(s["status"] == "complete" and s.get("artifact_valid") and s["review_approved"] for s in stages),
            "draft_complete": all(s["status"] == "complete" and s.get("artifact_valid") and s["progress_allowed"] for s in stages),
            "review_required": bool(required), "decisions": decisions, **shared,
            "stages": stages, "attempts": attempts,
        }


def initialize_pool(path: Path, budget_usd, *, run_path: Path | None = None, standing_budget_usd=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    budget = micros(budget_usd)
    if budget > micros("2.00"):
        raise PipelineError("Shared total authorization cannot exceed USD 2.00")
    allocation = None if standing_budget_usd is None else micros(standing_budget_usd)
    if allocation is not None and run_path is None:
        raise PipelineError("Standing spending allocation requires a run identity")
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("CREATE TABLE IF NOT EXISTS limits(id INTEGER PRIMARY KEY CHECK(id=1),budget INTEGER NOT NULL);"
                         "CREATE TABLE IF NOT EXISTS reservations(run_path TEXT NOT NULL,attempt INTEGER NOT NULL,reserved INTEGER NOT NULL,actual INTEGER,PRIMARY KEY(run_path,attempt));"
                         "CREATE TABLE IF NOT EXISTS base_limit(id INTEGER PRIMARY KEY CHECK(id=1),budget INTEGER NOT NULL);"
                         "CREATE TABLE IF NOT EXISTS allocations(run_path TEXT PRIMARY KEY,budget INTEGER NOT NULL CHECK(budget>=0));")
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR IGNORE INTO limits VALUES(1,?)", (budget,))
        db.execute("INSERT OR IGNORE INTO base_limit SELECT id,budget FROM limits")
        if db.execute("SELECT budget FROM base_limit").fetchone()[0] != budget:
            raise PipelineError("Shared budget cannot be changed without a new owner approval")
        if allocation is not None:
            identity = str(run_path.resolve())
            row = db.execute("SELECT budget FROM allocations WHERE run_path=?", (identity,)).fetchone()
            if row and row[0] != allocation:
                raise PipelineError("A routine run's spending cap cannot change when resuming")
            if not row:
                db.execute("INSERT INTO allocations VALUES(?,?)", (identity, allocation))
                db.execute("UPDATE limits SET budget=budget+? WHERE id=1", (allocation,))

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from baby_video.state import PipelineError, Run, initialize_pool


class RoutineBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = self.root / "budget.sqlite3"

    def spec(self, *, routine=False, budget="0.4", cost="0.24"):
        spec = {"procedure_confirmed": True, "budget_usd": budget,
                "budget_ledger_path": str(self.ledger), "shared_budget_usd": "2",
                "stages": [{"id": "video", "provider": "accounting_fixture", "max_cost_usd": cost}]}
        if routine:
            spec["job"] = {"standing_spending_authorized": True}
        return spec

    def legacy_reservation(self):
        run = Run.create(self.root / "historical", self.spec(budget="2", cost="1.813"), "live")
        run.fail(run.claim("video"), acceptance_unknown=True)
        return run

    def test_routine_run_adds_cap_once_and_preserves_unknown_historical_reservation(self):
        historical = self.legacy_reservation()
        previous = historical.status()["attempts"]
        spec = self.spec(routine=True)
        path = self.root / "routine"
        routine = Run.create(path, spec, "live")
        self.assertEqual(routine.status()["shared_budget_usd"], "2.4")
        routine.claim("video")  # 1.813 + .24 would exceed the old shared limit.
        for _ in range(3):
            resumed = Run.create(path, spec, "live")
            self.assertEqual(resumed.status()["shared_budget_usd"], "2.4")
            with self.assertRaises(PipelineError):
                resumed.claim("video")
        self.assertEqual(historical.status()["attempts"], previous)
        self.assertEqual(historical.status()["charged_or_reserved_usd"], "1.813")
        self.assertEqual(routine.status()["shared_charged_or_reserved_usd"], "2.053")

    def test_next_resume_allocates_once_for_a_run_initialized_before_the_upgrade(self):
        self.legacy_reservation()
        spec = self.spec(routine=True)
        path = self.root / "already-initialized"
        # The prior runtime initialized the ledger but did not allocate routine funds.
        with patch("baby_video.state.initialize_pool",
                   side_effect=lambda ledger, budget, **_: initialize_pool(ledger, budget)):
            run = Run.create(path, spec, "live")
        self.assertEqual(run.status()["shared_budget_usd"], "2")
        resumed = Run.create(path, spec, "live")
        self.assertEqual(resumed.status()["shared_budget_usd"], "2.4")
        resumed.claim("video")
        self.assertEqual(Run.create(path, spec, "live").status()["shared_budget_usd"], "2.4")

    def test_routine_funds_do_not_expand_the_legacy_test_limit(self):
        self.legacy_reservation()
        Run.create(self.root / "routine", self.spec(routine=True), "live")
        old = Run.create(self.root / "legacy-resume", self.spec(budget="2", cost="0.3"), "live")
        with self.assertRaisesRegex(PipelineError, "Historical test budget exhausted"):
            old.claim("video")
        self.assertEqual(old.status()["attempts"], [])
        with self.assertRaises(PipelineError):
            Run.create(self.root / "legacy-inflated", self.spec(budget="2.01"), "live")
        with self.assertRaises(PipelineError):
            initialize_pool(self.ledger, "2.01")

    def test_each_new_routine_run_has_its_own_internal_cap(self):
        first = Run.create(self.root / "first", self.spec(routine=True), "live")
        second = Run.create(self.root / "second", self.spec(routine=True), "live")
        self.assertEqual(second.status()["shared_budget_usd"], "2.8")
        first.fail(first.claim("video"), acceptance_unknown=False, actual_usd="0.2")
        first.retry_confirmed_failure("video")
        with self.assertRaisesRegex(PipelineError, "Budget exhausted"):
            first.claim("video")
        second.claim("video")
        self.assertEqual(first.status()["charged_or_reserved_usd"], "0.2")

    def test_concurrent_allocation_and_repeated_resume_do_not_multiply_the_cap(self):
        initialize_pool(self.ledger, "2")
        identity = self.root / "routine" / "state.sqlite3"
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: initialize_pool(self.ledger, "2", run_path=identity,
                                                   standing_budget_usd="0.4"), range(3)))
        run = Run.create(identity.parent, self.spec(routine=True), "live")
        self.assertEqual(run.status()["shared_budget_usd"], "2.4")
        with self.assertRaises(PipelineError):
            initialize_pool(self.ledger, "2", run_path=identity, standing_budget_usd="0.5")
        self.assertEqual(run.status()["shared_budget_usd"], "2.4")


if __name__ == "__main__":
    unittest.main()

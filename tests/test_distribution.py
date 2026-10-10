"""Verify installing the skill alone is enough to initialize a clean workspace."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skill = self.root / "installed skills/baby-video-producer"
        shutil.copytree(REPO / "skills/baby-video-producer", self.skill,
                        ignore=shutil.ignore_patterns("node_modules", "__pycache__", "runs", ".env.local"))
        self.workspace = self.root / "project with spaces/video workspace"

    def initialize(self):
        return subprocess.run([sys.executable, str(self.skill / "scripts/init-workspace.py"),
                               "--workspace", str(self.workspace)], text=True, capture_output=True)

    def test_installed_skill_initializes_without_private_state_and_runs_cli(self):
        runtime = self.skill / "runtime"
        (runtime / ".env.local").write_text("FAL_KEY=private-fixture-value\n")
        (runtime / "runs").mkdir()
        (runtime / "runs/state.sqlite3").write_bytes(b"private fixture state")
        result = self.initialize()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["network_called"])
        self.assertFalse((self.workspace / ".env.local").exists())
        self.assertFalse((self.workspace / "runs").exists())
        self.assertTrue((self.workspace / "references/instagram-caption.md").is_file())
        self.assertEqual((self.workspace / "LICENSE").read_bytes(), (REPO / "LICENSE").read_bytes())
        doctor = subprocess.run([sys.executable, "-m", "baby_video", "doctor"], cwd=self.workspace,
                                text=True, capture_output=True)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        report = json.loads(doctor.stdout)
        self.assertFalse(report["live_api_called"])
        self.assertFalse(any(report["credential_environment_present"].values()))

    def test_existing_workspace_is_preserved(self):
        self.workspace.mkdir(parents=True)
        sentinel = self.workspace / "keep.txt"
        sentinel.write_text("existing project data")
        result = self.initialize()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(), "existing project data")
        self.assertEqual(list(self.workspace.iterdir()), [sentinel])

    def test_public_mock_fixture_provides_its_own_configuration(self):
        result = self.initialize()
        self.assertEqual(result.returncode, 0, result.stderr)
        code = """
from pathlib import Path
from baby_video.offline import create_fixture, fixture_decision, OfflineServices
from baby_video.pipeline import load_job, resume
from baby_video.state import Run
root = Path('mock-fixture')
create_fixture(root)
job, selected, errors = load_job(root / 'job.json')
assert not errors, errors
assert job['voice_id'].startswith('mock-'), job['voice_id']
assert selected['text']
run = Path('mock-run')
services = OfflineServices(root, run)
for stage in ('source', 'dialogue'):
    result = resume(root / 'job.json', run, mode='mock', services=services)
    assert result['stage'] == stage, result
    fixture_decision(Run(run), stage)
"""
        fixture = subprocess.run([sys.executable, "-c", code], cwd=self.workspace,
                                 text=True, capture_output=True)
        self.assertEqual(fixture.returncode, 0, fixture.stderr)


if __name__ == "__main__":
    unittest.main()

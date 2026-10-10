import json
import tempfile
import unittest
from pathlib import Path

from baby_video.pipeline import STAGES
from baby_video.review import REQUIRED_CHECKS
from baby_video.state import PipelineError, Run


class ScriptOnlyReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = {
            'procedure_confirmed': True, 'review_required': True, 'budget_usd': '2',
            'shared_budget_usd': '2', 'budget_ledger_path': str(self.root / 'budget.sqlite3'),
            'job': {'review_policy': 'script_only'},
            'stages': [{'id': s, 'provider': p, 'max_cost_usd': '0'} for s, p in STAGES],
        }
        self.run = Run.create(self.root / 'run', self.spec, mode='live')

    def produce(self, stage, text='Isolated review fixture, no generation APIs'):
        assets = self.run.directory / 'artifacts'
        assets.mkdir(exist_ok=True)
        file = assets / (stage + '.txt')
        file.write_text(text)
        attempt = self.run.claim(stage)
        self.run.complete(attempt, file, actual_usd='0')
        return {'summary': 'Observed fixture', 'checks': [],
                'evidence_files': ['artifacts/' + file.name], 'unverified': []}

    def approve_script(self):
        record = self.produce('source')
        record['checks'] = ['text_read']
        self.run.decide('source', 'next', record)
        record = self.produce('dialogue')
        record['checks'] = ['text_read']
        self.run.decide('dialogue', 'next', record, actor='human')

    def test_only_script_needs_human_and_media_can_keep_unverified_items(self):
        self.approve_script()
        requirements = {
            'audio': ['technical_checked'], 'image': ['image_viewed'],
            'video': ['technical_checked', 'image_viewed'], 'upscale': ['technical_checked'],
            'lipsync': ['technical_checked', 'image_viewed'],
            'edit': ['technical_checked', 'image_viewed', 'text_read'],
            'description': ['text_read'],
        }
        for stage, checks in requirements.items():
            record = self.produce(stage)
            record['checks'] = checks
            if stage in {'audio', 'video', 'lipsync', 'edit'}:
                record['unverified'] = ['Perceptual quality not directly observed']
            self.run.decide(stage, 'next', record)
        self.assertTrue(self.run.status()['complete'])
        self.assertFalse(self.run.status()['perceptual_quality_verified'])
        decision = next(d for d in self.run.status()['decisions'] if d['stage'] == 'audio')
        observed = json.loads(decision['evidence_json'])
        self.assertNotIn('audio_listened', observed['checks'])
        self.assertTrue(observed['unverified'])

    def test_script_cannot_be_approved_by_agent(self):
        record = self.produce('source')
        record['checks'] = ['text_read']
        self.run.decide('source', 'next', record)
        record = self.produce('dialogue')
        record['checks'] = ['text_read']
        with self.assertRaises(PipelineError):
            self.run.decide('dialogue', 'next', record)
        self.run.decide('dialogue', 'next', record, actor='human')
        self.assertIsInstance(self.run.claim('audio'), int)

    def test_media_without_technical_check_is_rejected(self):
        self.approve_script()
        record = self.produce('audio')
        record['unverified'] = ['No listening available']
        with self.assertRaises(PipelineError):
            self.run.decide('audio', 'next', record)

    def test_unresolved_source_claim_still_blocks(self):
        record = self.produce('source')
        record['checks'] = ['text_read']
        record['unverified'] = ['Article claim lacks a source']
        with self.assertRaises(PipelineError):
            self.run.decide('source', 'next', record)

    def test_existing_agent_script_approval_is_revoked_on_policy_change(self):
        strict = {**self.spec, 'job': {}}
        run = Run.create(self.root / 'strict', strict, mode='live')
        self.run = run
        for stage in ('source', 'dialogue'):
            record = self.produce(stage)
            record['checks'] = list(REQUIRED_CHECKS[stage])
            run.decide(stage, 'next', record)
        run.invalidate([], 'User requests script-only human checkpoint', new_spec=self.spec)
        with self.assertRaises(PipelineError):
            run.claim('audio')

    def timing_run(self, *, authorized=True):
        self.spec['job']['timing_revision_authorized'] = authorized
        self.run = Run.create(self.root / 'timing', self.spec, mode='live')

    def revise_script(self, reason='尺調整: 実測発話が短いため同じ意味で言い回しを調整'):
        self.run.invalidate(['dialogue'], reason)
        record = self.produce('dialogue', 'Rephrased timing fixture with the same source')
        record['checks'] = ['text_read']
        return record

    def test_authorized_timing_revisions_progress_without_new_human_approval(self):
        self.timing_run()
        self.approve_script()
        for _ in range(2):
            self.produce('audio')
            record = self.revise_script()
            self.run.decide('dialogue', 'next', record, actor='codex_agent')
            script = next(s for s in self.run.status()['stages'] if s['id'] == 'dialogue')
            self.assertTrue(script['progress_allowed'])
        approvals = [d for d in self.run.status()['decisions'] if d['stage'] == 'dialogue']
        self.assertEqual([d['actor'] for d in approvals], ['human', 'codex_agent', 'codex_agent'])
        self.assertIsInstance(self.run.claim('audio'), int)

    def test_timing_flag_does_not_allow_initial_agent_script_approval(self):
        self.timing_run()
        record = self.produce('source')
        record['checks'] = ['text_read']
        self.run.decide('source', 'next', record)
        record = self.produce('dialogue')
        record['checks'] = ['text_read']
        with self.assertRaises(PipelineError):
            self.run.decide('dialogue', 'next', record)

    def test_timing_revision_without_same_run_human_approval_is_rejected(self):
        self.timing_run()
        record = self.produce('source')
        record['checks'] = ['text_read']
        self.run.decide('source', 'next', record)
        self.produce('dialogue')
        record = self.revise_script()
        with self.assertRaises(PipelineError):
            self.run.decide('dialogue', 'next', record)

    def test_timing_revision_without_authorization_is_rejected(self):
        self.timing_run(authorized=False)
        self.approve_script()
        record = self.revise_script()
        with self.assertRaises(PipelineError):
            self.run.decide('dialogue', 'next', record)

    def test_non_timing_revision_cannot_inherit_script_approval(self):
        self.timing_run()
        self.approve_script()
        record = self.revise_script('新しい主題と主張へ変更')
        with self.assertRaises(PipelineError):
            self.run.decide('dialogue', 'next', record)
        record = self.revise_script()
        with self.assertRaises(PipelineError):
            self.run.decide('dialogue', 'next', record)

    def test_changed_source_cannot_inherit_script_approval(self):
        self.timing_run()
        self.approve_script()
        self.run.invalidate(['source'], '尺調整: 記事自体を変更')
        record = self.produce('source', 'Different source article')
        record['checks'] = ['text_read']
        self.run.decide('source', 'next', record)
        record = self.produce('dialogue')
        record['checks'] = ['text_read']
        with self.assertRaises(PipelineError):
            self.run.decide('dialogue', 'next', record)

    def test_timing_revision_rejects_modified_source_or_original_script_history(self):
        self.timing_run()
        self.approve_script()
        record = self.revise_script()
        source = self.run.directory / 'artifacts/source.txt'
        original = source.read_text()
        source.write_text('Untracked changed source')
        with self.assertRaises(PipelineError):
            self.run.decide('dialogue', 'next', record)
        source.write_text(original)
        history = self.run.directory / 'history/dialogue/r0/dialogue.txt'
        history.write_text('Changed approved original script')
        with self.assertRaises(PipelineError):
            self.run.decide('dialogue', 'next', record)

    def test_timing_revision_retains_spending_and_approved_budget(self):
        self.spec['stages'][2]['max_cost_usd'] = '.01'
        self.timing_run()
        self.approve_script()
        audio = self.run.directory / 'artifacts/audio.txt'
        audio.write_text('Paid audio fixture')
        self.run.complete(self.run.claim('audio'), audio, actual_usd='.01')
        record = self.revise_script()
        self.run.decide('dialogue', 'next', record)
        self.assertEqual(self.run.status()['charged_or_reserved_usd'], '0.01')
        inflated = {**self.spec, 'budget_usd': '2.01'}
        with self.assertRaises(PipelineError):
            self.run.invalidate(['dialogue'], '尺調整: 予算増額は不可', new_spec=inflated)


if __name__ == '__main__':
    unittest.main()

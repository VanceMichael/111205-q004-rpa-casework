"""端到端：编排、dry-run、故障注入、证据、重启与选择性续做。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from desktop import DesktopControls, controls_factory
from rpa import planner as p
from rpa.executor import Executor
from rpa.store import RunStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'vendors.json'


def rows():
    return json.loads(FIXTURE.read_text())['rows']


def plans():
    return p.prepare_batch(rows())


class PlannerTests(unittest.TestCase):
    def test_normalized_dedup_keeps_first_row(self):
        batch = plans()
        self.assertEqual([item.key for item in batch],
                         ['tax:CN123', 'tax:CN123@2', 'tax:CN124'])
        self.assertEqual(batch[0].status, p.PENDING)
        self.assertEqual(batch[1].status, p.DUPLICATE_LOCAL)
        self.assertEqual(batch[2].status, p.NEEDS_REVIEW)
        self.assertEqual(batch[1].tax_id, 'CN123')

    def test_normalization_is_shared_with_existing_service(self):
        first, second = rows()[0], rows()[1]
        self.assertEqual(p.plan_vendor(first), p.plan_vendor(second))
        self.assertEqual(p.plan_vendor(first)['tax_id'], 'CN123')

    def test_dry_run_is_field_level_and_has_no_side_effects(self):
        with TemporaryDirectory() as directory:
            steps = p.plan_to_dicts(p.build_steps(plans(), include_blocked=True))
            self.assertEqual(steps[0]['action'], 'open_form')
            self.assertEqual(steps[1]['action'], 'refocus')
            self.assertEqual(steps[2]['fields'], {'tax_id': 'CN123'})
            self.assertEqual(steps[5]['action'], 'verify_visible_values')
            self.assertEqual(steps[6]['action'], 'click_save')
            self.assertEqual(steps[-1]['action'], 'skip')
            self.assertFalse((Path(directory) / 'desktop.json').exists())

    def test_dry_run_never_emits_input_actions_for_committed_records(self):
        with TemporaryDirectory() as directory:
            store = RunStore(directory)
            store.mark('tax:CN123', p.PORTAL_COMMITTED)
            steps = p.plan_to_dicts(p.build_steps(
                plans(), resume_from=store.records, include_blocked=True))
            actions = [s['action'] for s in steps if s['record_key'] == 'tax:CN123']
            self.assertEqual(actions, ['skip'])

    def test_committed_blocks_are_hidden_without_include_flag(self):
        with TemporaryDirectory() as directory:
            store = RunStore(directory)
            store.mark('tax:CN123', p.PORTAL_COMMITTED)
            steps = p.build_steps(plans(), resume_from=store.records)
            self.assertNotIn('tax:CN123', {step.record_key for step in steps})


class ExecutorTests(unittest.TestCase):
    def test_happy_path_commits_with_evidence_and_transitions(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            store = RunStore(directory)
            summary = Executor(DesktopControls(path), store).run(plans())
            self.assertEqual(summary['committed'], ['tax:CN123'])
            self.assertEqual(summary['duplicate'], [])
            self.assertEqual(summary['review'], ['tax:CN124'])
            state = store.state_for('tax:CN123')
            self.assertEqual(state.status, p.PORTAL_COMMITTED)
            self.assertEqual(state.evidence[0]['kind'], 'synthetic-screen-metadata')
            self.assertEqual(state.transitions[0]['from'], p.PENDING)
            self.assertIn('saved', [e['step'] for e in state.evidence])
            portal = json.loads(path.read_text())
            self.assertEqual(portal['CN123']['address'], '一区')

    def test_confirmation_pauses_and_never_resubmits(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            store = RunStore(directory)
            summary = Executor(DesktopControls(path, faults=['confirmation']),
                               store).run(plans())
            self.assertEqual(summary['awaiting'], ['tax:CN123'])
            self.assertEqual(DesktopControls(path).records, {})
            state = store.state_for('tax:CN123')
            self.assertEqual(state.status, p.AWAITING_CONFIRMATION)
            self.assertTrue(state.manual_steps)
            self.assertEqual(state.evidence[-1]['screen'], 'confirmation')
            self.assertEqual(state.attempts, 1)

    def test_explicit_confirmation_commits_exactly_once(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            store = RunStore(directory)
            summary = Executor(DesktopControls(path, faults=['confirmation']),
                               store).run(plans(), allow_confirmation=True)
            self.assertEqual(summary['committed'], ['tax:CN123'])
            self.assertEqual(len(DesktopControls(path).records), 1)

    def test_timeout_after_save_is_reconciled_not_replayed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            store = RunStore(directory)
            summary = Executor(DesktopControls(path, faults=['timeout_after_save']),
                               store).run(plans())
            self.assertEqual(summary['committed'], ['tax:CN123'])
            state = store.state_for('tax:CN123')
            self.assertEqual(state.status, p.PORTAL_COMMITTED)
            self.assertIn('timeout_but_present', [e['step'] for e in state.evidence])
            self.assertEqual(state.attempts, 1)   # 只提交过一次

    def test_timeout_with_invisible_portal_stays_unknown(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            desktop = DesktopControls(path, faults=['timeout_after_save'])
            desktop.find = lambda tax_id: None
            store = RunStore(directory)
            summary = Executor(desktop, store).run(plans())
            self.assertEqual(summary['unknown'], ['tax:CN123'])
            state = store.state_for('tax:CN123')
            self.assertEqual(state.status, p.SUBMIT_UNKNOWN)
            self.assertTrue(state.manual_steps)

    def test_unknown_resumes_only_after_portal_confirmation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            desktop = DesktopControls(path, faults=['timeout_after_save'])
            desktop.find = lambda tax_id: None
            store = RunStore(directory)
            Executor(desktop, store).run(plans())
            desktop.find = DesktopControls(path).find
            desktop.screen = 'list'
            resumed = Executor(desktop, store).run(plans(), keys=['tax:CN123'])
            self.assertEqual(resumed['committed'], ['tax:CN123'])
            self.assertEqual(len(DesktopControls(path).records), 1)

    def test_focus_loss_pauses_before_commit(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            store = RunStore(directory)
            summary = Executor(DesktopControls(path, faults=['focus_lost']),
                               store).run(plans())
            self.assertEqual(DesktopControls(path).records, {})
            state = store.state_for('tax:CN123')
            self.assertEqual(state.status, p.UNSAVED_INCOMPLETE)
            self.assertTrue(state.manual_steps)
            self.assertEqual(summary['review'], ['tax:CN123'])

    def test_validation_failure_leaves_address_for_human(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            store = RunStore(directory)
            desktop = DesktopControls(path, faults=['validation'])
            summary = Executor(desktop, store).run(plans())
            self.assertEqual(summary['review'], ['tax:CN123'])
            self.assertEqual(DesktopControls(path).records, {})
            self.assertIn('地址', '\n'.join(store.state_for('tax:CN123').manual_steps))

    def test_crash_mid_batch_keeps_committed_out_of_replay(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            store = RunStore(directory)
            batch = plans()
            desktop = DesktopControls(path)
            Executor(desktop, store).run(batch[:1])
            desktop.schedule('crash')
            resumed = Executor(desktop, store).run(batch)
            self.assertEqual(resumed['skipped'], ['tax:CN123', 'tax:CN123@2'])
            self.assertEqual(list(DesktopControls(path).records), ['CN123'])
            self.assertEqual(store.state_for('tax:CN123').status, p.PORTAL_COMMITTED)

    def test_crash_during_next_record_pauses_with_manual_steps(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            store = RunStore(directory)
            batch = plans()
            desktop = DesktopControls(path)
            Executor(desktop, store).run(batch[:1])
            desktop.schedule('crash')
            resumed = Executor(desktop, store).run(batch, keys=['tax:CN124'])
            self.assertEqual(resumed['review'], ['tax:CN124'])
            state = store.state_for('tax:CN124')
            self.assertEqual(state.status, p.NEEDS_REVIEW)
            self.assertTrue(state.manual_steps)

    def test_portal_duplicate_is_rejected_before_opening_form(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            path.write_text(json.dumps({'CN123': {'tax_id': 'CN123',
                                                  'name': 'existing',
                                                  'address': 'x'}}))
            desktop = DesktopControls(path)
            store = RunStore(directory)
            summary = Executor(desktop, store).run(plans())
            self.assertEqual(summary['duplicate'], ['tax:CN123'])
            self.assertEqual(desktop.actions, [])

    def test_truncated_write_is_flagged_not_silently_committed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            store = RunStore(directory)
            desktop = DesktopControls(path, faults=['partial_write'])
            summary = Executor(desktop, store).run(plans())
            state = store.state_for('tax:CN123')
            self.assertEqual(state.status, p.NEEDS_REVIEW)
            self.assertIn('tax:CN123', summary['review'])
            self.assertTrue(state.manual_steps)
            self.assertEqual(DesktopControls(path).records['CN123']['address'], '一')

    def test_confirmation_dialog_lost_after_restart_requires_explicit_approval(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            store = RunStore(directory)
            desktop = DesktopControls(path, faults=['confirmation'])
            Executor(desktop, store).run(plans())
            state = store.state_for('tax:CN123')
            self.assertEqual(state.status, p.AWAITING_CONFIRMATION)
            # 客户端重启：确认框消失，门户仍是空的。
            fresh = DesktopControls(path)
            refused = Executor(fresh, store).run(plans(), keys=['tax:CN123'])
            self.assertEqual(refused['awaiting'], ['tax:CN123'])
            self.assertEqual(fresh.actions, [])
            approved = Executor(fresh, store).run(
                plans(), keys=['tax:CN123'], allow_confirmation=True)
            self.assertEqual(approved['committed'], ['tax:CN123'])
            self.assertEqual(len(DesktopControls(path).records), 1)

    def test_selective_resume_only_touches_named_keys(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            desktop = DesktopControls(path)
            store = RunStore(directory)
            Executor(desktop, store).run(plans())
            fixed = p.prepare_batch([{'tax_id': 'CN124', 'name': '人工复核样本',
                                      'address': '二区'}])
            resumed = Executor(desktop, store).run(fixed, keys=['tax:CN124'])
            self.assertEqual(resumed['committed'], ['tax:CN124'])
            self.assertEqual(DesktopControls(path).records['CN124']['address'], '二区')

    def test_store_round_trip_preserves_evidence_and_transitions(self):
        with TemporaryDirectory() as directory:
            store = RunStore(directory)
            state = store.state_for('tax:CN123')
            store.transition(state, p.PENDING, 'x', 'form')
            store.add_evidence(state, {'kind': 'metadata', 'sha256': 'abc'}, 'filled')
            reloaded = RunStore(directory).state_for('tax:CN123')
            self.assertEqual(reloaded.transitions, state.transitions)
            self.assertEqual(len(reloaded.evidence), 1)

    def test_manual_queue_lists_blocked_records(self):
        with TemporaryDirectory() as directory:
            store = RunStore(directory)
            store.mark('tax:CN124', p.NEEDS_REVIEW, 'missing address',
                       ['补齐地址后再续做。'])
            queue = store.manual_queue()
            self.assertEqual(queue[0]['key'], 'tax:CN124')
            self.assertTrue(queue[0]['manual_steps'])


class ResumeAfterRestartTests(unittest.TestCase):
    def test_fresh_controls_instance_reads_persisted_portal(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            store = RunStore(directory)
            factory = controls_factory(path)
            first = Executor(factory(), store).run(plans())
            self.assertEqual(first['committed'], ['tax:CN123'])
            second = Executor(factory(), store).run(plans())
            self.assertEqual(second['skipped'], ['tax:CN123', 'tax:CN123@2'])
            self.assertEqual(len(factory().records), 1)


if __name__ == '__main__':
    unittest.main()

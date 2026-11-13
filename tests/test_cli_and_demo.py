"""CLI 与演示流程的端到端测试（离线、不访问真实客户端）。"""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def cli(*args, workdir):
    result = subprocess.run(
        [sys.executable, '-m', 'rpa.cli', *args, '--workdir', str(workdir)],
        cwd=ROOT, text=True, capture_output=True, timeout=30)
    return result, json.loads(result.stdout)


class CliTests(unittest.TestCase):
    def test_dry_run_does_not_write_any_state(self):
        with TemporaryDirectory() as directory:
            workdir = Path(directory) / 'state'
            result, payload = cli('dry-run', '--input', 'fixtures/vendors.csv',
                                  workdir=workdir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(payload['mode'], 'dry-run')
            self.assertFalse(workdir.exists())
            self.assertEqual([s['sequence'] for s in payload['steps']],
                             list(range(1, len(payload['steps']) + 1)))

    def test_run_then_resume_corrected_row(self):
        with TemporaryDirectory() as directory:
            workdir = Path(directory) / 'state'
            result, payload = cli('run', '--input', 'fixtures/vendors.csv',
                                  workdir=workdir)
            self.assertEqual(result.returncode, 1)  # 有人工步骤
            self.assertEqual(payload['summary']['committed'], ['tax:CN123'])
            self.assertEqual(payload['records']['tax:CN124']['status'],
                             'needs_review')
            self.assertTrue(payload['manual_queue'])
            fixed = Path(directory) / 'fixed.json'
            fixed.write_text(json.dumps({'rows': [
                {'tax_id': 'CN124', 'name': '人工复核样本', 'address': '二区'}]}))
            result, resumed = cli('resume', '--keys', 'tax:CN124',
                                  '--input', str(fixed), workdir=workdir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(resumed['summary']['committed'], ['tax:CN124'])
            portal = json.loads((workdir / 'desktop.json').read_text())
            self.assertEqual(set(portal), {'CN123', 'CN124'})

    def test_status_reports_evidence_and_manual_queue(self):
        with TemporaryDirectory() as directory:
            workdir = Path(directory) / 'state'
            cli('run', '--input', 'fixtures/vendors.csv', workdir=workdir)
            result, payload = cli('status', workdir=workdir)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = payload['records']['tax:CN123']
            self.assertTrue(state['evidence'])
            self.assertEqual(state['evidence'][0]['kind'],
                             'synthetic-screen-metadata')
            self.assertIn('manual_queue', payload)


class DemoFlowTests(unittest.TestCase):
    def test_demo_covers_import_to_manual_handoff(self):
        from demo import run
        first = run()
        self.assertEqual(first, run())  # 无隐藏状态、可重复
        self.assertEqual(first['first_run']['summary']['awaiting'], ['tax:CN123'])
        self.assertEqual(first['resume']['summary']['committed'], ['tax:CN123'])
        self.assertEqual(first['rerun']['summary']['committed'], [])
        self.assertEqual(first['rerun']['summary']['skipped'],
                         ['tax:CN123', 'tax:CN123@2'])
        self.assertEqual(first['manual_queue'][0]['key'], 'tax:CN124')
        self.assertTrue(first['resume']['evidence'])
        self.assertTrue(first['event_log'])


if __name__ == '__main__':
    unittest.main()

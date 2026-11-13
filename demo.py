"""离线演示：从 CSV 导入到人工接管的完整流程。

流程：导入 → 规范化去重 → dry-run 计划 → 执行（注入二次确认故障而暂停）
→ 重启客户端并选择性续做（人工放行确认）→ 再次运行验证已提交记录不被重放
→ 输出待人工队列。
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from desktop import DesktopControls
from rpa import planner as p
from rpa.executor import Executor
from rpa.store import RunStore

ROOT = Path(__file__).resolve().parent


def _snapshot(store):
    return {key: {'status': state.status, 'attempts': state.attempts,
                  'manual_steps': list(state.manual_steps)}
            for key, state in store.records.items()}


def run():
    rows = p.load_rows(ROOT / 'fixtures' / 'vendors.csv')
    batch = p.prepare_batch(rows)
    result = {
        'imported_rows': len(rows),
        'records': [item.to_dict() for item in batch],
        'dry_run': p.plan_to_dicts(p.build_steps(batch, include_blocked=True)),
    }
    with TemporaryDirectory() as directory:
        state_path = Path(directory) / 'desktop.json'

        # 1) 首次执行：第一条保存时客户端弹出二次确认，流程在保存前暂停。
        store = RunStore(directory)
        desktop = DesktopControls(state_path, faults=['confirmation'])
        first = Executor(desktop, store).run(batch)
        result['first_run'] = {
            'summary': first,
            'records': _snapshot(store),
            'portal': DesktopControls(state_path).records,
            'manual_queue': store.manual_queue(),
        }

        # 2) 模拟客户端重启（新实例）后，只续做被暂停的记录；人工显式放行确认。
        restarted = DesktopControls(state_path)
        resumed = Executor(restarted, store).run(
            batch, keys=['tax:CN123'], allow_confirmation=True)
        result['resume'] = {
            'summary': resumed,
            'records': _snapshot(store),
            'portal': DesktopControls(state_path).records,
            'evidence': store.state_for('tax:CN123').evidence,
            'transitions': store.state_for('tax:CN123').transitions,
            'manual_queue': store.manual_queue(),
        }

        # 3) 整批再跑一次：已提交记录被跳过，不产生任何输入动作。
        again = Executor(DesktopControls(state_path), store).run(batch)
        result['rerun'] = {
            'summary': again,
            'portal': DesktopControls(state_path).records,
        }
        result['manual_queue'] = store.manual_queue()
        result['event_log'] = store.event_log()
    return result


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2, sort_keys=True))

"""命令行入口：dry-run、执行、选择性续做与人工队列。

用法示例（在仓库根目录）::

    python3 -m rpa.cli dry-run --input fixtures/vendors.csv
    python3 -m rpa.cli run --workdir /tmp/rpa-state --input fixtures/vendors.csv
    python3 -m rpa.cli resume --workdir /tmp/rpa-state --keys tax:CN124
    python3 -m rpa.cli status --workdir /tmp/rpa-state

所有输入均在本地；不连接真实客户端。dry-run 不写门户、不写状态目录。
"""

import argparse
import json
import sys
from pathlib import Path

from rpa import planner as p
from rpa.executor import Executor
from rpa.store import RunStore


def _load_plans(path: str):
    return p.prepare_batch(p.load_rows(path))


def _desktop_factory(workdir: Path):
    from desktop import controls_factory
    return controls_factory(workdir / 'desktop.json')


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_dry_run(args) -> int:
    plans = _load_plans(args.input)
    resume_from = None
    if args.workdir and (Path(args.workdir) / 'run-state.json').exists():
        resume_from = RunStore(args.workdir).records
    steps = p.plan_to_dicts(p.build_steps(
        plans, resume_from=resume_from, include_blocked=True))
    _print({'mode': 'dry-run', 'records': [item.to_dict() for item in plans],
            'steps': steps})
    return 0


def _run(args, plans, keys=None, allow_confirmation=False) -> int:
    workdir = Path(args.workdir)
    store = RunStore(workdir)
    summary = {'mode': 'resume' if keys else 'run'}
    for name in ('committed', 'awaiting', 'unknown', 'duplicate', 'review', 'skipped'):
        summary[name] = []
    summary.update(Executor(_desktop_factory(workdir)(), store).run(
        plans, keys=keys, allow_confirmation=allow_confirmation,
        pause_on_manual=not args.no_pause))
    payload = {
        'summary': summary,
        'manual_queue': store.manual_queue(),
        'records': {key: state.to_dict() for key, state in store.records.items()},
        'desktop_state': str(workdir / 'desktop.json'),
    }
    _print(payload)
    return 0 if not (summary['awaiting'] or summary['unknown']
                     or summary['review']) else 1


def cmd_run(args) -> int:
    return _run(args, _load_plans(args.input))


def cmd_resume(args) -> int:
    plans = _load_plans(args.input)
    keys = [key.strip() for key in args.keys.split(',') if key.strip()]
    return _run(args, plans, keys=keys, allow_confirmation=args.confirm)


def cmd_status(args) -> int:
    store = RunStore(args.workdir)
    _print({'records': {key: state.to_dict() for key, state in store.records.items()},
            'manual_queue': store.manual_queue(),
            'events': store.event_log()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='rpa', description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    dry = sub.add_parser('dry-run', help='只生成逐字段操作计划，不执行')
    dry.add_argument('--input', default='fixtures/vendors.csv')
    dry.add_argument('--workdir', default=None,
                     help='可选：读取已有状态，以便展示哪些记录会被跳过')
    dry.set_defaults(func=cmd_dry_run, workdir=None)

    for name, func, help_text in (
            ('run', cmd_run, '执行导批；遇人工步骤暂停'),
            ('resume', cmd_resume, '从失败批次中选择性续做')):
        command = sub.add_parser(name, help=help_text)
        command.add_argument('--input', default='fixtures/vendors.csv')
        command.add_argument('--workdir', default='.rpa-state',
                             help='状态目录（默认 .rpa-state）')
        command.add_argument('--no-pause', action='store_true',
                             help='不在人工步骤处暂停（仍不重放已提交记录）')
        if name == 'resume':
            command.add_argument('--keys', required=True,
                                 help='逗号分隔的记录键，如 tax:CN124')
            command.add_argument('--confirm', action='store_true',
                                 help='放行二次确认弹窗（仅对本次选中记录）')
        command.set_defaults(func=func)

    status = sub.add_parser('status', help='查看记录状态、证据与待人工队列')
    status.add_argument('--workdir', default='.rpa-state')
    status.set_defaults(func=cmd_status)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())

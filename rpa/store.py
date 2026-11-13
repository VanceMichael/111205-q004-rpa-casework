"""持久化运行状态：每条记录的状态、证据与状态转移事件。

状态目录只写 JSON；证据是模拟器返回的可见屏幕元数据，不含真实截图。
写入使用临时文件加原子替换，进程中断不会留下半个状态文件。
"""

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from rpa.planner import RecordState


class RunStore:
    def __init__(self, directory) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'run-state.json'
        self.data: Dict[str, Any] = {'version': 1, 'records': {}, 'events': []}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())

    @property
    def records(self) -> Dict[str, RecordState]:
        return {key: RecordState.from_dict(value)
                for key, value in self.data['records'].items()}

    def state_for(self, key: str) -> RecordState:
        data = self.data['records'].get(key)
        return RecordState.from_dict(data) if data else RecordState(key=key)

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2,
                                        sort_keys=True))
        temporary.replace(self.path)

    def put(self, state: RecordState) -> None:
        self.data['records'][state.key] = state.to_dict()

    def transition(self, state: RecordState, target: str, action: str,
                   detail: str = '', screen: Optional[str] = None) -> None:
        """记录一次状态转移并立即落盘；source 取转移前的状态。"""
        source = state.status
        if screen is None:
            screen = state.last_screen
        event = {'record': state.key, 'from': source, 'to': target,
                 'action': action, 'screen': screen, 'detail': detail}
        state.status = target
        state.last_screen = screen
        state.transitions.append(event)
        self.data['events'].append(event)
        self.put(state)
        self.save()

    def add_evidence(self, state: RecordState, evidence: Dict[str, Any],
                     step: str) -> None:
        entry = dict(evidence)
        entry['step'] = step
        state.evidence.append(entry)
        self.put(state)
        self.save()

    def mark(self, key: str, status: str, note: str = '',
             manual_steps: Optional[Iterable[str]] = None) -> RecordState:
        """不经过界面，直接给人或批处理留下终态说明。"""
        state = self.state_for(key)
        source = state.status
        state.status = status
        state.note = note
        if manual_steps is not None:
            state.manual_steps = list(manual_steps)
        event = {'record': key, 'from': source, 'to': status,
                 'action': 'mark', 'screen': state.last_screen, 'detail': note}
        state.transitions.append(event)
        self.data['events'].append(event)
        self.put(state)
        self.save()
        return state

    def manual_queue(self) -> List[Dict[str, Any]]:
        from rpa.planner import NEEDS_HUMAN
        queue = []
        for key, state in self.records.items():
            if state.status in NEEDS_HUMAN:
                queue.append({'key': key, 'status': state.status,
                              'note': state.note,
                              'manual_steps': list(state.manual_steps)})
        return queue

    def event_log(self) -> List[Dict[str, Any]]:
        return list(self.data['events'])

    def clear(self) -> None:
        self.data = {'version': 1, 'records': {}, 'events': []}
        self.save()

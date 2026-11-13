"""纯业务编排：批次、去重、逐字段计划与状态定义。不触碰界面对象。"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from service import plan_vendor

# 一条记录的生命周期。只有 portal_committed 表示可靠写入了门户；
# submit_unknown 表示“可能已写入也可能没有”，必须查门户后才能继续。
PENDING = 'pending'
DUPLICATE_LOCAL = 'duplicate_local'          # 批次内规范化税号重复
DUPLICATE_PORTAL = 'duplicate_portal'        # 门户已有同税号
UNSAVED_INCOMPLETE = 'unsaved_incomplete'    # 校验失败 / 焦点丢失，未提交
AWAITING_CONFIRMATION = 'awaiting_confirmation'
SUBMIT_UNKNOWN = 'submit_unknown'            # 保存超时等，结果不可确认
PORTAL_COMMITTED = 'portal_committed'
NEEDS_REVIEW = 'needs_review'                # 数据或界面异常，转人工

# 已确认处理完毕的记录，任何情况下都不再生成输入动作。
BLOCKED = frozenset({DUPLICATE_LOCAL, DUPLICATE_PORTAL, PORTAL_COMMITTED})
# 需要操作员介入的状态；批次默认在此暂停并进入人工队列。
NEEDS_HUMAN = frozenset({UNSAVED_INCOMPLETE, AWAITING_CONFIRMATION, SUBMIT_UNKNOWN,
                         NEEDS_REVIEW})
FIELDS = ('tax_id', 'name', 'address')


@dataclass
class RecordPlan:
    """一条导入记录及其规范化后的逐字段输入计划。"""

    key: str
    source: Dict[str, Any]
    plan: Optional[Dict[str, str]] = None
    status: str = PENDING
    detail: str = ''
    manual_steps: List[str] = field(default_factory=list)

    @property
    def tax_id(self) -> Optional[str]:
        return self.plan['tax_id'] if self.plan else None

    def to_dict(self) -> Dict[str, Any]:
        return {'key': self.key, 'status': self.status, 'plan': self.plan,
                'detail': self.detail, 'manual_steps': list(self.manual_steps)}


@dataclass
class Step:
    """计划中的一步界面动作。dry-run 只输出这些，不执行。"""

    sequence: int
    record_key: str
    action: str
    fields: Dict[str, str] = field(default_factory=dict)
    detail: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {'sequence': self.sequence, 'record_key': self.record_key,
                'action': self.action, 'fields': dict(self.fields),
                'detail': self.detail}


@dataclass
class RecordState:
    key: str
    status: str = PENDING
    attempts: int = 0
    last_screen: str = ''
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    transitions: List[Dict[str, Any]] = field(default_factory=list)
    manual_steps: List[str] = field(default_factory=list)
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {'key': self.key, 'status': self.status, 'attempts': self.attempts,
                'last_screen': self.last_screen, 'evidence': list(self.evidence),
                'transitions': list(self.transitions),
                'manual_steps': list(self.manual_steps), 'note': self.note}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RecordState':
        names = {name for name in cls.__dataclass_fields__}
        return cls(**{name: data[name] for name in names if name in data})


def _fingerprint(row: Dict[str, Any]) -> str:
    raw = json.dumps(row, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def make_keys(rows: Iterable[Dict[str, Any]]) -> List[str]:
    """为每行生成稳定且唯一的记录键（税号优先，重复出现加行号）。"""
    keys: List[str] = []
    used = set()
    for index, row in enumerate(rows):
        try:
            base = 'tax:' + plan_vendor(row)['tax_id']
        except ValueError:
            base = 'row:%03d:%s' % (index + 1, _fingerprint(row))
        key = base
        if key in used:
            key = '%s@%d' % (base, index + 1)
        used.add(key)
        keys.append(key)
    return keys


def load_rows(path) -> List[Dict[str, Any]]:
    """接受 vendors.json（{rows: [...]}）或 CSV 转换后的行列表。"""
    source = Path(path)
    if source.suffix.lower() == '.csv':
        import csv
        with source.open(newline='', encoding='utf-8') as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    payload = json.loads(source.read_text())
    if isinstance(payload, dict):
        payload = payload['rows']
    if not isinstance(payload, list):
        raise ValueError('fixture must contain a list of rows')
    return payload


def build_plans(rows: Iterable[Dict[str, Any]]) -> List[RecordPlan]:
    """规范化每一行并生成逐字段输入计划。纯函数，不读取门户。"""
    plans = []
    keys = make_keys(rows)
    for index, (row, key) in enumerate(zip(rows, keys)):
        try:
            plan = plan_vendor(row)
        except ValueError as error:
            plans.append(RecordPlan(
                key=key, source=dict(row), status=NEEDS_REVIEW,
                detail='invalid row: %s' % error,
                manual_steps=['修正第 %d 行的必填税号/名称后重试。' % (index + 1)]))
        else:
            if plan['address']:
                plans.append(RecordPlan(key=key, source=dict(row), plan=plan))
            else:
                # 门户要求地址；已知缺字段时不打开表单，直接留人工步骤。
                plans.append(RecordPlan(
                    key=key, source=dict(row), plan=plan, status=NEEDS_REVIEW,
                    detail='address is empty; portal requires it',
                    manual_steps=['补齐第 %d 行的地址后再选择续做。' % (index + 1)]))
    return plans


def deduplicate(plans: Sequence[RecordPlan]) -> List[RecordPlan]:
    """按规范化税号在批次内去重，保留首次出现的记录。"""
    seen = set()
    result = []
    for item in plans:
        if item.plan and item.tax_id in seen:
            result.append(RecordPlan(
                key=item.key, source=item.source, plan=item.plan,
                status=DUPLICATE_LOCAL,
                detail='tax id %s already appears earlier in this batch' % item.tax_id,
                manual_steps=['删除批次内重复行；仅保留首条。']))
            continue
        if item.plan:
            seen.add(item.tax_id)
        result.append(item)
    return result


def prepare_batch(rows: Iterable[Dict[str, Any]]) -> List[RecordPlan]:
    return deduplicate(build_plans(rows))


def build_steps(plans: Sequence[RecordPlan], start_sequence: int = 1,
                resume_from: Optional[Dict[str, RecordState]] = None,
                include_blocked: bool = False) -> List[Step]:
    """生成逐字段操作计划，不执行任何动作。

    已提交、重复的记录不生成输入动作（include_blocked 仅展示 skip），
    确保 dry-run 不会承诺重放已提交记录。
    """
    steps: List[Step] = []
    sequence = start_sequence
    resume_from = resume_from or {}
    for item in plans:
        state = resume_from.get(item.key)
        if state and state.status in BLOCKED:
            if include_blocked:
                steps.append(Step(sequence, item.key, 'skip', {},
                                  'refusing to replay: %s' % state.status))
                sequence += 1
            continue
        if item.status in (DUPLICATE_LOCAL, NEEDS_REVIEW) or not item.plan:
            if include_blocked:
                steps.append(Step(sequence, item.key, 'skip', {},
                                  item.detail or item.status))
                sequence += 1
            continue
        if state and state.status == AWAITING_CONFIRMATION:
            # 确认框只允许人工决策：计划中只显示确认动作，绝不重填/重提交。
            steps.append(Step(sequence, item.key, 'recover_screen', {},
                              'confirmation dialog is visible; do not re-enter fields'))
            sequence += 1
            steps.append(Step(sequence, item.key, 'confirm_pending', {},
                              'requires operator approval before clicking confirm'))
            sequence += 1
            continue
        if state and state.status == SUBMIT_UNKNOWN:
            # 结果未知：先对账，不生成任何输入动作。
            steps.append(Step(sequence, item.key, 'reconcile_portal', {},
                              'look up tax id before deciding to resubmit'))
            sequence += 1
            continue
        if state and state.last_screen and state.last_screen != 'list':
            steps.append(Step(sequence, item.key, 'recover_screen', {},
                              'observed %s; dismiss before input'
                              % state.last_screen))
            sequence += 1
        steps.append(Step(sequence, item.key, 'open_form', {}, 'start from list screen'))
        sequence += 1
        steps.append(Step(sequence, item.key, 'refocus', {}, 'confirm form has focus'))
        sequence += 1
        for field_name in FIELDS:
            steps.append(Step(sequence, item.key, 'fill',
                              {field_name: item.plan[field_name]},
                              'field=%s' % field_name))
            sequence += 1
        steps.append(Step(sequence, item.key, 'verify_visible_values',
                          {name: item.plan[name] for name in FIELDS},
                          'read back before save'))
        sequence += 1
        steps.append(Step(sequence, item.key, 'click_save', {}, 'screen=form'))
        sequence += 1
    return steps


def plan_to_dicts(steps: Sequence[Step]) -> List[Dict[str, Any]]:
    return [step.to_dict() for step in steps]

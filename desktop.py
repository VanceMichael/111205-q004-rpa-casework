"""本地桌面控件模拟器。不连接真实客户端，只维护可观察的门户状态。

接口与真实适配器约定一致：find / open_form / fill / save / confirm /
evidence。额外的故障注入仅用于测试：

- ``schedule('confirmation'|'validation'|'focus_lost'|'timeout_after_save')``
  在下一次保存时消费一次；
- ``schedule('focus_lost')`` 也会在下一次输入时消费；
- ``schedule('crash')`` 让下一次界面动作抛出 ConnectionError，模拟客户端重启；
- ``schedule('partial_write')`` 保存时只写入截断的地址，用于模拟“半截地址”。

模拟屏幕元数据不是真实截图；它记录可见窗口、字段和可见内容哈希。
"""

import hashlib
import json
from pathlib import Path

SAVE_FAULTS = ('confirmation', 'validation', 'focus_lost', 'timeout_after_save',
               'partial_write')
UI_FAULTS = SAVE_FAULTS + ('crash',)


class DesktopControls:
    def __init__(self, state_path, faults=None):
        self.path = Path(state_path)
        self.records = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.screen = 'list'
        self.fields = {}
        self.actions = []
        self._faults = []
        for fault in faults or ():
            self.schedule(fault)

    # -- 测试注入 -------------------------------------------------------
    def schedule(self, fault, action=None):
        """排队一次性故障；action 可限定到 'fill' / 'save' / 'open_form'。"""
        if fault not in UI_FAULTS:
            raise ValueError('unknown fault %r' % fault)
        self._faults.append((fault, action))

    def _take(self, allowed, action):
        for index, (fault, target) in enumerate(self._faults):
            if fault in allowed and target in (None, action):
                del self._faults[index]
                return fault
        return None

    def restart(self):
        """模拟客户端重启：会话界面重置，已落盘的门户记录保留。"""
        self.screen = 'list'
        self.fields = {}
        self.actions.append({'action': 'restart'})
        return self.screen

    # -- 查询与输入 -----------------------------------------------------
    def find(self, tax_id):
        return self.records.get(tax_id)

    def open_form(self):
        fault = self._take(('crash',), 'open_form')
        if fault:
            self.restart()
            raise ConnectionError('client unavailable during open_form')
        self.screen = 'form'
        self.fields = {}
        self.actions.append({'action': 'open_form', 'screen': self.screen})
        return self.screen

    def fill(self, field, value):
        fault = self._take(('crash', 'focus_lost'), 'fill')
        if fault == 'crash':
            self.restart()
            raise ConnectionError('client unavailable during fill')
        if self.screen != 'form':
            raise ValueError('form is not focused')
        if fault == 'focus_lost':
            self.screen = 'unfocused'
            self.actions.append({'action': 'focus_lost', 'field': field})
            raise ValueError('form lost focus')
        self.fields[field] = value
        self.actions.append({'action': 'fill', 'field': field, 'screen': self.screen})

    def save(self, fault=None):
        if self.screen not in ('form', 'confirmation'):
            raise ValueError('save is not visible')
        fault = fault if fault is not None else self._take(SAVE_FAULTS + ('crash',), 'save')
        if fault == 'crash':
            self.restart()
            raise ConnectionError('client unavailable during save')
        if fault == 'focus_lost':
            self.screen = 'unfocused'
            self.actions.append({'action': 'save', 'result': self.screen})
            return self.screen
        if not self.fields.get('address') or fault == 'validation':
            self.screen = 'validation'
            self.actions.append({'action': 'save', 'result': self.screen})
            return self.screen
        if fault == 'confirmation':
            self.screen = 'confirmation'
            self.actions.append({'action': 'save', 'result': self.screen})
            return self.screen
        return self._commit(fault)

    def confirm(self):
        """在确认框中点“确认”。只允许从可见确认框调用。"""
        if self.screen != 'confirmation':
            raise ValueError('confirmation is not visible')
        self.actions.append({'action': 'confirm'})
        return self._commit(None)

    def _commit(self, fault):
        key = self.fields['tax_id']
        if key in self.records:
            self.screen = 'list'
            raise ValueError('vendor already exists')
        record = dict(self.fields)
        if fault == 'partial_write':
            # 地址被截断：写入已发生，但内容不完整，必须人工复核。
            record['address'] = (record.get('address') or '')[:1]
        self.records[key] = record
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.records, ensure_ascii=False))
        self.screen = 'list'
        self.actions.append({'action': 'save', 'result': self.screen, 'fault': fault})
        if fault == 'timeout_after_save':
            # 先提交再丢失确认：这正是不能盲目重放的原因。
            raise TimeoutError('save acknowledgement lost')
        return self.screen

    # -- 证据 -----------------------------------------------------------
    def evidence(self):
        visible = json.dumps({'screen': self.screen, 'fields': self.fields}, sort_keys=True)
        return {
            'kind': 'synthetic-screen-metadata',
            'screen': self.screen,
            'window': 'vendor-form' if self.screen in ('form', 'confirmation') else 'portal-list',
            'visible_fields': sorted(self.fields),
            'sha256': hashlib.sha256(visible.encode()).hexdigest(),
            'actions_observed': len(self.actions),
        }


def controls_factory(state_path, faults=None):
    """返回工厂：每次连接都重建控件实例，等价于客户端重新启动。"""
    return lambda: DesktopControls(state_path, faults=faults)

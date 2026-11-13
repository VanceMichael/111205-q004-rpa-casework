"""执行器：把逐字段计划翻译成界面动作，按可见屏幕状态推进。

要点：
- 每步先读屏幕状态；确认框只暂停，从不自动二次点击；
- 校验失败 / 失焦 / 重启 → 明确的人工待办，不提交；
- 保存超时后立即查门户：已存在则记为已提交，否则 submit_unknown，绝不重放；
- 界面访问全部经由注入的 controls 对象。
"""

from typing import Any, Callable, Dict, Optional, Sequence, List

from rpa import planner as p


class Interrupted(Exception):
    """操作员要求暂停；已完成记录保留状态，后续可选续做。"""


class Executor:
    def __init__(self, controls, store, observe: Optional[Callable[[], str]] = None) -> None:
        self.controls = controls
        self.store = store
        self.observe = observe

    # -- 辅助 -----------------------------------------------------------
    def _screen(self) -> str:
        if self.observe is not None:
            return self.observe()
        return getattr(self.controls, 'screen', '')

    def _evidence(self, state: p.RecordState, step: str) -> None:
        try:
            evidence = self.controls.evidence()
        except Exception as error:  # 证据失败不应掩盖真实业务状态
            evidence = {'kind': 'unavailable', 'error': str(error)}
        self.store.add_evidence(state, evidence, step)

    def _transition(self, state: p.RecordState, target: str, action: str,
                    detail: str = '') -> None:
        self.store.transition(state, target, action, detail, screen=self._screen())

    def _manual(self, state: p.RecordState, status: str, action: str,
                detail: str, steps: Sequence[str]) -> p.RecordState:
        self._evidence(state, action)
        state.manual_steps = list(steps)
        self._transition(state, status, action, detail)
        return state

    def _portal_has(self, tax_id: str) -> Optional[Dict[str, Any]]:
        try:
            found = self.controls.find(tax_id)
        except Exception:
            return None
        return found or None

    # -- 单条记录 -------------------------------------------------------
    def run_record(self, item: p.RecordPlan,
                   allow_resubmit: bool = False) -> p.RecordState:
        """推进一条记录直到终态; 已终态记录立即返回，绝不重放。"""
        state = self.store.state_for(item.key)
        if state.status in p.BLOCKED:
            return state
        if not item.plan:
            return self.store.mark(item.key, p.NEEDS_REVIEW,
                                   item.detail or 'invalid row',
                                   item.manual_steps or ['修正必填字段后重试。'])
        if state.status == p.SUBMIT_UNKNOWN:
            return self._resolve_unknown(item, state)
        if state.status == p.AWAITING_CONFIRMATION:
            # 确认框仍可见：绝不重填或二次点击，直接保持等待人工决策。
            if self._screen() == 'confirmation':
                self._evidence(state, 'confirmation_still_visible')
                return state
            # 框已消失（通常伴随客户端重启）：先查门户，再决定能否重做。
            found = self._portal_has(item.tax_id)
            if found and found.get('tax_id') == item.tax_id:
                return self._verify_committed(item, state, 'confirmation_gone')
            if not allow_resubmit:
                return self._manual(
                    state, p.AWAITING_CONFIRMATION, 'observe_confirmation_gone',
                    'confirmation dialog disappeared and portal is empty',
                    ['确认框已消失且门户无此记录，本次未提交。人工核对后可用 '
                     'resume --confirm 显式放行重做。'])
            self._evidence(state, 'confirmation_gone_approved')
            self._transition(state, p.PENDING, 'approve_resubmit',
                             'operator explicitly approved re-entering the form')

        state.attempts += 1
        try:
            return self._submit(item, state)
        except Interrupted:
            raise
        except ConnectionError as error:
            # 客户端重启：本次动作未完成，记录停在可续做状态。
            return self._manual(
                state, p.NEEDS_REVIEW, 'connection_lost', str(error),
                ['重新启动客户端，确认该税号未出现在门户，再从该记录续做。'])
        except Exception as error:  # 未预期异常也不得假装成功
            return self._manual(
                state, p.NEEDS_REVIEW, 'unexpected_error', repr(error),
                ['人工查看客户端当前屏幕，确认状态后再选择续做。'])
    def _open_form(self, item: p.RecordPlan, state: p.RecordState) -> bool:
        """丢弃残留表单，从列表重新打开空白表单。"""
        screen = self._screen()
        if screen != 'list':
            self._evidence(state, 'before_recover')
            restart = getattr(self.controls, 'restart', None)
            if restart is not None:
                restart()
        self.controls.open_form()
        self._evidence(state, 'open_form')
        state.last_screen = self._screen()
        self.store.save()
        if state.last_screen != 'form':
            self._manual(state, p.NEEDS_REVIEW, 'open_form',
                         'form is not visible (screen=%r)' % state.last_screen,
                         ['手动打开供应商表单，确认窗口标题与字段可见后重试。'])
            return False
        return True

    def _fill(self, item: p.RecordPlan, state: p.RecordState) -> bool:
        """按顺序填写；失焦即暂停交人，不做自动重填，避免半截地址。"""
        assert item.plan is not None
        for field_name in p.FIELDS:
            try:
                self.controls.fill(field_name, item.plan[field_name])
            except ValueError as error:
                self._evidence(state, 'fill_failed_' + field_name)
                if self._screen() == 'unfocused':
                    self._manual(
                        state, p.UNSAVED_INCOMPLETE, 'fill',
                        'form lost focus while filling %s; nothing was submitted'
                        % field_name,
                        ['恢复窗口焦点，核对 %s 后选择续做；不要直接点保存。'
                         % field_name])
                else:
                    self._manual(
                        state, p.NEEDS_REVIEW, 'fill',
                        'cannot fill %s: %s' % (field_name, error),
                        ['手动聚焦表单并输入 %s。' % field_name])
                return False
        self._evidence(state, 'filled')
        return True
        self._evidence(state, 'filled')
        return True

    def _submit(self, item: p.RecordPlan, state: p.RecordState) -> p.RecordState:
        if not self._open_form(item, state):
            return state
        if not self._fill(item, state):
            return state

        try:
            screen = self.controls.save()
        except TimeoutError as error:
            return self._after_timeout(item, state, error)
        except ValueError as error:
            return self._after_rejection(item, state, error)

        if screen == 'confirmation':
            return self._manual(
                state, p.AWAITING_CONFIRMATION, 'click_save',
                'confirmation dialog is visible; a human must decide',
                ['确认弹窗已出现。人工核对字段后选择确认或取消；'
                 '确认时用 resume 并显式放行二次确认。'])
        if screen == 'validation':
            return self._manual(
                state, p.UNSAVED_INCOMPLETE, 'click_save',
                'form validation failed; nothing was submitted',
                ['补齐或修正缺失字段（如地址），再选择续做该记录。'])
        if screen == 'unfocused':
            return self._manual(
                state, p.UNSAVED_INCOMPLETE, 'click_save',
                'form lost focus; nothing was submitted',
                ['恢复窗口焦点，核对字段后选择续做。'])
        if screen == 'list':
            return self._verify_committed(item, state, 'observed_list')
        return self._manual(state, p.NEEDS_REVIEW, 'click_save',
                            'unexpected screen after save: %r' % screen,
                            ['人工查看客户端当前屏幕后决定是否续做。'])

    def _verify_committed(self, item: p.RecordPlan, state: p.RecordState,
                          action: str) -> p.RecordState:
        """保存后读回门户记录，确认字段完整；半截写入不能算成功。"""
        if item.plan is None:
            return state
        found = self._portal_has(item.tax_id)
        if not found or found.get('tax_id') != item.tax_id:
            self._evidence(state, 'saved_but_not_found')
            return self._manual(
                state, p.SUBMIT_UNKNOWN, action,
                'list screen visible but portal lookup cannot confirm the record',
                ['先查门户同税号：已存在则核对字段，不存在才可续做。'])
        mismatched = [name for name in p.FIELDS
                      if found.get(name) != item.plan[name]]
        if mismatched:
            self._evidence(state, 'saved_with_mismatch')
            return self._manual(
                state, p.NEEDS_REVIEW, action,
                'portal record differs in %s' % ', '.join(mismatched),
                ['门户记录与导入值不一致（%s）。人工校正门户并删除重复导入行。'
                 % ', '.join(mismatched)])
        self._evidence(state, 'saved')
        self._transition(state, p.PORTAL_COMMITTED, action,
                         'portal record matches the planned fields')
        return state

    def _after_timeout(self, item: p.RecordPlan, state: p.RecordState,
                       error: Exception) -> p.RecordState:
        """超时后先查门户：已写入就记为提交成功，绝不重复提交。"""
        found = self._portal_has(item.tax_id)
        if found and found.get('tax_id') == item.tax_id:
            self._evidence(state, 'timeout_but_present')
            return self._verify_committed(item, state, 'reconcile_after_timeout')
        return self._manual(
            state, p.SUBMIT_UNKNOWN, 'click_save',
            'save acknowledgement lost: %s' % error,
            ['先查门户同税号：已存在则不再提交；确认不存在后才可续做。'])

    def _after_rejection(self, item: p.RecordPlan, state: p.RecordState,
                         error: Exception) -> p.RecordState:
        """门户拒绝保存：查询后区分“已存在”与其他错误。"""
        found = self._portal_has(item.tax_id)
        if found and found.get('tax_id') == item.tax_id:
            self._evidence(state, 'duplicate_portal')
            if item.plan and any(found.get(name) != item.plan[name] for name in p.FIELDS):
                return self._manual(
                    state, p.NEEDS_REVIEW, 'portal_rejected',
                    'portal has %s but fields differ from this row' % item.tax_id,
                    ['门户已有同税号但字段不同；人工核对后再处理重复导入行。'])
            self._transition(state, p.DUPLICATE_PORTAL, 'portal_rejected',
                             'portal already has %s' % item.tax_id)
            return state
        return self._manual(state, p.NEEDS_REVIEW, 'click_save', str(error),
                            ['人工读取客户端错误提示，修正后选择续做。'])

    def _resolve_unknown(self, item: p.RecordPlan,
                         state: p.RecordState) -> p.RecordState:
        """上次结果未知的记录：先观察门户，绝不直接重放。"""
        found = self._portal_has(item.tax_id)
        if found and found.get('tax_id') == item.tax_id:
            self._evidence(state, 'resolve_unknown_present')
            return self._verify_committed(item, state, 'reconcile_unknown')
        return self._manual(
            state, p.SUBMIT_UNKNOWN, 'reconcile_unknown',
            'previous submit result is still unknown and portal is empty',
            ['人工确认门户确实没有该记录后，选择续做并重新提交。'])

    def _resolve_unknown(self, item: p.RecordPlan,
                         state: p.RecordState) -> p.RecordState:
        """上次结果未知的记录：先观察门户，绝不直接重放。"""
        found = self._portal_has(item.tax_id)
        if found and found.get('tax_id') == item.tax_id:
            self._evidence(state, 'resolve_unknown_present')
            self._transition(state, p.PORTAL_COMMITTED, 'reconcile_unknown',
                             'portal contains the record; treating as committed')
            return state
        return self._manual(
            state, p.SUBMIT_UNKNOWN, 'reconcile_unknown',
            'previous submit result is still unknown and portal is empty',
            ['人工确认门户确实没有该记录后，选择续做并重新提交。'])

    # -- 确认框 ---------------------------------------------------------
    def _confirm(self, item: p.RecordPlan, state: p.RecordState) -> p.RecordState:
        """仅当操作员显式放行时执行确认；点击后仍以可见屏幕为准。"""
        confirm = getattr(self.controls, 'confirm', None)
        if confirm is None:
            return self._manual(state, p.AWAITING_CONFIRMATION, 'confirm',
                                'controls do not expose a confirmation action',
                                state.manual_steps)
        try:
            screen = confirm()
        except TimeoutError as error:
            return self._after_timeout(item, state, error)
        except ValueError as error:
            return self._manual(state, p.AWAITING_CONFIRMATION, 'confirm',
                                'confirmation action failed: %s' % error,
                                state.manual_steps)
        state.attempts += 1
        if screen == 'list':
            return self._verify_committed(item, state, 'confirm')
        if screen == 'confirmation':
            return self._manual(state, p.AWAITING_CONFIRMATION, 'confirm',
                                'confirmation finished but dialog is still visible',
                                state.manual_steps)
        return self._manual(state, p.NEEDS_REVIEW, 'confirm',
                            'unexpected screen after confirm: %r' % screen,
                            ['人工查看客户端当前屏幕后决定是否续做。'])

    # -- 批次 -----------------------------------------------------------
    def run(self, plans: Sequence[p.RecordPlan],
            keys: Optional[Sequence[str]] = None,
            allow_confirmation: bool = False,
            should_stop: Optional[Callable[[], bool]] = None,
            duplicate_check: bool = True,
            pause_on_manual: bool = True) -> Dict[str, List[str]]:
        """按顺序处理；keys 用于从失败批次中挑记录续做。

        pause_on_manual=True 时遇到需要人工的记录即暂停，返回可续做清单。
        """
        selected = set(keys) if keys is not None else None
        summary: Dict[str, List[str]] = {name: [] for name in
                                         ('committed', 'awaiting', 'unknown',
                                          'duplicate', 'review', 'skipped')}
        for item in plans:
            if should_stop and should_stop():
                raise Interrupted('paused before %s' % item.key)
            if selected is not None and item.key not in selected:
                summary['skipped'].append(item.key)
                continue
            state = self.store.state_for(item.key)
            if state.status in p.BLOCKED or item.status in p.BLOCKED:
                summary['skipped'].append(item.key)
                continue
            if item.status == p.DUPLICATE_LOCAL:
                self.store.mark(item.key, p.DUPLICATE_LOCAL, item.detail,
                                item.manual_steps)
                summary['duplicate'].append(item.key)
                continue
            if item.status == p.NEEDS_REVIEW or not item.plan:
                # 已明确缺字段（如地址）：不打开表单，直接留人工步骤。
                self.store.mark(item.key, p.NEEDS_REVIEW,
                                item.detail or 'invalid row', item.manual_steps)
                summary['review'].append(item.key)
                if pause_on_manual:
                    break
                continue
            if duplicate_check and state.status not in p.NEEDS_HUMAN \
                    and self._portal_has(item.tax_id):
                self.store.mark(item.key, p.DUPLICATE_PORTAL,
                                'portal already has %s' % item.tax_id,
                                ['门户已有该税号；核对后删除重复导入行。'])
                summary['duplicate'].append(item.key)
                continue
            if state.status == p.AWAITING_CONFIRMATION and not allow_confirmation:
                summary['awaiting'].append(item.key)
                break

            state = self.run_record(item, allow_resubmit=allow_confirmation)
            bucket = 'committed'
            if state.status == p.AWAITING_CONFIRMATION:
                bucket = 'awaiting'
                if allow_confirmation:
                    state = self._confirm(item, state)
                    bucket = 'committed' if state.status == p.PORTAL_COMMITTED else 'awaiting'
            elif state.status == p.SUBMIT_UNKNOWN:
                bucket = 'unknown'
            elif state.status == p.DUPLICATE_PORTAL:
                bucket = 'duplicate'
            elif state.status == p.PORTAL_COMMITTED:
                bucket = 'committed'
            else:
                bucket = 'review'
            summary[bucket].append(item.key)

            if pause_on_manual and state.status in p.NEEDS_HUMAN:
                break
        self.store.save()
        return summary

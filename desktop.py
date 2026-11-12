import hashlib
import json
from pathlib import Path


class DesktopControls:
    def __init__(self, state_path):
        self.path = Path(state_path)
        self.records = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.screen = 'list'
        self.fields = {}

    def find(self, tax_id):
        return self.records.get(tax_id)

    def open_form(self):
        self.screen = 'form'
        self.fields = {}

    def fill(self, field, value):
        if self.screen != 'form':
            raise ValueError('form is not focused')
        self.fields[field] = value

    def save(self, fault=None):
        if self.screen not in ('form', 'confirmation'):
            raise ValueError('save is not visible')
        if fault == 'focus_lost':
            self.screen = 'unfocused'
            return self.screen
        if not self.fields.get('address') or fault == 'validation':
            self.screen = 'validation'
            return self.screen
        if fault == 'confirmation':
            self.screen = 'confirmation'
            return self.screen
        key = self.fields['tax_id']
        if key in self.records:
            raise ValueError('vendor already exists')
        self.records[key] = dict(self.fields)
        self.path.write_text(json.dumps(self.records, ensure_ascii=False))
        self.screen = 'list'
        if fault == 'timeout_after_save':
            raise TimeoutError('save acknowledgement lost')
        return self.screen

    def evidence(self):
        visible = json.dumps({'screen': self.screen, 'fields': self.fields}, sort_keys=True)
        return {'kind': 'synthetic-screen-metadata', 'screen': self.screen, 'sha256': hashlib.sha256(visible.encode()).hexdigest()}

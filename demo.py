import json
from pathlib import Path
from tempfile import TemporaryDirectory
from desktop import DesktopControls
from service import plan_vendor


def run():
    fixture = json.loads((Path(__file__).parent / 'fixtures/vendors.json').read_text())
    plan = plan_vendor(fixture['rows'][0])
    with TemporaryDirectory() as directory:
        state = Path(directory) / 'desktop.json'
        desktop = DesktopControls(state)
        desktop.open_form()
        for field, value in plan.items():
            desktop.fill(field, value)
        assert desktop.save('confirmation') == 'confirmation'
        confirmation = desktop.evidence()
        try:
            desktop.save('timeout_after_save')
        except TimeoutError:
            pass
        else:
            raise AssertionError('expected a lost acknowledgement')
        restored = DesktopControls(state)
        assert restored.find(plan['tax_id']) == plan
        return {'plan': plan, 'confirmation': confirmation, 'portal_after_restart': restored.records}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))

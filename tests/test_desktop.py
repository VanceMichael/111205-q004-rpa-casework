import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from desktop import DesktopControls


class DesktopTests(unittest.TestCase):
    def test_focus_loss_and_validation_do_not_commit(self):
        with TemporaryDirectory() as directory:
            desktop = DesktopControls(Path(directory) / 'state.json')
            desktop.open_form()
            desktop.fill('tax_id', 'SYN1')
            self.assertEqual(desktop.save(), 'validation')
            self.assertIsNone(desktop.find('SYN1'))
            desktop.open_form()
            self.assertEqual(desktop.save('focus_lost'), 'unfocused')
            with self.assertRaises(ValueError):
                desktop.fill('name', 'synthetic')
            self.assertFalse(desktop.path.exists())

    def test_duplicate_tax_id_is_rejected_by_portal(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            desktop = DesktopControls(path)
            for attempt in range(2):
                desktop.open_form()
                desktop.fill('tax_id', 'SYN1')
                desktop.fill('address', 'synthetic')
                if attempt == 0:
                    self.assertEqual(desktop.save(), 'list')
                else:
                    with self.assertRaises(ValueError):
                        desktop.save()
            self.assertEqual(len(DesktopControls(path).records), 1)

import unittest
from decimal import Decimal
from service import plan_vendor

class RpaCaseworkTests(unittest.TestCase):
    def test_builds_normalized_plan(self):
        self.assertEqual(plan_vendor({'tax_id': ' us-12 ', 'name': ' Acme  Ltd ', 'address': ' A '})['tax_id'], 'US12')

    def test_requires_identity(self):
        with self.assertRaises(ValueError):
            plan_vendor({'name': 'Acme'})


if __name__ == '__main__':
    unittest.main()

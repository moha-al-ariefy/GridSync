from datetime import date

from odoo.tests import tagged
from odoo.tests.common import BaseCase

from ..services.carbon_intensity import parse_neso_intensity
from ..services.energy_charts import PriceDataError


@tagged('post_install', '-at_install')
class TestCarbonIntensity(BaseCase):
    def test_forecast_and_published_estimate_stay_separate(self):
        day = date(2026, 11, 13)
        row = {
            'from': '2026-11-13T00:00Z', 'to': '2026-11-13T00:30Z',
            'intensity': {'actual': 0, 'forecast': 85},
        }
        self.assertEqual(list(parse_neso_intensity({'data': [row]}, day).values()), [
            {'actual': 0, 'forecast': 85},
        ])
        empty = dict(row, intensity={'actual': None, 'forecast': None})
        with self.assertRaisesRegex(PriceDataError, 'repeated'):
            parse_neso_intensity({'data': [empty, row]}, day)
        with self.assertRaises(PriceDataError):
            parse_neso_intensity({'data': [dict(row, to='2026-11-13T00:15Z')]}, day)

from datetime import datetime

from odoo.tests import TransactionCase, tagged

from ..services.energy_charts import PriceDataError, parse_prices


@tagged('post_install', '-at_install')
class TestEnergyChartsParser(TransactionCase):
    def payload(self, rows):
        return {
            'endpoint': 'price', 'bidding_zone': 'DE-LU', 'unit': 'EUR / MWh',
            'interval_minutes': 15, 'deprecated': False,
            'license': 'CC BY 4.0 from Bundesnetzagentur | SMARD.de',
            'generated_at': '2026-10-25T12:00:00+01:00', 'data': rows,
        }

    def row(self, timestamp, price):
        return {'timestamp': timestamp, 'values': {'day_ahead_price': price}}

    def test_dst_offsets_and_negative_prices(self):
        result = parse_prices(self.payload([
            self.row('2026-10-25T02:00:00+02:00', -4.5),
            self.row('2026-10-25T02:00:00+01:00', 0),
        ]))
        self.assertEqual(result['slots'], [
            (datetime(2026, 10, 25, 0, 0), -4.5),
            (datetime(2026, 10, 25, 1, 0), 0.0),
        ])

    def test_missing_prices_are_not_replaced_with_zero(self):
        result = parse_prices(self.payload([
            self.row('2026-11-12T00:00:00+01:00', None),
            self.row('2026-11-12T00:15:00+01:00', 12),
        ]))
        self.assertEqual(result['missing'], 1)
        self.assertEqual(len(result['slots']), 1)

    def test_rejects_bad_data_and_unverified_licenses(self):
        payload = self.payload([self.row('2026-11-12T00:00:00+01:00', 12)])
        for field, value in [('bidding_zone', 'FR'), ('unit', 'EUR/kWh'), ('unit', None),
                             ('interval_minutes', 60), ('license', 'private use only')]:
            invalid = dict(payload, **{field: value})
            with self.subTest(field=field), self.assertRaises(PriceDataError):
                parse_prices(invalid)

    def test_rejects_duplicate_and_naive_timestamps(self):
        row = self.row('2026-11-12T00:00:00+01:00', 12)
        with self.assertRaises(PriceDataError):
            parse_prices(self.payload([row, row]))
        with self.assertRaises(PriceDataError):
            parse_prices(self.payload([self.row('2026-11-12T00:00:00', 12)]))

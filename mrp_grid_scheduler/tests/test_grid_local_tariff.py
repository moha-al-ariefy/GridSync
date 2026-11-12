import base64
from collections import Counter
from datetime import datetime

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..services.local_tariffs import expand_profile


@tagged('post_install', '-at_install')
class TestGridLocalTariff(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.manager = new_test_user(
            cls.env, login='local_tariff_manager', groups='mrp_grid_scheduler.group_grid_manager',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])],
        )
        cls.planner = new_test_user(
            cls.env, login='local_tariff_planner', groups='mrp_grid_scheduler.group_grid_planner',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])],
        )
        cls.sources = cls.env['grid.tariff.source'].with_user(cls.manager)
        cls.wizards = cls.env['grid.tariff.load'].with_user(cls.manager)

    def test_published_taqa_week_and_repeat_generation(self):
        source = self.sources.create({
            'name': 'TAQA 2025 historical tariff', 'source_type': 'manual_tou',
            'source_reference': 'https://www.addc.ae/en-US/business/Documents/2025%20Tariff%20(English).pdf',
            'currency_id': self.env.ref('base.AED').id,
            'timezone': 'Asia/Dubai', 'valid_from': '2025-01-01', 'valid_to': '2025-12-31',
            'rule_ids': [
                Command.create({'start_time': '00:00', 'end_time': '10:00', 'price_per_kwh': 0.27}),
                Command.create({'start_time': '10:00', 'end_time': '22:00', 'price_per_kwh': 0.366,
                                'date_from': '2025-06-01', 'date_to': '2025-09-30'}),
                Command.create({'start_time': '22:00', 'end_time': '24:00', 'price_per_kwh': 0.27}),
                Command.create({'start_time': '10:00', 'end_time': '22:00', 'price_per_kwh': 0.27,
                                'date_from': '2025-01-01', 'date_to': '2025-05-31'}),
                Command.create({'start_time': '10:00', 'end_time': '22:00', 'price_per_kwh': 0.27,
                                'date_from': '2025-10-01', 'date_to': '2025-12-31'}),
            ],
        })
        wizard = self.wizards.create({
            'source_id': source.id, 'first_day': '2025-06-02', 'last_day': '2025-06-08',
        })
        wizard.action_load()
        self.assertEqual(Counter(source.slot_ids.sorted('start_utc').mapped('price_per_mwh')), {270: 336, 366: 336})
        prices = {slot.start_utc: slot.price_per_mwh for slot in source.slot_ids}
        self.assertEqual(prices[datetime(2025, 6, 2, 5, 45)], 270)
        self.assertEqual(prices[datetime(2025, 6, 2, 6, 0)], 366)
        self.assertEqual(prices[datetime(2025, 6, 2, 18, 0)], 270)
        wizard.action_load()
        self.assertEqual(len(source.slot_ids), 672)
        last_load = source.sync_ids.sorted('id', reverse=True)[0]
        self.assertEqual((last_load.inserted, last_load.updated, last_load.missing), (0, 0, 0))
        self.assertFalse(source.profile_dirty)
        source.rule_ids.filtered(lambda rule: rule.price_per_kwh > 0.3).price_per_kwh = 0.4
        self.assertTrue(source.profile_dirty)
        self.assertFalse(source.slot_ids.filtered('is_current'))
        wizard.action_load()
        self.assertEqual(Counter(source.slot_ids.sorted('start_utc').mapped('price_per_mwh')), {270: 336, 400: 336})
        self.assertFalse(source.profile_dirty)
        self.wizards.create({
            'source_id': source.id, 'first_day': '2025-11-13', 'last_day': '2025-11-13',
        }).action_load()
        november = source.slot_ids.filtered(lambda slot: slot.start_utc >= datetime(2025, 11, 12, 20))
        self.assertEqual(Counter(november.mapped('price_per_mwh')), {270: 96})

    def test_profile_overlap_and_uncovered_hours(self):
        source = self.sources.create({
            'name': 'Validation test', 'source_type': 'manual_tou', 'source_reference': 'Test fixture',
            'valid_from': '2025-01-01', 'valid_to': '2025-01-02',
            'rule_ids': [Command.create({'start_time': '00:00', 'end_time': '01:00', 'price_per_kwh': 1})],
        })
        wizard = self.wizards.create({
            'source_id': source.id, 'first_day': '2025-01-01', 'last_day': '2025-01-01',
        })
        wizard.action_load()
        self.assertEqual((len(source.slot_ids), source.sync_ids.sorted('id', reverse=True)[0].missing), (4, 92))
        source.write({'rule_ids': [Command.create({'start_time': '00:45', 'end_time': '01:15', 'price_per_kwh': 1})]})
        with self.assertRaisesRegex(UserError, 'overlap'):
            wizard.action_load()
        self.assertEqual(len(source.slot_ids), 4)

    def test_profile_dst_days_keep_actual_intervals(self):
        rule = {'weekday': 'all', 'date_from': None, 'date_to': None, 'start': 0, 'end': 1440, 'price': 1}
        for day, expected in [(datetime(2026, 3, 29).date(), 92), (datetime(2026, 10, 25).date(), 100)]:
            rows, missing = expand_profile([rule], 'Europe/Berlin', day, day)
            self.assertEqual((len(rows), len({start for start, _ in rows}), missing), (expected, expected, 0))

    def test_csv_validates_before_writes_and_replaces_only_on_request(self):
        source = self.sources.create({
            'name': 'CSV validation test', 'source_type': 'csv', 'source_reference': 'Test fixture',
            'currency_id': self.env.ref('base.EUR').id,
        })
        header = 'start,price_per_mwh,currency\n'
        wizard = self.wizards.create({'source_id': source.id})
        wizard.csv_file = base64.b64encode(
            (header + '2025-01-01T01:00+01:00,-3.5,EUR\n2025-01-01T00:30Z,0,EUR\n').encode(),
        )
        wizard.action_load()
        wizard.action_load()
        self.assertEqual(source.slot_ids.sorted('start_utc').mapped('price_per_mwh'), [-3.5, 0])
        self.assertEqual(source.sync_ids.sorted('id', reverse=True)[0].missing, 1)
        wizard.csv_file = base64.b64encode(
            (header + '2025-01-01T00:00Z,12,EUR\n2025-01-01T00:15Z,1,EUR\n').encode(),
        )
        with self.assertRaisesRegex(UserError, 'different price'):
            wizard.action_load()
        self.assertEqual(source.slot_ids.sorted('start_utc').mapped('price_per_mwh'), [-3.5, 0])
        wizard.replace_existing = True
        wizard.action_load()
        self.assertEqual(source.slot_ids.sorted('start_utc').mapped('price_per_mwh'), [12, 1, 0])
        for bad_row in ('2025-01-01T00:45,1,EUR', '2025-01-01T00:45Z,1,AED', '2025-01-01T02:45+01:60,1,EUR',
                        '2025-01-01T00:00+00:00,1,EUR', '2025-01-01T00:45Z,NaN,EUR'):
            wizard.csv_file = base64.b64encode((header + '2025-01-01T00:00Z,12,EUR\n' + bad_row).encode())
            with self.subTest(bad_row=bad_row), self.assertRaises(UserError):
                wizard.action_load()
            self.assertEqual(len(source.slot_ids), 3)

    def test_local_tariff_access(self):
        other = self.env['res.company'].create({'name': 'Other local tariff company'})
        source = self.env['grid.tariff.source'].create({
            'name': 'Other company CSV', 'source_type': 'csv',
            'source_reference': 'Test fixture', 'company_id': other.id,
        })
        with self.assertRaises(AccessError):
            self.wizards.create({'source_id': source.id}).action_load()
        allowed = self.sources.create({'name': 'Allowed CSV', 'source_type': 'csv', 'source_reference': 'Test fixture'})
        with self.assertRaises(AccessError):
            allowed.with_user(self.planner).action_open_tariff_load()
        with self.assertRaises(AccessError):
            self.env['grid.tariff.slot'].with_user(self.manager).create({
                'source_id': allowed.id, 'start_utc': '2025-01-01 00:00:00', 'price_per_mwh': 1,
            })

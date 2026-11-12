import json
from unittest.mock import MagicMock, patch

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestGridTariff(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env['res.company'].create({'name': 'Other tariff company'})
        cls.manager = new_test_user(
            cls.env, login='tariff_test_manager', groups='mrp_grid_scheduler.group_grid_manager',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])],
        )
        cls.planner = new_test_user(
            cls.env, login='tariff_test_planner', groups='mrp_grid_scheduler.group_grid_planner',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])],
        )
        cls.outsider = new_test_user(cls.env, login='tariff_test_outsider', groups='base.group_user')
        cls.source = cls.env['grid.tariff.source'].with_user(cls.manager).create({
            'name': 'DE test', 'source_type': 'energy_charts', 'timezone': 'Europe/Berlin',
            'currency_id': cls.env.ref('base.EUR').id,
        })

    def test_read_only_roles_and_company_boundary(self):
        other = self.env['grid.tariff.source'].create({
            'name': 'Other company tariff', 'company_id': self.other_company.id,
            'source_reference': 'Development test tariff',
        })
        sources = self.env['grid.tariff.source'].with_user(self.planner).with_context(
            allowed_company_ids=[self.company.id],
        )
        self.assertIn(self.source.id, sources.search([]).ids)
        self.assertNotIn(other.id, sources.search([]).ids)
        with self.assertRaises(AccessError):
            sources.browse(other.id).read(['name'])
        with self.assertRaises(AccessError):
            sources.create({'name': 'Not allowed', 'source_reference': 'test'})
        with self.assertRaises(AccessError):
            self.env['grid.tariff.source'].with_user(self.outsider).search([])
        with self.assertRaises(AccessError):
            self.source.with_user(self.planner).action_sync_energy_charts()
        with self.assertRaises(AccessError):
            self.source.with_user(self.manager).write({'company_id': self.other_company.id})

    def test_live_prices_and_sync_metadata_cannot_be_forged(self):
        slots = self.env['grid.tariff.slot'].with_user(self.manager)
        with self.assertRaises(AccessError):
            slots.create({'source_id': self.source.id, 'start_utc': '2026-11-12 00:00:00', 'price_per_mwh': 5})
        with self.assertRaises(AccessError):
            slots.with_context(default_source_id=self.source.id).create({
                'start_utc': '2026-11-12 00:00:00', 'price_per_mwh': 5,
            })
        with self.assertRaises(AccessError):
            self.source.with_user(self.manager).write({'license_text': 'forged'})
        with self.assertRaises(AccessError):
            self.source.with_user(self.manager).write({'source_reference': 'forged'})

    def test_manual_intervals_need_a_source_and_valid_time(self):
        sources = self.env['grid.tariff.source'].with_user(self.manager)
        with self.assertRaises(ValidationError):
            sources.create({'name': 'Missing reference'})
        source = sources.create({'name': 'Manual tariff', 'source_reference': 'Development tariff document'})
        with self.assertRaises(ValidationError):
            self.env['grid.tariff.slot'].with_user(self.manager).create({
                'source_id': source.id, 'start_utc': '2026-11-12 00:01:00', 'price_per_mwh': 10,
            })

    def test_sync_imports_prices_without_filling_missing_values(self):
        payload = {
            'endpoint': 'price', 'bidding_zone': 'DE-LU', 'unit': 'EUR / MWh',
            'interval_minutes': 15, 'deprecated': False, 'license': 'CC BY 4.0 from SMARD.de',
            'generated_at': '2026-11-12T12:00:00+01:00',
            'data': [
                {'timestamp': '2026-11-12T12:00:00+01:00', 'values': {'day_ahead_price': -3.5}},
                {'timestamp': '2026-11-12T12:15:00+01:00', 'values': {'day_ahead_price': None}},
            ],
        }
        response = MagicMock(status_code=200)
        response.__enter__.return_value = response
        response.iter_content.return_value = [json.dumps(payload).encode()]
        with patch('odoo.addons.mrp_grid_scheduler.models.grid_tariff.requests.get', return_value=response):
            self.source.with_user(self.manager).action_sync_energy_charts()
        slot = self.env['grid.tariff.slot'].search([('source_id', '=', self.source.id)])
        self.assertEqual(len(slot), 1)
        self.assertEqual(slot.price_per_mwh, -3.5)
        log = self.env['grid.tariff.sync'].search([('source_id', '=', self.source.id)])
        self.assertEqual((log.state, log.inserted, log.missing), ('success', 1, 1))
        with self.assertRaises(UserError):
            self.source.with_user(self.manager).action_sync_energy_charts()
        log.sudo().write({'next_allowed_at': '2026-01-01 00:00:00'})
        payload['data'][0]['values']['day_ahead_price'] = None
        payload['data'][1]['values']['day_ahead_price'] = 7
        response.iter_content.return_value = [json.dumps(payload).encode()]
        with patch('odoo.addons.mrp_grid_scheduler.models.grid_tariff.requests.get', return_value=response):
            self.source.with_user(self.manager).action_sync_energy_charts()
        self.assertFalse(slot.is_current)
        self.assertEqual(len(self.env['grid.tariff.slot'].search([('source_id', '=', self.source.id)])), 2)

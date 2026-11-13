import json
from unittest.mock import MagicMock, patch

from requests.exceptions import ConnectionError

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
        other = self.source.sudo().copy({'company_id': self.other_company.id})
        with patch('odoo.addons.mrp_grid_scheduler.models.grid_tariff.requests.get', return_value=response):
            self.assertTrue(self.env['grid.tariff.source'].with_user(self.planner).refresh_energy_charts())
        self.assertFalse(other.sync_ids or other.slot_ids)
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

        source = self.source.with_user(self.manager)
        source.sync_ids.sudo().write({'next_allowed_at': '2026-01-01 00:00:00'})
        for item in payload['data']:
            item['values']['day_ahead_price'] = 9
        response.iter_content.return_value = [json.dumps(payload).encode()]
        with patch('odoo.addons.mrp_grid_scheduler.models.grid_tariff.requests.get', return_value=response):
            source.action_sync_energy_charts()
        self.assertEqual(source.slot_ids.mapped('price_per_mwh'), [9, 9])
        self.assertTrue(all(source.slot_ids.mapped('is_current')))
        self.assertEqual(source.sync_ids.sorted('id')[-1].updated, 2)

        last_sync = source.last_sync_at
        saved = source.slot_ids.sorted('start_utc').mapped('price_per_mwh')
        source.sync_ids.sudo().write({'next_allowed_at': '2026-01-01 00:00:00'})
        with patch('odoo.addons.mrp_grid_scheduler.models.grid_tariff.requests.get', side_effect=ConnectionError):
            source.action_sync_energy_charts()
        self.assertEqual(source.last_sync_at, last_sync)
        self.assertEqual(source.slot_ids.sorted('start_utc').mapped('price_per_mwh'), saved)
        self.assertEqual(source.sync_ids.sorted('id', reverse=True)[0].state, 'failed')

    def test_native_market_sync_keeps_each_markets_prices_separate(self):
        model = self.env['grid.tariff.source'].with_user(self.manager)
        france = model.create({
            'name': 'France day-ahead', 'source_type': 'energy_charts', 'energy_charts_market': 'FR',
        })
        self.assertEqual(france.timezone, 'Europe/Paris')
        self.assertEqual(france.currency_id.name, 'EUR')
        with self.assertRaises(UserError):
            france.write({'energy_charts_market': 'NL'})

        def market_response(_url, *, params, **_kwargs):
            market = params['bzn']
            response = MagicMock(status_code=200)
            response.__enter__.return_value = response
            response.iter_content.return_value = [json.dumps({
                'endpoint': 'price', 'bidding_zone': market, 'unit': 'EUR / MWh',
                'interval_minutes': 15, 'deprecated': False, 'license': 'CC BY 4.0',
                'generated_at': '2026-11-12T12:00:00+01:00',
                'data': [{
                    'timestamp': '2026-11-12T12:00:00+01:00',
                    'values': {'day_ahead_price': {'FR': 22, 'DE-LU': 10}[market]},
                }],
            }).encode()]
            return response

        with patch('odoo.addons.mrp_grid_scheduler.models.grid_tariff.requests.get',
                   side_effect=market_response) as get:
            self.assertTrue(model.refresh_energy_charts())
            self.assertEqual(get.call_count, 2)
        self.assertEqual(france.slot_ids.price_per_mwh, 22)
        self.assertEqual(self.source.slot_ids.price_per_mwh, 10)

    def test_auto_sync_respects_company_and_keeps_manual_sync_available(self):
        self.assertTrue(self.company.grid_auto_sync)
        self.assertTrue(self.other_company.grid_auto_sync)
        other = self.source.sudo().copy({'company_id': self.other_company.id})
        sources = self.env['grid.tariff.source'].with_user(self.planner)
        get_path = 'odoo.addons.mrp_grid_scheduler.models.grid_tariff.requests.get'
        with patch(get_path) as get:
            self.assertFalse(sources.refresh_energy_charts(other.id))
            get.assert_not_called()
            settings = self.env['res.config.settings'].create({
                'company_id': self.company.id, 'grid_auto_sync': False,
            })
            self.assertFalse(settings.company_id.grid_auto_sync)
            self.assertTrue(self.other_company.grid_auto_sync)
            self.assertFalse(sources.refresh_energy_charts())
            get.assert_not_called()
            get.return_value.__enter__.return_value.status_code = 503
            self.source.with_user(self.manager).action_sync_energy_charts()
            get.assert_called_once()
        with self.assertRaises(AccessError):
            sources.with_user(self.outsider).refresh_energy_charts()

    def test_cron_batches_sources_and_page_open_reuses_recent_attempt(self):
        other = self.source.sudo().copy({'company_id': self.other_company.id})
        model = self.env['grid.tariff.source']
        job = self.env.ref('mrp_grid_scheduler.ir_cron_grid_tariff_refresh')
        self.assertTrue(job.active)
        self.assertEqual((job.interval_number, job.interval_type), (5, 'minutes'))
        get_path = 'odoo.addons.mrp_grid_scheduler.models.grid_tariff.requests.get'
        with patch(get_path) as get:
            get.return_value.__enter__.return_value.status_code = 503
            with self.enter_registry_test_mode():
                self.assertIs(job.method_direct_trigger(), True)
            self.assertEqual(len((self.source.sudo() | other).sync_ids), 2)
            self.assertFalse(model.with_user(self.planner).refresh_energy_charts(self.source.id))
            get.assert_called_once()
        (self.source.sudo() | other).sync_ids.write({'next_allowed_at': '2026-01-01 00:00:00'})
        with patch(get_path) as get:
            model._cron_sync_energy_charts()
            get.assert_not_called()

    def test_manual_interval_metadata_stays_tied_to_the_source(self):
        source = self.env['grid.tariff.source'].with_user(self.manager).create({
            'name': 'Manual metadata test', 'source_reference': 'Test fixture',
        })
        slots = self.env['grid.tariff.slot'].with_user(self.manager)
        values = {'source_id': source.id, 'start_utc': '2026-11-12 00:00:00', 'price_per_mwh': 5}
        with self.assertRaises(AccessError):
            slots.create(dict(values, currency_id=self.env.ref('base.EUR').id))
        slot = slots.with_context(default_currency_id=self.env.ref('base.EUR').id,
                                  default_company_id=self.other_company.id).create(values)
        self.assertEqual(slot.company_id, source.company_id)
        self.assertEqual(slot.currency_id, source.currency_id)
        with self.assertRaises(AccessError):
            slot.write({'company_id': self.other_company.id})

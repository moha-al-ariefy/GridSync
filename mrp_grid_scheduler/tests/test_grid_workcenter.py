from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestGridWorkcenter(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env['res.company'].create({'name': 'Other work center company'})
        companies = {
            'company_id': cls.company.id,
            'company_ids': [Command.set([cls.company.id, cls.other_company.id])],
        }
        cls.manager = new_test_user(
            cls.env, login='grid_energy_manager',
            groups='mrp_grid_scheduler.group_grid_manager,mrp.group_mrp_manager', **companies,
        )
        cls.planner = new_test_user(
            cls.env, login='grid_energy_planner',
            groups='mrp_grid_scheduler.group_grid_planner,mrp.group_mrp_manager', **companies,
        )
        cls.workcenters = cls.env['mrp.workcenter'].with_user(cls.manager).with_context(
            allowed_company_ids=[cls.company.id, cls.other_company.id],
        )
        cls.center = cls.workcenters.create({'name': 'Test work center', 'company_id': cls.company.id})
        cls.sources = cls.env['grid.tariff.source'].with_env(cls.workcenters.env)
        cls.tariff = cls.sources.create({
            'name': 'Test company tariff', 'source_reference': 'Test fixture', 'company_id': cls.company.id,
        })
        cls.other_tariff = cls.sources.create({
            'name': 'Other company tariff', 'source_reference': 'Test fixture', 'company_id': cls.other_company.id,
        })

    def test_energy_settings_require_an_active_same_company_tariff(self):
        self.assertFalse(self.center.grid_allow_shift)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.center.write({'grid_allow_shift': True})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.center.write({'grid_power_kw': -1})
        with self.assertRaises(UserError), self.env.cr.savepoint():
            self.center.write({'grid_tariff_source_id': self.other_tariff.id})
        self.center.write({
            'grid_tariff_source_id': self.tariff.id, 'grid_power_kw': 15.5,
            'grid_power_reference': 'Test equipment specification', 'grid_allow_shift': True,
        })
        self.assertEqual(self.center.grid_tariff_source_id, self.tariff)
        self.assertTrue(self.center.grid_allow_shift)
        with self.assertRaises(UserError), self.env.cr.savepoint():
            self.tariff.write({'company_id': self.other_company.id})
        self.assertEqual(self.tariff.company_id, self.company)

    def test_grid_planner_can_read_but_cannot_change_energy_settings(self):
        center = self.center.with_user(self.planner)
        self.assertFalse(center.grid_energy_can_edit)
        center.write({'note': 'Ordinary manufacturing edit'})
        with self.assertRaises(AccessError):
            center.write({'grid_power_kw': 5})
        with self.assertRaises(AccessError):
            center.with_context(default_grid_power_kw=5).create({'name': 'Test work center copy'})

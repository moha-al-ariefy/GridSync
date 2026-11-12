from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestGridSite(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.company
        cls.company_b = cls.env['res.company'].create({'name': 'Other company'})
        cls.planner = new_test_user(
            cls.env, login='grid_test_planner',
            groups='mrp_grid_scheduler.group_grid_planner',
            company_id=cls.company_a.id,
            company_ids=[Command.set([cls.company_a.id])],
        )
        cls.outsider = new_test_user(cls.env, login='grid_test_outsider', groups='base.group_user')
        cls.site_a = cls.env['grid.site'].create({'name': 'Factory A', 'company_id': cls.company_a.id})
        cls.site_b = cls.env['grid.site'].create({'name': 'Factory B', 'company_id': cls.company_b.id})

    def test_planner_can_create_and_update_site(self):
        sites = self.env['grid.site'].with_user(self.planner)
        site = sites.create({'name': 'Another factory'})
        site.write({'name': 'Updated factory'})
        self.assertEqual(site.company_id, self.company_a)
        self.assertEqual(site.name, 'Updated factory')
        with self.assertRaises(AccessError):
            site.unlink()

    def test_non_planner_cannot_read_or_create(self):
        sites = self.env['grid.site'].with_user(self.outsider)
        with self.assertRaises(AccessError):
            sites.search([])
        with self.assertRaises(AccessError):
            sites.create({'name': 'Not allowed'})

    def test_company_rule_blocks_cross_company_access(self):
        sites = self.env['grid.site'].with_user(self.planner).with_context(
            allowed_company_ids=[self.company_a.id],
        )
        self.assertIn(self.site_a.id, sites.search([]).ids)
        self.assertNotIn(self.site_b.id, sites.search([]).ids)
        with self.assertRaises(AccessError):
            sites.browse(self.site_b.id).read(['name'])
        with self.assertRaises(AccessError):
            sites.browse(self.site_b.id).write({'name': 'Not allowed'})
        with self.assertRaises(AccessError):
            sites.create({'name': 'Wrong company', 'company_id': self.company_b.id})

    def test_manager_can_delete_site(self):
        manager = new_test_user(
            self.env, login='grid_test_manager',
            groups='mrp_grid_scheduler.group_grid_manager',
            company_id=self.company_a.id,
            company_ids=[Command.set([self.company_a.id])],
        )
        site = self.env['grid.site'].with_user(manager).create({'name': 'Temporary site'})
        site.unlink()
        self.assertFalse(site.exists())

    def test_foreign_company_workcenter_cannot_be_linked(self):
        workcenter = self.env['mrp.workcenter'].with_company(self.company_b).create({
            'name': 'Other work center', 'company_id': self.company_b.id,
        })
        with self.assertRaises(UserError):
            self.site_a.write({'workcenter_ids': [Command.link(workcenter.id)]})

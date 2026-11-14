import base64
import csv
import io
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..services.scheduler import load_inputs, schedule
from ..services.meter_readings import parse_meter_csv, price_readings


DAY = datetime(2030, 1, 7)  # A Monday in the future, so "now" never limits these tests.


def at(hours, minutes=0, day=DAY):
    return day + timedelta(hours=hours, minutes=minutes)


@tagged('post_install', '-at_install')
class TestGridSchedule(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env['res.company'].create({'name': 'Other schedule company'})
        cls.manager = new_test_user(
            cls.env, login='grid_schedule_manager',
            groups='mrp_grid_scheduler.group_grid_manager,mrp.group_mrp_manager',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])],
        )
        cls.planner = new_test_user(
            cls.env, login='grid_schedule_planner', groups='mrp_grid_scheduler.group_grid_planner',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])],
        )
        cls.outsider = new_test_user(
            cls.env, login='grid_schedule_outsider', groups='mrp_grid_scheduler.group_grid_planner',
            company_id=cls.other_company.id, company_ids=[Command.set([cls.other_company.id])],
        )
        cls.calendar = cls.make_calendar('GridSync test shifts', 'UTC', [(6, 12), (13, 19)])
        cls.tariff = cls.env['grid.tariff.source'].with_user(cls.manager).create({
            'name': 'Schedule test tariff', 'source_reference': 'Test fixture', 'currency_id': cls.env.ref('base.EUR').id,
            'timezone': 'UTC',
        })
        cls.add_prices(DAY, 100, {at(13) + timedelta(minutes=15 * index): 10 for index in range(8)})
        cls.center = cls.env['mrp.workcenter'].with_user(cls.manager).create({
            'name': 'Schedule test press', 'company_id': cls.company.id, 'resource_calendar_id': cls.calendar.id,
            'grid_power_kw': 20, 'grid_power_reference': 'Test fixture', 'grid_tariff_source_id': cls.tariff.id,
            'grid_allow_shift': True,
        })
        cls.other_center = cls.env['mrp.workcenter'].create({
            'name': 'Schedule test manual bench', 'company_id': cls.company.id, 'resource_calendar_id': cls.calendar.id,
        })
        cls.product = cls.env['product.product'].create({'name': 'Schedule test product'})

    @classmethod
    def make_calendar(cls, name, tz, hours):
        return cls.env['resource.calendar'].create({
            'name': name, 'tz': tz, 'company_id': cls.company.id,
            'attendance_ids': [Command.clear()] + [
                Command.create({'name': f'{day} {start}', 'dayofweek': str(day), 'hour_from': start, 'hour_to': end,
                                'day_period': 'morning' if start < 12 else 'afternoon'})
                for day in range(7) for start, end in hours
            ],
        })

    @classmethod
    def add_prices(cls, day, default, overrides=None, tariff=None):
        overrides = overrides or {}
        cls.env['grid.tariff.slot'].sudo().create([{
            'source_id': (tariff or cls.tariff).id, 'start_utc': day + timedelta(minutes=15 * index),
            'price_per_mwh': overrides.get(day + timedelta(minutes=15 * index), default),
        } for index in range(96)])

    def make_production(self, operations, deadline=None):
        production = self.env['mrp.production'].create({
            'product_id': self.product.id, 'product_qty': 1, 'product_uom_id': self.product.uom_id.id,
            'workorder_ids': [Command.create({
                'name': name, 'workcenter_id': center.id, 'product_uom_id': self.product.uom_id.id,
                'duration_expected': minutes,
            }) for name, center, minutes in operations],
        })
        production.action_confirm()
        if deadline:
            production.date_deadline = deadline
        return production, production.workorder_ids.sorted(lambda workorder: (workorder.sequence, workorder.id))

    def make_run(self, day=DAY, user=None, centers=None):
        return self.env['grid.schedule.run'].with_user(user or self.planner).create({
            'name': 'Test proposal', 'date_from': day, 'date_to': day + timedelta(days=1),
            'workcenter_ids': [Command.set((centers or self.center).ids)],
        })

    def test_proposal_moves_operations_and_keeps_order(self):
        _production, (cut, finish) = self.make_production([('Cut', self.center, 60), ('Finish', self.center, 30)])
        self.assertEqual(finish.blocked_by_workorder_ids, cut)
        cut.write({'date_start': at(8), 'date_finished': at(9)})
        finish.write({'date_start': at(9), 'date_finished': at(9, 30)})
        self.env['resource.calendar.leaves'].create({
            'name': 'Test maintenance', 'calendar_id': self.calendar.id, 'resource_id': self.center.resource_id.id,
            'date_from': at(13), 'date_to': at(13, 30), 'time_type': 'other',
        })
        run = self.make_run()
        run.action_compute()
        self.assertEqual(run.state, 'proposed')
        lines = {line.workorder_id: line for line in run.line_ids}
        self.assertEqual((lines[cut].proposed_start, lines[cut].proposed_end), (at(13, 30), at(14, 30)))
        self.assertEqual((lines[finish].proposed_start, lines[finish].proposed_end), (at(14, 30), at(15)))
        self.assertEqual({line.status for line in run.line_ids}, {'moved'})
        self.assertEqual(lines[cut].reason_code, 'cheaper')
        self.assertAlmostEqual(lines[cut].proposed_cost, 20 * 1 * 10 / 1000)
        self.assertAlmostEqual(lines[cut].energy_kwh, 20)
        self.assertAlmostEqual(run.proposed_cost, 0.3)
        self.assertAlmostEqual(run.current_cost, 3.0)
        self.assertAlmostEqual(run.saving, 2.7)
        self.assertTrue(run.complete and run.has_lower_bound)
        self.assertAlmostEqual(run.lower_bound, 0.3)
        self.assertEqual(run.currency_id, self.env.ref('base.EUR'))
        # The proposal does not change work orders.
        self.assertEqual((cut.date_start, finish.date_start), (at(8), at(9)))
        # The saved snapshot reproduces the same proposal.
        replay = schedule(*load_inputs(run.snapshot['inputs']))
        self.assertEqual({str(key): item.proposed.start.isoformat() for key, item in replay.jobs.items()},
                         {key: item['start'] for key, item in run.snapshot['result'].items()})

    def test_shift_penalty_is_separate_and_rechecked_before_apply(self):
        _production, (order,) = self.make_production([('Press', self.center, 60)])
        order.write({'date_start': at(8), 'date_finished': at(9)})
        self.center.with_user(self.manager).grid_shift_penalty = 0.2

        run = self.make_run()
        run.action_compute()
        self.assertEqual(run.line_ids.proposed_start, at(13))
        self.assertAlmostEqual(run.proposed_cost, 0.2)
        self.assertAlmostEqual(run.shift_penalty, 1)
        self.assertAlmostEqual(run.saving, 1.8)
        self.assertFalse(run.has_lower_bound)
        self.assertEqual(run.snapshot['result'][str(order.id)]['objective_score'], 1.2)

        self.center.with_user(self.manager).grid_shift_penalty = 0.5
        with self.assertRaisesRegex(UserError, 'Shift penalties'):
            run._change_schedule('apply')
        self.assertEqual(order.date_start, at(8))

        run = self.make_run()
        run.action_compute()
        self.assertEqual(run.line_ids.proposed_start, at(8))
        self.assertEqual(run.shift_penalty, 0)

    def test_gb_carbon_proposal_uses_intensity_and_rechecks_the_source(self):
        _production, (order,) = self.make_production([('Press', self.center, 30)])
        order.write({'date_start': at(8), 'date_finished': at(8, 30)})
        run = self.make_run()
        run.objective = 'gb_carbon'
        with self.assertRaisesRegex(UserError, 'Great Britain'):
            run.action_compute()
        self.center.sudo().write({'grid_use_gb_carbon': True, 'grid_tariff_source_id': False})
        day = self.env['grid.carbon.day'].sudo().create({'day': DAY.date(), 'company_id': self.company.id})
        intervals = self.env['grid.carbon.interval'].sudo().create([
            {'day_id': day.id, 'start_utc': at(8), 'actual': 400, 'has_actual': True},
            {'day_id': day.id, 'start_utc': at(13), 'forecast': 100, 'has_forecast': True},
        ])
        run.action_compute()
        self.assertTrue(run.complete)
        self.assertEqual(run.proposed_carbon_kg, 1)
        self.assertEqual(run.current_carbon_kg, 4)
        self.assertEqual(run.proposed_cost, 0)
        self.assertEqual(run.line_ids.proposed_start, at(13))
        with self.assertRaisesRegex(UserError, 'Start a new run'):
            run.objective = 'cost'
        intervals[1].write({'actual': 100, 'has_actual': True})
        with self.assertRaisesRegex(UserError, 'Carbon intensity'):
            run._change_schedule('apply')
        self.assertEqual(order.date_start, at(8))
        intervals[1].has_actual = False
        run._change_schedule('apply')
        self.assertEqual(order.date_start, at(13))
        run._change_schedule('revert')
        self.assertEqual(order.date_start, at(8))

    def test_combined_objective_reports_cost_and_carbon_separately(self):
        self.company.currency_id = self.tariff.currency_id
        self.center.sudo().grid_use_gb_carbon = True
        _production, (order,) = self.make_production([('Press', self.center, 30)])
        order.write({'date_start': at(8), 'date_finished': at(8, 30)})
        day = self.env['grid.carbon.day'].sudo().create({'day': DAY.date(), 'company_id': self.company.id})
        self.env['grid.carbon.interval'].sudo().create([
            {'day_id': day.id, 'start_utc': at(8), 'actual': 0, 'has_actual': True},
            {'day_id': day.id, 'start_utc': at(13), 'forecast': 400, 'has_forecast': True},
        ])
        run = self.make_run()
        run.write({'objective': 'gb_combined', 'carbon_price': 1000})
        run.action_compute()
        self.assertTrue(run.complete)
        self.assertEqual(run.line_ids.proposed_start, at(8))
        self.assertAlmostEqual(run.proposed_cost, 1)
        self.assertEqual(run.proposed_carbon_kg, 0)
        self.assertEqual(run.snapshot['carbon_price'], 1000)

    def test_queue_setting_is_company_specific_and_admin_only(self):
        self.assertEqual(self.company.grid_queue_threshold, 500)
        self.env['res.config.settings'].create({
            'company_id': self.company.id, 'grid_queue_threshold': 1,
        })
        self.assertEqual(self.company.grid_queue_threshold, 1)
        self.assertEqual(self.other_company.grid_queue_threshold, 500)
        with self.assertRaises(AccessError):
            self.company.with_user(self.planner).write({'grid_queue_threshold': 0})
        self.make_production([('Press', self.center, 30)])
        run = self.make_run()
        run.action_compute()
        self.assertEqual(run.state, 'proposed')

    def test_queue_computes_without_applying(self):
        _production, (order,) = self.make_production([('Press', self.center, 30)])
        order.write({'date_start': at(8), 'date_finished': at(8, 30)})
        run = self.make_run()
        self.company.sudo().grid_queue_threshold = 0
        run.action_compute()
        self.assertEqual(run.state, 'queued')
        self.assertEqual(run.queued_by, self.planner)
        self.assertFalse(run.line_ids)
        with self.assertRaises(UserError):
            run.action_compute()
        with self.assertRaises(UserError):
            run.date_to = at(16)
        cron = self.env.ref('mrp_grid_scheduler.ir_cron_grid_schedule_queue')
        with self.enter_registry_test_mode():
            self.assertIs(cron.method_direct_trigger(), True)
        self.assertEqual(run.state, 'proposed')
        self.assertEqual(run.line_ids.proposed_start, at(13))
        self.assertEqual(order.date_start, at(8))

    def test_queue_stops_when_the_planner_loses_access(self):
        self.make_production([('Press', self.center, 30)])
        run = self.make_run()
        self.company.sudo().grid_queue_threshold = 0
        run.action_compute()
        self.assertEqual(run.state, 'queued')
        self.planner.sudo().active = False
        run.sudo()._compute_queued_proposal()
        self.assertEqual(run.sudo().state, 'failed')
        self.assertIn('no longer has access', run.sudo().compute_error)
        self.assertFalse(run.sudo().line_ids)

    def test_apply_dialog_warns_when_the_saved_market_refresh_ages(self):
        source = self.env['grid.tariff.source'].with_user(self.manager).create({
            'name': 'Market refresh test', 'source_type': 'energy_charts',
        })
        self.add_prices(DAY, 100, {at(13): 10, at(13, 15): 10}, tariff=source)
        self.center.with_user(self.manager).grid_tariff_source_id = source
        self.make_production([('Press', self.center, 30)])
        now = at(0) - timedelta(hours=1)
        source.sudo().last_sync_at = now
        run = self.make_run()
        with patch.object(fields.Datetime, 'now', return_value=now):
            run.action_compute()
            self.assertFalse(run.price_refresh_warning)

        source.sudo().last_sync_at = now + timedelta(minutes=16)
        run.invalidate_recordset(['price_refresh_warning'])
        with patch.object(fields.Datetime, 'now', return_value=now + timedelta(minutes=16)):
            self.assertTrue(run.price_refresh_warning)
            action = run.action_review_apply()
            dialog = self.env['grid.schedule.confirm'].with_user(self.planner).with_context(action['context']).create({})
            self.assertTrue(dialog.price_refresh_warning)

    def test_window_days_and_times_set_the_period(self):
        form = Form(self.env['grid.schedule.run'].with_user(self.planner).with_context(tz='Asia/Dubai'))
        form.name = 'Three-day window'
        form.window_start_date = DAY.date()
        form.window_end_date = DAY.date() + timedelta(days=2)
        form.window_start_time = '06:00'
        form.window_end_time = '24:00'
        form.workcenter_ids.add(self.center)
        run = form.save()
        # Dubai is UTC+4: 06:00 on the first day and midnight after the last day.
        self.assertEqual(run.date_from, DAY + timedelta(hours=2))
        self.assertEqual(run.date_to, DAY + timedelta(days=3, hours=-4))
        run = run.with_context(tz='Asia/Dubai')
        self.assertEqual((run.window_start_date, run.window_start_time), (DAY.date(), '06:00'))
        self.assertEqual((run.window_end_date, run.window_end_time), (DAY.date() + timedelta(days=2), '24:00'))

    def test_cost_analysis_compares_like_for_like(self):
        _production, (cut, finish) = self.make_production([('Cut', self.center, 60), ('Finish', self.center, 30)])
        cut.write({'date_start': at(8), 'date_finished': at(9)})
        finish.write({'date_start': at(9), 'date_finished': at(9, 30)})
        self.make_production([('Unplanned', self.center, 15)])
        run = self.make_run()
        run.action_compute()
        compared = run.line_ids.filtered('has_current_cost')
        self.assertEqual((run.job_count, run.compared_count), (3, 2))
        self.assertAlmostEqual(run.compared_current_cost, 3.0)
        self.assertAlmostEqual(run.compared_proposed_cost, 0.3)
        self.assertAlmostEqual(run.compared_saving, 2.7)
        self.assertAlmostEqual(run.saving_percent, 90.0)
        self.assertAlmostEqual(run.current_price_kwh, 3.0 / 30)
        self.assertAlmostEqual(sum(compared.mapped('saving')), 2.7)
        self.assertFalse((run.line_ids - compared).saving)
        self.assertIn('Schedule test press', run.cost_breakdown)
        blank = self.env['grid.schedule.run'].with_user(self.planner).new({})
        self.assertFalse(blank.name or blank.date_from or blank.date_to or blank.workcenter_ids)

    def test_flat_tariff_keeps_bookings_and_reports_zero_savings(self):
        self.tariff.slot_ids.sudo().write({'price_per_mwh': 270})
        _production, (operation,) = self.make_production([('Press', self.center, 60)])
        operation.write({'date_start': at(8), 'date_finished': at(9)})
        run = self.make_run()
        run.action_compute()
        self.assertTrue(run.complete and run.has_current_cost)
        self.assertEqual((run.moved_count, run.saving, run.line_ids.proposed_start), (0, 0, at(8)))
        self.assertAlmostEqual(run.proposed_cost, 5.4)

    def test_shared_calendar_respects_the_proposal_company(self):
        self.calendar.company_id = False
        self.planner.sudo().company_ids = [Command.link(self.other_company.id)]
        leaves = self.env['resource.calendar.leaves']
        for time_type in ('leave', 'other'):
            other_booking = leaves.with_company(self.other_company).create({
                'name': 'Other company booking', 'calendar_id': self.calendar.id,
                'company_id': self.other_company.id,
                'resource_id': self.center.resource_id.id, 'time_type': time_type,
                'date_from': at(13), 'date_to': at(14),
            })
            self.assertEqual(other_booking.company_id, self.other_company)
        leaves.with_company(self.company).create({
            'name': 'This company time off', 'calendar_id': self.calendar.id,
            'resource_id': self.center.resource_id.id, 'time_type': 'leave',
            'date_from': at(13, 30), 'date_to': at(14, 30),
        })
        self.make_production([('Press', self.center, 30)])
        run = self.make_run().with_context(allowed_company_ids=[self.company.id, self.other_company.id])
        run.action_compute()
        self.assertEqual((run.line_ids.proposed_start, run.line_ids.proposed_end), (at(13), at(13, 30)))

    def test_deadlines_missing_prices_and_outside_dependencies(self):
        self.env['grid.tariff.slot'].sudo().search([
            ('source_id', '=', self.tariff.id), ('start_utc', '>=', at(6)), ('start_utc', '<', at(8)),
        ]).unlink()
        _production, (on_time,) = self.make_production([('Due at ten', self.center, 60)], deadline=at(10))
        _production, (too_soon,) = self.make_production([('Due at seven', self.center, 60)], deadline=at(7))
        _production, (manual, pressed) = self.make_production([
            ('Unplanned manual step', self.other_center, 30), ('Pressed after manual step', self.center, 30),
        ])
        run = self.make_run()
        run.action_compute()
        lines = {line.workorder_id: line for line in run.line_ids}
        self.assertNotIn(manual, lines)
        self.assertEqual((lines[on_time].status, lines[on_time].reason_code), ('moved', 'unplanned'))
        self.assertEqual((lines[on_time].proposed_start, lines[on_time].proposed_end), (at(8), at(9)))
        self.assertEqual((lines[too_soon].status, lines[too_soon].reason_code), ('unscheduled', 'no_prices'))
        self.assertEqual((lines[pressed].status, lines[pressed].reason_code), ('unscheduled', 'external_unplanned'))
        self.assertFalse(lines[too_soon].proposed_start)
        self.assertFalse(run.complete or run.has_lower_bound or run.has_current_cost)
        self.assertEqual((run.job_count, run.moved_count, run.unscheduled_count), (3, 1, 2))
        self.assertIn('88 of 96 price intervals', run.data_notes)

    def test_partial_apply_and_revert_only_change_the_reviewed_moves(self):
        self.env['grid.tariff.slot'].sudo().search([
            ('source_id', '=', self.tariff.id), ('start_utc', '>=', at(6)), ('start_utc', '<', at(8)),
        ]).unlink()
        _production, (moved, finish) = self.make_production([
            ('Move to the cheap window', self.center, 30),
            ('Keep the booked finish', self.center, 30),
        ])
        moved.write({'date_start': at(9), 'date_finished': at(9, 30)})
        finish.write({'date_start': at(14), 'date_finished': at(14, 30)})
        _production, (retained,) = self.make_production([
            ('Keep the booking without prices', self.center, 30),
        ], deadline=at(7))
        retained.write({'date_start': at(6), 'date_finished': at(6, 30)})
        _production, (manual, skipped) = self.make_production([
            ('Unplanned manual step', self.other_center, 30), ('Wait for manual step', self.center, 30),
        ])

        run = self.make_run()
        run.action_compute()
        lines = {line.workorder_id: line for line in run.line_ids}
        self.assertEqual([lines[order].status for order in (moved, finish, retained, skipped)],
                         ['moved', 'unchanged', 'unscheduled', 'unscheduled'])
        self.assertFalse(run.complete)
        self.assertEqual(run.action_review_apply()['target'], 'new')
        untouched = run._snapshot_workorders(finish | retained | skipped | manual)
        run._change_schedule('apply')
        self.assertEqual(run.state, 'applied')
        self.assertEqual((moved.date_start, moved.leave_id.date_to), (at(13), at(13, 30)))
        self.assertEqual(run._snapshot_workorders(finish | retained | skipped | manual), untouched)

        run._change_schedule('revert')
        self.assertEqual(run.state, 'reverted')
        self.assertEqual((moved.date_start, moved.leave_id.date_to), (at(9), at(9, 30)))
        self.assertEqual(run._snapshot_workorders(finish | retained | skipped | manual), untouched)

    def test_working_hours_follow_daylight_saving_time(self):
        day = datetime(2030, 3, 31)  # Germany moves to summer time on this date.
        calendar = self.make_calendar('GridSync Berlin mornings', 'Europe/Berlin', [(8, 12)])
        tariff = self.env['grid.tariff.source'].with_user(self.manager).create({
            'name': 'Schedule DST tariff', 'source_reference': 'Test fixture',
            'currency_id': self.env.ref('base.EUR').id, 'timezone': 'Europe/Berlin',
        })
        self.add_prices(day, 50, tariff=tariff)
        center = self.env['mrp.workcenter'].with_user(self.manager).create({
            'name': 'Schedule DST press', 'company_id': self.company.id, 'resource_calendar_id': calendar.id,
            'grid_power_kw': 4, 'grid_tariff_source_id': tariff.id, 'grid_allow_shift': True,
        })
        _production, (operation,) = self.make_production([('Morning run', center, 240)])
        run = self.make_run(day=day, centers=center)
        run.action_compute()
        self.assertEqual((run.line_ids.proposed_start, run.line_ids.proposed_end), (at(6, day=day), at(10, day=day)))
        self.assertAlmostEqual(run.line_ids.proposed_cost, 4 * 4 * 50 / 1000)

    def test_access_company_and_inputs(self):
        self.make_production([('Access check', self.center, 15)])
        run = self.make_run()
        with self.assertRaises(AccessError):
            run.write({'proposed_cost': 1})
        with self.assertRaises(AccessError):
            self.env['grid.schedule.run'].with_user(self.planner).create({'name': 'Forged', 'state': 'proposed'})
        run.action_compute()
        with self.assertRaises(UserError):
            run.action_compute()
        with self.assertRaises(UserError):
            run.write({'date_to': at(12)})
        with self.assertRaises(AccessError):
            run.unlink()
        self.assertEqual(len(run.line_ids), 1)
        with self.assertRaises(AccessError):
            run.line_ids.write({'reason': 'Edited'})
        with self.assertRaises(AccessError):
            self.env['grid.schedule.line'].with_user(self.planner).create({'run_id': run.id, 'status': 'moved'})
        self.assertFalse(self.env['grid.schedule.run'].with_user(self.outsider).search([('id', '=', run.id)]))
        copy = self.env['grid.schedule.run'].browse(run.action_new_run()['res_id']).with_user(self.planner)
        self.assertEqual((copy.state, copy.workcenter_ids), ('draft', self.center))
        copy.with_user(self.planner).write({'workcenter_ids': [Command.set(self.other_center.ids)]})
        with self.assertRaisesRegex(UserError, 'schedule suggestions'):
            copy.action_compute()


    def test_apply_and_revert_restore_the_same_workorder_bookings(self):
        production, (cut, finish) = self.make_production([('Cut', self.center, 60), ('Finish', self.center, 30)])
        cut.write({'date_start': at(8), 'date_finished': at(9)})
        finish.write({'date_start': at(9), 'date_finished': at(9, 30)})
        original_production = (production.date_start, production.date_finished)
        run = self.make_run()
        run.action_compute()
        self.assertEqual(run.action_review_apply()['target'], 'new')
        self.assertEqual((cut.date_start, finish.date_start), (at(8), at(9)))
        lines = {line.workorder_id.id: line for line in run.line_ids}
        original_bookings = (cut.leave_id.id, finish.leave_id.id)
        run._change_schedule('apply')
        self.assertEqual(run.state, 'applied')
        self.assertEqual(run.applied_by, self.planner)
        with self.assertRaisesRegex(UserError, 'Choose a proposal with changes'):
            run._change_schedule('apply')
        for operation in cut | finish:
            line = lines[operation.id]
            self.assertEqual((operation.date_start, operation.date_finished), (line.proposed_start, line.proposed_end))
            self.assertEqual((operation.leave_id.date_from, operation.leave_id.date_to),
                             (line.proposed_start, line.proposed_end))
        self.assertEqual((cut.leave_id.id, finish.leave_id.id), original_bookings)
        self.assertEqual(run.action_review_revert()['target'], 'new')
        run._change_schedule('revert')
        self.assertEqual(run.state, 'reverted')
        self.assertEqual(run.reverted_by, self.planner)
        with self.assertRaisesRegex(UserError, 'Only an applied proposal'):
            run._change_schedule('revert')
        self.assertEqual((cut.date_start, cut.date_finished, finish.date_start, finish.date_finished),
                         (at(8), at(9), at(9), at(9, 30)))
        self.assertEqual((production.date_start, production.date_finished), original_production)
        self.assertEqual((cut.leave_id.id, finish.leave_id.id), original_bookings)

    def test_revert_clears_booking_created_for_unplanned_order(self):
        production, (operation,) = self.make_production([('Unplanned', self.center, 30)])
        self.assertFalse(operation.leave_id)
        original_dates = (production.date_start, production.date_finished)
        run = self.make_run()
        run.action_compute()
        run._change_schedule('apply')
        booking = operation.leave_id
        self.assertTrue(booking)
        run._change_schedule('revert')
        self.assertFalse(booking.exists())
        self.assertFalse(operation.leave_id or operation.date_start or operation.date_finished)
        self.assertEqual((production.date_start, production.date_finished), original_dates)

    def test_apply_refuses_an_edit_or_new_booking(self):
        _production, (operation,) = self.make_production([('Cut', self.center, 30)])
        run = self.make_run()
        run.action_compute()
        operation.name = 'Edited after review'
        with self.assertRaisesRegex(UserError, 'changed'):
            run._change_schedule('apply')
        self.assertFalse(operation.date_start)
        run = self.make_run()
        run.action_compute()
        self.env['resource.calendar.leaves'].create({
            'name': 'New maintenance', 'calendar_id': self.calendar.id, 'resource_id': self.center.resource_id.id,
            'date_from': run.line_ids.proposed_start, 'date_to': run.line_ids.proposed_end, 'time_type': 'other',
        })
        with self.assertRaisesRegex(UserError, 'overlaps'):
            run._change_schedule('apply')
        self.assertEqual(run.state, 'proposed')
        self.assertFalse(operation.date_start)

    def test_apply_and_revert_allow_unrelated_bookings_and_time_off(self):
        _production, (operation,) = self.make_production([('Cut', self.center, 30)])
        operation.write({'date_start': at(8), 'date_finished': at(8, 30)})
        run = self.make_run()
        run.action_compute()
        _production, (other,) = self.make_production([('Other booking', self.center, 30)])
        other.write({'date_start': at(10), 'date_finished': at(10, 30)})
        self.env['resource.calendar.leaves'].create({
            'name': 'Unrelated time off', 'calendar_id': self.calendar.id, 'resource_id': self.center.resource_id.id,
            'date_from': at(11), 'date_to': at(11, 30), 'time_type': 'leave',
        })
        run._change_schedule('apply')
        self.assertEqual((run.state, operation.date_start), ('applied', at(13)))
        other.write({'date_start': at(10, 30), 'date_finished': at(11)})
        run._change_schedule('revert')
        self.assertEqual((run.state, operation.date_start, other.date_start), ('reverted', at(8), at(10, 30)))

    def test_apply_refuses_a_new_conflict_at_the_original_booking(self):
        _production, (operation,) = self.make_production([('Cut', self.center, 30)])
        operation.write({'date_start': at(8), 'date_finished': at(8, 30)})
        run = self.make_run()
        run.action_compute()
        self.env['resource.calendar.leaves'].create({
            'name': 'Maintenance at original booking', 'calendar_id': self.calendar.id,
            'resource_id': self.center.resource_id.id, 'date_from': at(8), 'date_to': at(8, 30),
            'time_type': 'other',
        })
        with self.assertRaisesRegex(UserError, 'overlaps'):
            run._change_schedule('apply')
        self.assertEqual((run.state, operation.date_start), ('proposed', at(8)))

    def test_revert_refuses_a_new_conflict(self):
        _production, (operation,) = self.make_production([('Cut', self.center, 30)])
        operation.write({'date_start': at(8), 'date_finished': at(8, 30)})
        run = self.make_run()
        run.action_compute()
        run._change_schedule('apply')
        applied_start = operation.date_start
        self.env['resource.calendar.leaves'].create({
            'name': 'New maintenance', 'calendar_id': self.calendar.id, 'resource_id': self.center.resource_id.id,
            'date_from': at(8), 'date_to': at(8, 30), 'time_type': 'other',
        })
        with self.assertRaisesRegex(UserError, 'overlaps'):
            run._change_schedule('revert')
        self.assertEqual(run.state, 'applied')
        self.assertEqual(operation.date_start, applied_start)

    def test_apply_refuses_changed_tariff_prices(self):
        _production, (operation,) = self.make_production([('Press', self.center, 30)])
        operation.write({'date_start': at(8), 'date_finished': at(8, 30)})
        run = self.make_run()
        run.action_compute()
        start = run.line_ids.proposed_start
        self.tariff.slot_ids.filtered(lambda slot: slot.start_utc == start).sudo().write({'price_per_mwh': 99})
        with self.assertRaisesRegex(UserError, 'Prices, exchange rates, working hours or bookings changed'):
            run._change_schedule('apply')
        self.assertEqual(run.state, 'proposed')
        self.assertEqual((operation.date_start, operation.date_finished), (at(8), at(8, 30)))

    def test_mixed_currency_requires_a_dated_rate(self):
        foreign = self.env['res.currency'].create({'name': 'XTS', 'symbol': 'XTS'})
        base_tariff = self.tariff.copy({'name': 'Company tariff', 'currency_id': self.company.currency_id.id})
        tariff = self.tariff.copy({'name': 'Foreign tariff', 'currency_id': foreign.id})
        run = self.make_run()
        rate_date = fields.Date.to_date('1900-01-02')
        with self.assertRaisesRegex(UserError, 'Add an Odoo exchange rate'):
            run._tariff_exchange_rates(base_tariff | tariff, rate_date)
        self.env['res.currency.rate'].create({
            'name': rate_date, 'currency_id': foreign.id, 'company_id': self.company.root_id.id, 'rate': 2,
        })
        rates = run._tariff_exchange_rates(base_tariff | tariff, rate_date)
        self.assertEqual(rates[foreign.id], {'factor': 0.5, 'date': '1900-01-02'})
        self.assertEqual(run._tariff_exchange_rates(tariff, rate_date), {})

    def test_mixed_currency_proposal_rechecks_rate_before_apply(self):
        foreign = self.env['res.currency'].create({'name': 'XTS', 'symbol': 'XTS'})
        base_tariff = self.tariff.copy({'name': 'Company tariff', 'currency_id': self.company.currency_id.id})
        foreign_tariff = self.tariff.copy({'name': 'Foreign tariff', 'currency_id': foreign.id})
        cheap = {at(13): 10}
        self.add_prices(DAY, 100, cheap, tariff=base_tariff)
        self.add_prices(DAY, 100, cheap, tariff=foreign_tariff)
        self.center.sudo().grid_tariff_source_id = base_tariff
        other = self.center.sudo().copy({
            'name': 'Foreign tariff press', 'grid_tariff_source_id': foreign_tariff.id,
            'grid_allow_shift': True, 'grid_power_kw': 20, 'grid_power_reference': 'Test fixture',
        })
        _first, (cut,) = self.make_production([('Cut', self.center, 15)])
        _second, (press,) = self.make_production([('Press', other, 15)])
        for workorder in cut | press:
            workorder.write({'date_start': at(8), 'date_finished': at(8, 15)})
        rate = self.env['res.currency.rate'].create({
            'name': DAY.date(), 'currency_id': foreign.id, 'company_id': self.company.root_id.id, 'rate': 2,
        })
        run = self.make_run(centers=self.center | other)
        run.action_compute()
        self.assertEqual(run.currency_id, self.company.currency_id)
        self.assertEqual(run.proposed_cost, self.company.currency_id.round(0.075))
        self.assertIn('Exchange rate:', run.data_notes)
        rate.rate = 4
        with self.assertRaisesRegex(UserError, 'exchange rates'):
            run._change_schedule('apply')
        self.assertEqual((cut.date_start, press.date_start), (at(8), at(8)))
        rate.rate = 2
        run._change_schedule('apply')
        self.assertEqual((cut.date_start, press.date_start), (at(13), at(13)))
        run._change_schedule('revert')
        self.assertEqual((cut.date_start, press.date_start), (at(8), at(8)))

    def make_meter_import(self):
        _production, (order,) = self.make_production([('Metered press', self.center, 30)])
        order.write({'date_start': at(8), 'date_finished': at(8, 30)})
        run = self.make_run()
        run.action_compute()
        run._change_schedule('apply')
        line = run.line_ids
        self.assertEqual((line.proposed_start, line.proposed_end), (at(13), at(13, 30)))
        loss = self.env['mrp.workcenter.productivity.loss'].search([('loss_type', '=', 'productive')], limit=1)
        self.env['mrp.workcenter.productivity'].create({
            'workcenter_id': self.center.id, 'workorder_id': order.id, 'loss_id': loss.id,
            'date_start': at(13), 'date_end': at(13, 30),
        })
        with patch.object(fields.Datetime, 'now', return_value=at(13, 30)):
            order.button_finish()
        wizard = self.env['grid.energy.import'].with_user(self.manager).create({
            'line_id': line.id, 'tariff_id': self.tariff.id, 'meter_reference': 'Test submeter export',
            'dedicated_meter': True, 'csv_file': base64.b64encode(
                b'start,end,kwh\n2030-01-07T13:00Z,2030-01-07T13:15Z,6\n2030-01-07T13:15Z,2030-01-07T13:30Z,4\n'),
        })
        return line, wizard

    def test_gateway_batch_can_be_retried_and_used_for_an_energy_result(self):
        line, wizard = self.make_meter_import()
        batches = self.env['grid.meter.batch']
        gateway = new_test_user(
            self.env, login='grid_meter_test_gateway', groups='mrp_grid_scheduler.group_grid_meter_gateway',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)],
        )
        readings = base64.b64decode(wizard.csv_file).decode()
        submit = batches.with_user(gateway).submit_readings
        with self.assertRaisesRegex(UserError, 'Provide a work center'):
            submit(2**31, 'Press meter', 'Batch 1', readings)
        with self.assertRaises(AccessError):
            submit(self.center.id, 'Press meter', 'Batch 1', readings)
        gateway.sudo().grid_meter_workcenter_ids = [Command.set(self.center.ids)]
        with self.assertRaises(AccessError):
            batches.with_user(self.manager).submit_readings(self.center.id, 'Press meter', 'Batch 1', readings)
        batch_id = submit(self.center.id, 'Press meter', 'Batch 1', readings)
        self.assertEqual(submit(self.center.id, 'Press meter', 'Batch 1', readings), batch_id)
        with self.assertRaisesRegex(UserError, 'different readings'):
            submit(self.center.id, 'Press meter', 'Batch 1', readings.replace(',4', ',5'))
        with self.assertRaises(AccessError):
            batches.with_user(gateway).browse(batch_id).read(['readings_csv'])
        wizard.write({'reading_source': 'gateway', 'gateway_batch_id': batch_id,
                      'csv_file': False, 'meter_reference': False})
        with patch.object(fields.Datetime, 'now', return_value=at(14)):
            wizard.action_import()
        result = line.energy_result_ids
        self.assertEqual(result.evidence['gateway_batch_id'], batch_id)
        self.assertEqual(result.with_user(self.planner).gateway_batch_reference, 'Batch 1')
        self.assertEqual((result.meter_reference, result.measured_kwh), ('Press meter', 10))

    def test_meter_result_keeps_its_emissions_factor(self):
        line, wizard = self.make_meter_import()
        self.center.sudo().write({
            'grid_use_carbon_factor': True, 'grid_carbon_factor': 400,
            'grid_carbon_reference': 'Test electricity factor',
        })
        with patch.object(fields.Datetime, 'now', return_value=at(14)):
            wizard.action_import()
        result = line.energy_result_ids
        self.center.sudo().grid_carbon_factor = 500
        self.assertTrue(result.has_emissions_estimate)
        self.assertEqual(result.estimated_emissions_kg, 4)
        self.assertEqual(result.emissions_reference, 'Test electricity factor')
        self.assertEqual(Decimal(result.evidence['emissions_factor_g_co2e_per_kwh']), 400)
        result = result.with_user(self.planner)
        payload = result.read(['interval_csv'])[0]['interval_csv']
        rows = list(csv.DictReader(io.StringIO(base64.b64decode(payload).decode())))
        metered, original = rows[:2], rows[2:]
        self.assertEqual([row['basis'] for row in rows], ['metered'] * 2 + ['original_booking_estimate'] * 2)
        self.assertEqual([row['interval_start_utc'] for row in metered], ['2030-01-07T13:00:00Z', '2030-01-07T13:15:00Z'])
        self.assertEqual([row['interval_start_utc'] for row in original], ['2030-01-07T08:00:00Z', '2030-01-07T08:15:00Z'])
        self.assertEqual(sum(Decimal(row['unrounded_energy_charge']) for row in metered), Decimal('0.10'))
        self.assertEqual(sum(Decimal(row['unrounded_energy_charge']) for row in original), Decimal('1.00'))
        self.assertEqual([Decimal(row['estimated_kg_co2e']) for row in metered], [Decimal('2.4'), Decimal('1.6')])
        self.assertTrue(all(not row['estimated_kg_co2e'] for row in original))
        with self.assertRaises(AccessError):
            result.with_user(self.outsider).action_download_intervals()

    def test_site_links_to_its_tariffs_proposals_and_energy_results(self):
        line, wizard = self.make_meter_import()
        with patch.object(fields.Datetime, 'now', return_value=at(14)):
            wizard.action_import()
        sites = self.env['grid.site'].with_user(self.planner)
        site = sites.create({'name': 'Press site', 'workcenter_ids': [Command.link(self.center.id)]})
        other = sites.create({'name': 'Bench site', 'workcenter_ids': [Command.link(self.other_center.id)]})
        self.assertEqual(site.tariff_ids, self.tariff.with_user(self.planner))
        self.assertFalse(other.tariff_ids)
        for method, record in (
            ('action_open_schedules', line.run_id),
            ('action_open_energy_results', line.energy_result_ids),
        ):
            action = getattr(site, method)()
            self.assertEqual(action['res_model'], record._name)
            model = self.env[action['res_model']].with_user(self.planner)
            self.assertIn(record.id, model.search(action['domain']).ids)
            self.assertNotIn(record.id, model.search(getattr(other, method)()['domain']).ids)
            with self.assertRaises(AccessError):
                getattr(site.with_user(self.outsider), method)()

    def test_meter_result_prices_actual_and_original_times(self):
        line, wizard = self.make_meter_import()
        wizard.write({'compare_bill': True, 'bill_reference': 'Test charge line', 'bill_start': at(13),
                      'bill_end': at(13, 30), 'billed_charge': 0.11})
        with patch.object(fields.Datetime, 'now', return_value=at(14)):
            action = wizard.action_import()
            self.assertEqual(wizard.action_import()['res_id'], action['res_id'])
            self.tariff.slot_ids.sudo().write({'price_per_mwh': 270})
            self.assertEqual(wizard.action_import()['res_id'], action['res_id'])
            wizard.meter_reference = 'A different meter'
            with self.assertRaisesRegex(UserError, 'already has a saved meter result'):
                wizard.action_import()
        result = line.energy_result_ids
        self.assertEqual((result.measured_kwh, result.has_baseline, result.has_bill), (10, True, True))
        self.assertAlmostEqual(result.energy_charge, 0.10)
        self.assertAlmostEqual(result.baseline_charge, 1.00)
        self.assertAlmostEqual(result.estimated_savings, 0.90)
        self.assertAlmostEqual(result.bill_difference, 0.01)
        self.assertEqual(result.imported_by, self.manager)
        self.assertEqual(line.workorder_id.with_user(self.planner).action_open_grid_energy_result()['res_id'], result.id)
        with self.assertRaises(AccessError):
            result.with_user(self.planner).write({'measured_kwh': 100})
        with self.assertRaises(AccessError):
            result.with_user(self.outsider).read(['measured_kwh'])

    def test_manager_can_compare_a_bill_after_import(self):
        line, wizard = self.make_meter_import()
        with patch.object(fields.Datetime, 'now', return_value=at(14)):
            wizard.action_import()
        result = line.energy_result_ids
        with self.assertRaises(AccessError):
            result.with_user(self.planner).action_open_bill_check()
        check = self.env['grid.energy.bill.check'].with_user(self.manager).create({
            'result_id': result.id, 'bill_reference': 'Late bill', 'bill_start': at(13, 15),
            'bill_end': at(13, 30), 'billed_charge': 0.11,
        })
        with self.assertRaisesRegex(UserError, 'exact metered period'):
            check.action_compare()
        check.bill_start = at(13)
        self.assertEqual(check.action_compare()['res_id'], result.id)
        self.assertEqual(result.bill_checked_by, self.manager)
        self.assertAlmostEqual(result.bill_difference, 0.01)
        self.assertAlmostEqual(result.energy_charge, 0.10)
        with self.assertRaisesRegex(UserError, 'already has a saved bill'):
            check.action_compare()
        with patch.object(fields.Datetime, 'now', return_value=at(14)):
            self.assertEqual(wizard.action_import()['res_id'], result.id)

    def test_post_analytic_energy_cost_once(self):
        line, wizard = self.make_meter_import()
        self.company.currency_id = self.tariff.currency_id
        with patch.object(fields.Datetime, 'now', return_value=at(14)):
            wizard.action_import()
        result = line.energy_result_ids.with_user(self.manager)
        with self.assertRaises(AccessError):
            result.action_open_analytic_post()
        self.manager.group_ids = [Command.link(self.env.ref('analytic.group_analytic_accounting').id)]
        plan = self.env['account.analytic.plan'].sudo().create({'name': 'Grid energy test'})
        account = self.env['account.analytic.account'].sudo().create({
            'name': 'Electricity test', 'plan_id': plan.id, 'company_id': self.company.id,
        })
        post = self.env['grid.energy.analytic.post'].with_user(self.manager).create({
            'result_id': result.id, 'account_id': account.id,
        })
        post.action_post()
        entry = result.analytic_line_id
        self.assertTrue(entry)
        self.assertEqual(entry.amount, -result.energy_charge)
        self.assertEqual(entry[plan._column_name()], account)
        post.action_post()
        self.assertEqual(result.analytic_line_id, entry)

    def test_bill_comparison_uses_posted_untaxed_vendor_line(self):
        line, wizard = self.make_meter_import()
        with patch.object(fields.Datetime, 'now', return_value=at(14)):
            wizard.action_import()
        result = line.energy_result_ids
        result.currency_id.sudo().active = True
        self.manager.group_ids = [Command.link(self.env.ref('account.group_account_invoice').id)]
        accounts = self.env['account.account']
        expense = accounts.create({
            'name': 'Grid energy expense', 'code': 'GE9901', 'account_type': 'expense',
            'company_ids': [Command.set(self.company.ids)],
        })
        payable = accounts.create({
            'name': 'Grid energy payable', 'code': 'GE9902', 'account_type': 'liability_payable',
            'reconcile': True, 'company_ids': [Command.set(self.company.ids)],
        })
        vendor = self.env['res.partner'].create({'name': 'Grid utility test', 'property_account_payable_id': payable.id})
        journal = self.env['account.journal'].create({
            'name': 'Grid utility bills', 'code': 'GUTIL', 'type': 'purchase', 'company_id': self.company.id,
        })
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice', 'company_id': self.company.id, 'journal_id': journal.id,
            'partner_id': vendor.id, 'currency_id': result.currency_id.id, 'invoice_date': at(13).date(),
            'invoice_line_ids': [Command.create({
                'name': 'Metered energy', 'account_id': expense.id, 'quantity': 2, 'price_unit': 0.06,
            })],
        })
        check = self.env['grid.energy.bill.check'].with_user(self.manager).create({
            'result_id': result.id, 'use_vendor_bill': True, 'bill_line_id': bill.invoice_line_ids.id,
            'bill_start': at(13), 'bill_end': at(13, 30),
        })
        with self.assertRaisesRegex(UserError, 'Choose a posted vendor bill'):
            check.action_compare()
        bill.action_post()
        self.assertEqual(check.vendor_id, vendor)
        self.assertEqual(check.invoice_date, bill.invoice_date)
        self.assertAlmostEqual(check.vendor_line_subtotal, 0.12)
        check.action_compare()
        self.assertEqual(result.with_user(self.manager).bill_line_id, bill.invoice_line_ids)
        self.assertAlmostEqual(result.billed_charge, 0.12)
        self.assertAlmostEqual(result.bill_difference, 0.02)
        self.assertEqual(bill.state, 'posted')

    def test_meter_import_rejects_incomplete_and_shared_usage(self):
        line, wizard = self.make_meter_import()
        original = wizard.csv_file
        with patch.object(fields.Datetime, 'now', return_value=at(14)):
            wizard.csv_file = base64.b64encode(
                base64.b64decode(original) + b'2030-01-07T17:00+04:00,2030-01-07T17:15+04:00,6\n',
            )
            with self.assertRaisesRegex(UserError, 'overlap'):
                wizard.action_import()
            wizard.csv_file = base64.b64encode(b'start,end,kwh\n2030-01-07T13:00Z,2030-01-07T13:15Z,6\n')
            with self.assertRaisesRegex(UserError, 'Check for gaps'):
                wizard.action_import()
            wizard.csv_file = original
            _production, (other,) = self.make_production([('Other operation', self.center, 15)])
            self.env['mrp.workcenter.productivity'].create({
                'workcenter_id': self.center.id, 'workorder_id': other.id,
                'loss_id': line.workorder_id.time_ids.loss_id.id, 'date_start': at(13, 15), 'date_end': at(13, 30),
            })
            with self.assertRaisesRegex(UserError, 'Another operation'):
                wizard.action_import()
        self.assertFalse(line.energy_result_ids)

    def test_meter_reading_split_at_tariff_change(self):
        readings = parse_meter_csv(b'start,end,kwh\n2030-01-07T17:55+05:00,2030-01-07T18:10+05:00,6\n')
        prices = {at(12, 45): Decimal('100'), at(13): Decimal('10')}
        self.assertEqual(price_readings(readings, prices), (Decimal('0.24'), set(prices)))

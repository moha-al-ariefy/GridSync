import base64
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
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
        with self.assertRaisesRegex(UserError, 'Choose a complete proposal'):
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
        with self.assertRaisesRegex(UserError, 'Prices, working hours or bookings changed'):
            run._change_schedule('apply')
        self.assertEqual(run.state, 'proposed')
        self.assertEqual((operation.date_start, operation.date_finished), (at(8), at(8, 30)))

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

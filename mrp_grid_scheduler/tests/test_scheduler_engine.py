import random
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import BaseCase

from ..services import scheduler
from ..services.scheduler import (
    STEP, JobInput, ScheduleInputError, WorkcenterInput, dump_inputs, load_inputs, schedule,
)


DAY = datetime(2030, 1, 7)  # A Monday; the engine only sees UTC.
END = DAY + timedelta(days=1)


def at(hours, minutes=0):
    return DAY + timedelta(hours=hours, minutes=minutes)


def prices(default, overrides=None, start=DAY, end=END):
    result = {}
    slot = start
    while slot < end:
        result[slot] = default
        slot += STEP
    result.update(overrides or {})
    return result


def hourly(start_hour, end_hour, price):
    return {at(start_hour) + STEP * index: price for index in range((end_hour - start_hour) * 4)}


def center(key=1, power=10.0, working=((DAY, END),), busy=(), price_map=None):
    return WorkcenterInput(key, power, tuple(working), tuple(busy), price_map if price_map is not None else prices(100))


def job(key, minutes=60, workcenter=1, release=DAY, **values):
    return JobInput(key=key, workcenter=workcenter, minutes=minutes, release=release, **values)


def minute_cost(data, start, end):
    """Independent cost check, one minute at a time."""
    cost = 0.0
    moment = start
    while moment < end:
        if any(low <= moment < high for low, high in data.working):
            slot = moment - (moment - DAY) % STEP
            cost += data.power_kw / 60 * data.prices[slot] / 1000
        moment += timedelta(minutes=1)
    return cost


@tagged('post_install', '-at_install')
class TestSchedulerEngine(BaseCase):
    def test_moves_job_to_lowest_cost_window(self):
        result = schedule(DAY, END, [center(price_map=prices(100, hourly(2, 4, 20)))],
                          [job(1, current_start=at(8))])
        line = result.jobs[1]
        self.assertEqual((line.status, line.reason), ('moved', 'cheaper'))
        self.assertEqual((line.proposed.start, line.proposed.end), (at(2), at(3)))
        self.assertAlmostEqual(line.proposed.cost, 10 * 1 * 20 / 1000)
        self.assertAlmostEqual(line.proposed.energy_kwh, 10)
        self.assertAlmostEqual(result.current_cost, 1.0)
        self.assertAlmostEqual(result.lower_bound, result.proposed_cost)
        self.assertTrue(result.complete)

    def test_keeps_current_time_when_nothing_is_cheaper(self):
        result = schedule(DAY, END, [center()], [job(1, current_start=at(10))])
        self.assertEqual((result.jobs[1].status, result.jobs[1].reason), ('unchanged', 'lowest'))
        self.assertEqual(result.jobs[1].proposed.start, at(10))

    def test_hides_a_bad_lower_bound_after_checking_the_proposal(self):
        data = center()
        operation = job(1)
        placement = scheduler.Placement(DAY, at(1), 10, 1)
        valid = (placement, None, 'outside')
        invalid_bound = (replace(placement, cost=2), None, 'outside')
        with patch.object(scheduler, '_evaluate', side_effect=[invalid_bound, valid]), \
                self.assertLogs(scheduler.__name__, level='WARNING'):
            result = schedule(DAY, END, [data], [operation])
        self.assertTrue(result.complete)
        self.assertIsNone(result.lower_bound)
        self.assertEqual(result.jobs[1].proposed, placement)
        self.assertEqual(result.proposed_cost, 1)

        invalid_proposal = (replace(placement, cost=0.5), None, 'outside')
        with patch.object(scheduler, '_evaluate', side_effect=[invalid_bound, invalid_proposal]):
            with self.assertRaisesRegex(ScheduleInputError, 'costs could not be verified'):
                schedule(DAY, END, [data], [operation])

    def test_partial_intervals_and_negative_prices(self):
        data = center(power=12, working=[(at(0, 5), at(6))], price_map=prices(80, {
            at(0): -40, at(0, 15): -40, at(0, 30): 60,
        }))
        line = schedule(DAY, END, [data], [job(1, minutes=37)]).jobs[1]
        self.assertEqual((line.proposed.start, line.proposed.end), (at(0, 5), at(0, 42)))
        expected = 12 * (25 / 60 * -40 + 12 / 60 * 60) / 1000
        self.assertAlmostEqual(line.proposed.cost, expected)
        self.assertAlmostEqual(line.proposed.cost, minute_cost(data, line.proposed.start, line.proposed.end))
        self.assertAlmostEqual(line.proposed.energy_kwh, 12 * 37 / 60)
        self.assertEqual(line.reason, 'unplanned')

    def test_missing_prices_are_not_filled(self):
        cheap = prices(100, hourly(2, 4, 10))
        del cheap[at(2, 30)]
        line = schedule(DAY, END, [center(price_map=cheap)], [job(1)]).jobs[1]
        self.assertGreaterEqual(line.proposed.start, at(2, 45))
        self.assertAlmostEqual(line.proposed.cost, 0.1)
        line = schedule(DAY, END, [center(price_map={})], [job(1, current_start=at(1))]).jobs[1]
        self.assertEqual((line.status, line.reason, line.current_issue), ('unscheduled', 'no_prices', 'no_prices'))

    def test_non_working_time_blocks_the_center_but_uses_no_energy(self):
        working = [(at(8), at(12)), (at(13), at(17))]
        price_map = prices(100, {**hourly(11, 12, 10), **hourly(12, 13, -500), **hourly(13, 14, 10)})
        data = center(working=working, price_map=price_map)
        result = schedule(DAY, END, [data], [job(1, minutes=120), job(2, minutes=30)])
        first = result.jobs[1].proposed
        self.assertEqual((first.start, first.end), (at(11), at(14)))
        self.assertAlmostEqual(first.cost, 10 * 2 * 10 / 1000)
        self.assertAlmostEqual(first.energy_kwh, 20)
        second = result.jobs[2].proposed
        self.assertTrue(second.end <= first.start or second.start >= first.end)

    def test_unscheduled_reasons(self):
        data = center(working=[(at(8), at(10))], busy=[(at(8), at(9, 30))])
        result = schedule(DAY, END, [data, center(2)], [
            job(1, due=at(7)),
            job(2, minutes=60),
            job(3, predecessors=(2,), workcenter=2),
            job(4, workcenter=2, blocked_reason='external_unplanned'),
            job(5, workcenter=2, predecessors=(4,)),
        ])
        reasons = {key: (line.status, line.reason) for key, line in result.jobs.items()}
        self.assertEqual(reasons, {
            1: ('unscheduled', 'no_window'), 2: ('unscheduled', 'conflict'), 3: ('unscheduled', 'predecessor'),
            4: ('unscheduled', 'external_unplanned'), 5: ('unscheduled', 'predecessor'),
        })
        self.assertFalse(result.complete)
        self.assertIsNone(result.lower_bound)
        self.assertIsNone(result.current_cost)

    def test_successor_deadline_limits_predecessor(self):
        result = schedule(DAY, END, [
            center(1, price_map=prices(100, hourly(20, 22, 1))), center(2),
        ], [job(1), job(2, workcenter=2, predecessors=(1,), due=at(21))])
        first, second = result.jobs[1].proposed, result.jobs[2].proposed
        self.assertLessEqual(first.end, at(20))
        self.assertGreaterEqual(second.start, first.end)
        self.assertLessEqual(second.end, at(21))
        self.assertTrue(result.complete)

    def test_unscheduled_job_keeps_its_booking(self):
        # Job 2 waits for an unplanned operation, so it stays at 02:00-03:00 and job 1 avoids it.
        price_map = prices(100, hourly(2, 3, 1))
        result = schedule(DAY, END, [center(price_map=price_map)], [
            job(1), job(2, current_start=at(2), current_end=at(3), blocked_reason='external_unplanned'),
        ])
        self.assertEqual(result.jobs[2].status, 'unscheduled')
        placement = result.jobs[1].proposed
        self.assertTrue(placement.end <= at(2) or placement.start >= at(3))

    def test_tight_successor_goes_first(self):
        # Job 1 would take the cheap hour, but job 2 needs it to meet its successor's deadline.
        result = schedule(DAY, END, [center(price_map=prices(100, hourly(0, 1, 1))), center(2)], [
            job(1), job(2, predecessors=(3,), due=at(1, 30)), job(3, workcenter=2, minutes=30),
        ])
        self.assertTrue(result.complete)
        self.assertEqual(result.jobs[2].proposed.start, at(0, 30))
        self.assertGreaterEqual(result.jobs[1].proposed.start, at(1, 30))

    def test_competing_deadlines_protect_kept_booking(self):
        result = schedule(DAY, END, [center()], [
            job(1, due=at(1)), job(2, due=at(1), current_start=at(0), current_end=at(1)),
        ])
        self.assertIsNone(result.jobs[1].proposed)
        self.assertEqual(result.jobs[1].reason, 'retained_booking')
        self.assertEqual((result.jobs[2].status, result.jobs[2].reason), ('unscheduled', 'conflict'))
        self.assertFalse(result.complete)

    def test_retained_successor_protects_dependency_across_workcenters(self):
        result = schedule(DAY, END, [
            center(1, price_map=prices(50, hourly(2, 3, 0))),
            center(2, price_map={}),
        ], [
            job(1, minutes=15, current_start=at(0), current_end=at(0, 15)),
            job(2, minutes=15, workcenter=2, predecessors=(1,),
                current_start=at(0, 15), current_end=at(0, 30)),
        ])
        self.assertIsNone(result.jobs[1].proposed)
        self.assertEqual(result.jobs[1].reason, 'retained_dependency')
        self.assertEqual(result.jobs[2].reason, 'no_prices')
        self.assertFalse(result.complete)

    def test_higher_cost_or_incomplete_greedy_result_keeps_feasible_baseline(self):
        jobs = [
            job(1, minutes=15, current_start=at(0), current_end=at(0, 15)),
            job(2, minutes=30, current_start=at(0, 15), current_end=at(0, 45)),
        ]
        price_map = {at(0): 100, at(0, 15): 0, at(0, 30): 0, at(0, 45): 1000}
        for end in (at(0, 45), at(1)):
            with self.subTest(end=end):
                result = schedule(DAY, end, [center(price_map=price_map)], jobs)
                self.assertTrue(result.complete)
                self.assertAlmostEqual(result.proposed_cost, 0.25)
                self.assertEqual(result.proposed_cost, result.current_cost)
                self.assertEqual([line.proposed.start for line in result.jobs.values()], [at(0), at(0, 15)])
                self.assertEqual({line.reason for line in result.jobs.values()}, {'baseline'})

    def test_lower_bound_uses_the_raw_minimum_before_ties(self):
        data = center(power=1, price_map={at(0): 100.0, at(0, 15): 100.000001})
        result = schedule(DAY, at(0, 30), [data], [job(1, minutes=15, current_start=at(0, 15))])
        self.assertEqual(result.jobs[1].proposed.start, at(0))
        self.assertEqual(result.lower_bound, 0.025)
        self.assertLess(result.proposed_cost, result.current_cost)

    def test_current_cost_requires_a_feasible_comparable_booking(self):
        result = schedule(DAY, END, [center()], [
            job(1, minutes=30, current_start=at(2), current_end=at(2, 15)),
        ])
        self.assertTrue(result.complete)
        self.assertIsNone(result.current_cost)
        self.assertIsNone(result.jobs[1].current)
        self.assertEqual((result.jobs[1].status, result.jobs[1].reason), ('moved', 'duration_changed'))
        self.assertEqual((result.jobs[1].proposed.start, result.jobs[1].proposed.end), (at(2), at(2, 30)))

    def test_duration_longer_than_horizon_is_unscheduled(self):
        result = schedule(DAY, END, [center()], [job(1, minutes=10**13)])
        self.assertEqual(result.jobs[1].reason, 'no_window')
        self.assertIsNone(result.jobs[1].proposed)

    def test_lower_bound_with_competition_and_zero_prices(self):
        price_map = prices(50, hourly(1, 2, 0))
        result = schedule(DAY, END, [center(price_map=price_map)], [job(1), job(2)])
        self.assertAlmostEqual(result.lower_bound, 0)
        self.assertAlmostEqual(result.proposed_cost, 0.5)
        self.assertLessEqual(result.lower_bound, result.proposed_cost)

    def test_random_instances_respect_constraints(self):
        for seed in range(25):
            rng = random.Random(seed)
            working = [(at(hour), at(hour + rng.choice([2, 3, 4]))) for hour in (0, 6, 12, 18)]
            centers = [center(
                key, power=rng.choice([5.5, 12, 40]), working=working,
                busy=[(at(hour), at(hour + 1)) for hour in [rng.randrange(0, 22)]][:rng.randrange(2)],
                price_map=prices(0, {slot: rng.uniform(-30, 200) for slot in prices(0)}),
            ) for key in (1, 2)]
            jobs = []
            for key in range(1, 9):
                predecessors = (key - 1,) if key % 4 != 1 else ()
                jobs.append(job(
                    key, minutes=rng.choice([15, 37, 60, 95]), workcenter=rng.choice([1, 2]),
                    release=at(rng.choice([0, 1, 2])), predecessors=predecessors,
                    due=at(rng.choice([20, 24])) if rng.random() < .5 else None,
                    current_start=at(rng.randrange(0, 20)) if rng.random() < .7 else None,
                ))
            result = schedule(DAY, END, centers, jobs)
            by_key = {data.key: data for data in centers}
            placed = {key: line.proposed for key, line in result.jobs.items() if line.proposed}
            for item in jobs:
                placement = placed.get(item.key)
                if not placement:
                    continue
                data = by_key[item.workcenter]
                self.assertGreaterEqual(placement.start, item.release)
                self.assertLessEqual(placement.end, item.due or END)
                self.assertTrue(any(low <= placement.start < high for low, high in data.working))
                for predecessor in item.predecessors:
                    self.assertLessEqual(placed[predecessor].end, placement.start)
                for low, high in data.busy:
                    self.assertFalse(low < placement.end and placement.start < high, seed)
                for other in jobs:
                    other_placement = placed.get(other.key)
                    if other.key != item.key and other.workcenter == item.workcenter and other_placement:
                        self.assertFalse(other_placement.start < placement.end and placement.start < other_placement.end)
                self.assertAlmostEqual(placement.cost, minute_cost(data, placement.start, placement.end), places=6)
            if result.lower_bound is not None:
                self.assertLessEqual(result.lower_bound, result.proposed_cost + 1e-6)

    def test_same_inputs_give_same_result_and_snapshot_round_trips(self):
        centers = [center(price_map=prices(100, hourly(3, 5, 10))), center(2)]
        jobs = [job(1, current_start=at(9), current_end=at(10)), job(2, predecessors=(1,)), job(3, workcenter=2, minutes=45)]
        first = schedule(DAY, END, centers, jobs)
        self.assertEqual(first, schedule(DAY, END, centers, jobs))
        self.assertEqual(first, schedule(*load_inputs(dump_inputs(DAY, END, centers, jobs))))

    def test_invalid_inputs(self):
        with self.assertRaises(ScheduleInputError):
            schedule(DAY, DAY + timedelta(days=15), [center()], [])
        with self.assertRaises(ScheduleInputError):
            schedule(DAY, END, [center(power=0)], [])
        with self.assertRaises(ScheduleInputError):
            schedule(DAY, END, [center()], [job(1, predecessors=(9,))])
        with self.assertRaises(ScheduleInputError):
            schedule(DAY, END, [center(price_map={at(0, 5): 1})], [])
        with self.assertRaises(ScheduleInputError):
            schedule(DAY, END, [center(busy=[(at(3), at(2))])], [])

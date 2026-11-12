"""Deterministic price-based scheduling heuristic.

The engine is independent of the Odoo ORM. All datetimes are naive UTC. A job
consumes working minutes on its work center calendar and blocks the work center
from its start to its end, including any non-working time in between, which
matches how Odoo books work orders. Energy is only counted during working time.

This is a greedy heuristic, not an exact optimizer. Jobs start on 15-minute
marks, at the start of a working interval or at their current start.
"""
import bisect
import heapq
import logging
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)

STEP = timedelta(minutes=15)
EPOCH = datetime(2000, 1, 1)
MAX_HORIZON = timedelta(days=14)
MAX_JOBS = 500
MIN_DURATION = timedelta(seconds=1)
COST_DIGITS = 6
METHOD = 'greedy-15min-v1'


class ScheduleInputError(ValueError):
    pass


@dataclass(frozen=True)
class WorkcenterInput:
    key: int
    power_kw: float
    working: tuple  # sorted, disjoint (start, end) pairs
    busy: tuple = ()  # fixed bookings that stay in place
    prices: dict = None  # 15-minute slot start -> price per MWh


@dataclass(frozen=True)
class JobInput:
    key: int
    workcenter: int
    minutes: float
    release: datetime
    due: datetime = None
    predecessors: tuple = ()
    current_start: datetime = None
    current_end: datetime = None
    blocked_reason: str = None


@dataclass(frozen=True)
class Placement:
    start: datetime
    end: datetime
    energy_kwh: float
    cost: float


@dataclass(frozen=True)
class JobResult:
    key: int
    status: str  # moved, unchanged or unscheduled
    reason: str
    proposed: Placement = None
    current: Placement = None
    current_issue: str = None


@dataclass(frozen=True)
class ScheduleResult:
    jobs: dict
    proposed_cost: float
    current_cost: float
    lower_bound: float
    complete: bool


def floor_slot(value):
    return value - (value - EPOCH) % STEP


def ceil_slot(value):
    floor = floor_slot(value)
    return floor if floor == value else floor + STEP


def _merge(intervals, low, high):
    merged = []
    for start, end in sorted((max(s, low), min(e, high)) for s, e in intervals):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlaps(intervals, start, end):
    return any(busy_start < end and start < busy_end for busy_start, busy_end in intervals)


class _Center:
    def __init__(self, data, horizon_start, horizon_end, require_power=True):
        if require_power and (not math.isfinite(data.power_kw) or data.power_kw <= 0):
            raise ScheduleInputError(f'Work center {data.key} needs a positive operating power.')
        for start, price in (data.prices or {}).items():
            if floor_slot(start) != start or not math.isfinite(price):
                raise ScheduleInputError(f'Work center {data.key} has an invalid price interval.')
        if any(start > end for start, end in tuple(data.working) + tuple(data.busy)):
            raise ScheduleInputError(f'Work center {data.key} has an interval that ends before it starts.')
        self.key = data.key
        self.power_kw = data.power_kw
        self.working = _merge(data.working, horizon_start, horizon_end)
        self.starts = [start for start, _end in self.working]
        self.busy = _merge(data.busy, horizon_start - MAX_HORIZON, horizon_end + MAX_HORIZON)
        self.prices = dict(data.prices or {})

    def in_working(self, moment):
        index = bisect.bisect_right(self.starts, moment) - 1
        return index >= 0 and moment < self.working[index][1]

    def walk(self, start, duration):
        """Return (end, worked segments) when the job starts in working time and fits in the horizon."""
        index = bisect.bisect_right(self.starts, start) - 1
        if index < 0 or start >= self.working[index][1]:
            return None
        remaining, moment, segments = duration, start, []
        while index < len(self.working):
            interval_start, interval_end = self.working[index]
            moment = max(moment, interval_start)
            if interval_end - moment >= remaining:
                end = moment + remaining
                segments.append((moment, end))
                return end, segments
            segments.append((moment, interval_end))
            remaining -= interval_end - moment
            index += 1
        return None

    def latest_start(self, finish, duration):
        """Latest start that finishes by `finish`, ignoring other bookings."""
        index = bisect.bisect_left(self.starts, finish) - 1
        remaining, moment = duration, finish
        while index >= 0:
            interval_start, interval_end = self.working[index]
            moment = min(moment, interval_end)
            if moment - interval_start >= remaining:
                return moment - remaining
            remaining -= moment - interval_start
            index -= 1
        return None

    def price(self, segments):
        """Return (energy kWh, cost) or None if any needed price interval is missing."""
        energy = cost = 0.0
        for start, end in segments:
            slot = floor_slot(start)
            while slot < end:
                price = self.prices.get(slot)
                if price is None:
                    return None
                hours = (min(end, slot + STEP) - max(start, slot)).total_seconds() / 3600
                energy += self.power_kw * hours
                cost += self.power_kw * hours * price / 1000
                slot += STEP
        return energy, cost

    def candidates(self, low, high, current_start):
        found = set()
        for interval_start, interval_end in self.working:
            if interval_end <= low or interval_start >= high:
                continue
            if interval_start >= low:
                found.add(interval_start)
            moment = ceil_slot(max(interval_start, low))
            while moment < interval_end and moment < high:
                found.add(moment)
                moment += STEP
        if current_start and low <= current_start < high and self.in_working(current_start):
            found.add(current_start)
        return sorted(found)


def _duration(job):
    if not math.isfinite(job.minutes) or job.minutes < 0:
        raise ScheduleInputError(f'Job {job.key} needs a finite, non-negative duration.')
    horizon_minutes = MAX_HORIZON.total_seconds() / 60
    if job.minutes > horizon_minutes:
        return MAX_HORIZON + MIN_DURATION
    return max(timedelta(seconds=round(job.minutes * 60)), MIN_DURATION)


def _evaluate(center, job, duration, low, finish_by, busy):
    """Find the lowest-cost start. Returns (placement, reason, current start issue)."""
    seen = {'window': False, 'free': False}
    best = best_key = None
    current_issue = 'outside'
    for start in center.candidates(low, finish_by, job.current_start):
        walked = center.walk(start, duration)
        if not walked or walked[0] > finish_by:
            break  # The end time only grows with the start time.
        end, segments = walked
        seen['window'] = True
        if _overlaps(busy, start, end):
            if start == job.current_start:
                current_issue = 'conflict'
            continue
        seen['free'] = True
        priced = center.price(segments)
        if priced is None:
            if start == job.current_start:
                current_issue = 'no_prices'
            continue
        if start == job.current_start:
            current_issue = None
        key = (priced[1], start != job.current_start, start)
        if best_key is None or key < best_key:
            best_key = key
            best = Placement(start, end, priced[0], priced[1])
    if best:
        return best, None, current_issue
    reason = 'no_prices' if seen['free'] else 'conflict' if seen['window'] else 'no_window'
    return None, reason, current_issue


def _topological(jobs, finish_by=None):
    successors = {key: [] for key in jobs}
    pending = {key: 0 for key in jobs}
    for job in jobs.values():
        for predecessor in set(job.predecessors):
            successors[predecessor].append(job.key)
            pending[job.key] += 1
    finish_by = finish_by or {}
    priority = lambda key: (finish_by.get(key) or jobs[key].due or datetime.max, jobs[key].release, key)
    heap = [(priority(key), key) for key, count in pending.items() if not count]
    heapq.heapify(heap)
    order = []
    while heap:
        _priority, key = heapq.heappop(heap)
        order.append(key)
        for successor in sorted(successors[key]):
            pending[successor] -= 1
            if not pending[successor]:
                heapq.heappush(heap, (priority(successor), successor))
    cyclic = sorted(key for key, count in pending.items() if count)
    return order, cyclic, successors


def _current_placement(center, job, duration):
    if not job.current_start:
        return None, 'unplanned'
    walked = center.walk(job.current_start, duration)
    if not walked:
        return None, 'outside'
    if job.current_end and walked[0] != job.current_end:
        return None, 'duration_changed'
    priced = center.price(walked[1])
    if priced is None:
        return None, 'no_prices'
    return Placement(job.current_start, walked[0], priced[0], priced[1]), None


def _feasible_baseline(jobs, current, centers, finish_by):
    """fixed. this returns the existing schedule only when every booking is feasible and priced."""
    if not jobs or len(finish_by) != len(jobs):
        return None
    bookings = {key: list(center.busy) for key, center in centers.items()}
    baseline = {}
    for key, job in jobs.items():
        placement = current[key][0]
        if (not placement or job.blocked_reason or placement.start < job.release or placement.end > finish_by[key] or
                _overlaps(bookings[job.workcenter], placement.start, placement.end)):
            return None
        baseline[key] = placement
        bookings[job.workcenter].append((placement.start, placement.end))
    for key, job in jobs.items():
        if any(baseline[parent].end > baseline[key].start for parent in job.predecessors):
            return None
    return baseline


def _protect_retained_bookings(jobs, successors, kept_booking, placed, unscheduled):
    """fixed. this withdraws unsafe moves, and each withdrawn move can restore another booking."""
    pending = deque(sorted(unscheduled))

    def retain(key, reason):
        if key in placed:
            del placed[key]
            unscheduled[key] = reason
            pending.append(key)

    while pending:
        key = pending.popleft()
        job = jobs[key]
        booking = kept_booking(key)
        if booking:
            for other, placement in list(placed.items()):
                if (jobs[other].workcenter == job.workcenter and
                        placement.start < booking[1] and booking[0] < placement.end):
                    retain(other, 'retained_booking')
        if job.current_start:
            for predecessor in sorted(job.predecessors):
                placement = placed.get(predecessor)
                if placement and placement.end > job.current_start:
                    retain(predecessor, 'retained_dependency')
        for successor in sorted(successors[key]):
            retain(successor, 'predecessor')


def schedule(horizon_start, horizon_end, workcenters, jobs):
    """Propose lower-cost start times for `jobs`. Inputs are not modified."""
    if horizon_start >= horizon_end or horizon_end - horizon_start > MAX_HORIZON:
        raise ScheduleInputError('Choose a scheduling period of up to 14 days.')
    if len(jobs) > MAX_JOBS:
        raise ScheduleInputError(f'Schedule at most {MAX_JOBS} operations at a time.')
    centers = {data.key: _Center(data, horizon_start, horizon_end) for data in workcenters}
    if len(centers) != len(workcenters) or len({job.key for job in jobs}) != len(jobs):
        raise ScheduleInputError('Work centers and operations must have unique IDs.')
    jobs = {job.key: job for job in sorted(jobs, key=lambda job: job.key)}
    durations = {}
    for job in jobs.values():
        if job.workcenter not in centers:
            raise ScheduleInputError(f'Job {job.key} uses an unknown work center.')
        if any(key not in jobs or key == job.key for key in job.predecessors):
            raise ScheduleInputError(f'Job {job.key} depends on an operation outside this run.')
        durations[job.key] = _duration(job)

    order, cyclic, successors = _topological(jobs)
    unscheduled = {key: 'cycle' for key in cyclic}
    finish_by = {}
    for key in reversed(order):
        job = jobs[key]
        limit = min(horizon_end, job.due) if job.due else horizon_end
        for successor in successors[key]:
            if successor in finish_by:
                latest = centers[jobs[successor].workcenter].latest_start(
                    finish_by[successor], durations[successor])
                if latest is not None:
                    limit = min(limit, latest)
        finish_by[key] = limit
    # Jobs with the tightest latest finish, including their successors' needs, go first.
    order, _, _ = _topological(jobs, finish_by)

    # Static pass: jobs that cannot run whatever else moves stay where they are.
    fixed = {key: list(center.busy) for key, center in centers.items()}
    bounds = {}
    for key in order:
        job = jobs[key]
        if job.blocked_reason:
            unscheduled[key] = job.blocked_reason
        elif any(predecessor in unscheduled for predecessor in job.predecessors):
            unscheduled[key] = 'predecessor'
        else:
            best, reason, _issue = _evaluate(
                centers[job.workcenter], job, durations[key], job.release, finish_by[key], fixed[job.workcenter])
            if best:
                bounds[key] = best.cost
            else:
                unscheduled[key] = reason
    current = {key: _current_placement(centers[job.workcenter], job, durations[key]) for key, job in jobs.items()}

    def kept_booking(key):
        job = jobs[key]
        if not job.current_start:
            return None
        end = job.current_end
        if not end:
            walked = centers[job.workcenter].walk(job.current_start, durations[key])
            end = walked[0] if walked else horizon_end
        return (job.current_start, end) if end > job.current_start else None

    for key in unscheduled:
        if kept_booking(key):
            fixed[jobs[key].workcenter].append(kept_booking(key))

    # Greedy pass in dependency order.
    placed = {}
    results = {}
    for key in order:
        if key in unscheduled:
            continue
        job = jobs[key]
        if any(predecessor in unscheduled for predecessor in job.predecessors):
            unscheduled[key] = 'predecessor'
            continue
        low = max([job.release] + [placed[predecessor].end for predecessor in job.predecessors])
        busy = fixed[job.workcenter] + [
            (placement.start, placement.end) for other, placement in placed.items()
            if jobs[other].workcenter == job.workcenter
        ]
        best, reason, issue = _evaluate(centers[job.workcenter], job, durations[key], low, finish_by[key], busy)
        if not best:
            unscheduled[key] = reason
            continue
        placed[key] = best
        if not job.current_start:
            status, why = 'moved', 'unplanned'
        elif best.start == job.current_start and (not job.current_end or best.end == job.current_end):
            status, why = 'unchanged', 'lowest'
        elif current[key][1] == 'duration_changed':
            status, why = 'moved', 'duration_changed'
        else:
            status, why = 'moved', f'current_{issue}' if issue else 'cheaper'
        results[key] = (status, why)

    _protect_retained_bookings(jobs, successors, kept_booking, placed, unscheduled)
    proposed_cost = math.fsum(placement.cost for placement in placed.values())
    baseline = _feasible_baseline(jobs, current, centers, finish_by)
    current_cost = math.fsum(placement.cost for placement in baseline.values()) if baseline else None
    if baseline and (unscheduled or (proposed_cost >= current_cost and placed != baseline)):
        placed = baseline
        unscheduled = {}
        proposed_cost = current_cost
        results = {key: ('unchanged', 'baseline') for key in baseline}

    job_results = {}
    for key, job in jobs.items():
        current_booking, current_issue = current[key]
        if key in unscheduled:
            job_results[key] = JobResult(key, 'unscheduled', unscheduled[key], None, current_booking, current_issue)
        else:
            status, why = results[key]
            job_results[key] = JobResult(key, status, why, placed[key], current_booking, current_issue)

    complete = not unscheduled
    lower_bound = math.fsum(bounds[key] for key in jobs) if complete and jobs else None
    if lower_bound is not None and lower_bound > proposed_cost + 10 ** -COST_DIGITS * max(1, len(jobs)):
        times = {key: (item.start, item.end) for key, item in placed.items()}
        verified = check_placements(horizon_start, horizon_end, workcenters, jobs.values(), times)
        if verified != placed:
            raise ScheduleInputError('The proposal costs could not be verified. Compute a new proposal.')
        _logger.warning('GridSync hid a lower bound that exceeded the verified proposal cost.')
        lower_bound = None
    return ScheduleResult(job_results, proposed_cost, current_cost, lower_bound, complete)


def check_placements(start, end, workcenters, jobs, times, require_prices=True):
    """making sure of the booking date before odoo save them"""
    centers = {item.key: _Center(item, start, end, require_power=require_prices) for item in workcenters}
    bookings = {key: list(center.busy) for key, center in centers.items()}
    placements = {}
    for job in jobs:
        window = times.get(job.key)
        if not window:
            continue
        low, high = window
        center = centers[job.workcenter]
        worked = center.walk(low, _duration(job))
        if low < job.release or high > min(job.due or end, end) or job.blocked_reason:
            raise ScheduleInputError(f'Operation {job.key} no longer fits its dependencies or deadline.')
        if not worked or worked[0] != high:
            raise ScheduleInputError(f'Operation {job.key} no longer fits its working hours.')
        if _overlaps(bookings[job.workcenter], low, high):
            raise ScheduleInputError(f'Operation {job.key} overlaps an existing booking.')
        if require_prices:
            priced = center.price(worked[1])
            if priced is None:
                raise ScheduleInputError(f'Operation {job.key} is missing price intervals.')
            placements[job.key] = Placement(low, high, *priced)
        for predecessor in job.predecessors:
            earlier = times.get(predecessor)
            if not earlier or earlier[1] > low:
                raise ScheduleInputError(f'Operation {job.key} no longer fits its dependency order.')
        bookings[job.workcenter].append((low, high))
    return placements


def _iso(value):
    return value.isoformat() if value else None


def _parse(value):
    return datetime.fromisoformat(value) if value else None


def dump_inputs(horizon_start, horizon_end, workcenters, jobs):
    """Serialize engine inputs so a run can be reproduced exactly."""
    return {
        'method': METHOD,
        'horizon': [_iso(horizon_start), _iso(horizon_end)],
        'workcenters': [{
            'key': data.key, 'power_kw': data.power_kw,
            'working': [[_iso(start), _iso(end)] for start, end in data.working],
            'busy': [[_iso(start), _iso(end)] for start, end in data.busy],
            'prices': [[_iso(start), price] for start, price in sorted((data.prices or {}).items())],
        } for data in sorted(workcenters, key=lambda data: data.key)],
        'jobs': [{
            'key': job.key, 'workcenter': job.workcenter, 'minutes': job.minutes,
            'release': _iso(job.release), 'due': _iso(job.due),
            'predecessors': sorted(job.predecessors), 'current_start': _iso(job.current_start),
            'current_end': _iso(job.current_end), 'blocked_reason': job.blocked_reason,
        } for job in sorted(jobs, key=lambda job: job.key)],
    }


def load_inputs(snapshot):
    workcenters = [WorkcenterInput(
        key=data['key'], power_kw=data['power_kw'],
        working=tuple((_parse(start), _parse(end)) for start, end in data['working']),
        busy=tuple((_parse(start), _parse(end)) for start, end in data['busy']),
        prices={_parse(start): price for start, price in data['prices']},
    ) for data in snapshot['workcenters']]
    jobs = [JobInput(
        key=data['key'], workcenter=data['workcenter'], minutes=data['minutes'],
        release=_parse(data['release']), due=_parse(data['due']),
        predecessors=tuple(data['predecessors']), current_start=_parse(data['current_start']),
        current_end=_parse(data['current_end']),
        blocked_reason=data['blocked_reason'],
    ) for data in snapshot['jobs']]
    start, end = snapshot['horizon']
    return _parse(start), _parse(end), workcenters, jobs

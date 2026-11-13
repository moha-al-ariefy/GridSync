import csv
import io
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .local_tariffs import MAX_CSV_BYTES, MAX_CSV_ROWS, PRICE_PATTERN
from .scheduler import STEP, floor_slot


STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::[0-5]\d)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)')


class MeterInputError(ValueError):
    pass


def _utc(value):
    if not STAMP.fullmatch(value):
        raise ValueError
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).replace(tzinfo=None)


def parse_meter_csv(content):
    if not content or len(content) > MAX_CSV_BYTES:
        raise MeterInputError('Upload a meter CSV up to 2 MB.')
    readings = []
    try:
        rows = csv.reader(io.StringIO(content.decode('utf-8-sig'), newline=''), strict=True)
        if next(rows, []) != ['start', 'end', 'kwh']:
            raise MeterInputError('Use the CSV header start,end,kwh.')
        for number, row in enumerate(rows, 2):
            if not row:
                continue
            if len(row) != 3 or len(readings) >= MAX_CSV_ROWS:
                raise MeterInputError(f'Row {number}: expected three columns. Import at most {MAX_CSV_ROWS:,} readings.')
            start, end, amount = (value.strip() for value in row)
            try:
                start, end = _utc(start), _utc(end)
            except (ValueError, OverflowError) as exc:
                raise MeterInputError(f'Row {number}: use valid timestamps with Z or a UTC offset.') from exc
            if not timedelta(0) < end - start <= STEP:
                raise MeterInputError(f'Row {number}: an interval must be longer than zero and at most 15 minutes.')
            if not PRICE_PATTERN.fullmatch(amount) or not 0 <= Decimal(amount) < 1_000_000_000:
                raise MeterInputError(f'Row {number}: enter non-negative kWh with up to six decimal places.')
            readings.append((start, end, Decimal(amount)))
    except (UnicodeError, csv.Error) as exc:
        raise MeterInputError('Upload a well-formed UTF-8 CSV.') from exc
    if not readings:
        raise MeterInputError('The CSV has no meter readings.')
    readings.sort()
    if any(end > next_start for (_, end, _), (next_start, _, _) in zip(readings, readings[1:])):
        raise MeterInputError('Meter intervals overlap or appear more than once.')
    return readings


def merge_times(windows):
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def price_readings(readings, prices, offset=timedelta(0)):
    cost = Decimal(0)
    used = set()
    for start, end, kwh in readings:
        start, end = start + offset, end + offset
        slot = floor_slot(start)
        seconds = Decimal((end - start).total_seconds())
        while slot < end:
            if slot not in prices:
                raise MeterInputError(f'An electricity price is missing at {slot.isoformat()} UTC.')
            overlap = Decimal((min(end, slot + STEP) - max(start, slot)).total_seconds())
            cost += kwh * overlap / seconds * prices[slot] / 1000
            used.add(slot)
            slot += STEP
    return cost, used

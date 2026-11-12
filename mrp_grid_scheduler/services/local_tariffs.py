import csv
import io
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

import pytz


STEP = timedelta(minutes=15)
MAX_DAYS = 92
MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 35_200
WEEKDAYS = [
    ('all', 'Every day'), ('weekday', 'Monday to Friday'), ('weekend', 'Saturday and Sunday'),
    ('0', 'Monday'), ('1', 'Tuesday'), ('2', 'Wednesday'), ('3', 'Thursday'),
    ('4', 'Friday'), ('5', 'Saturday'), ('6', 'Sunday'),
]
CSV_COLUMNS = ['start', 'price_per_mwh', 'currency']
CLOCK_PATTERN = re.compile(r'(?:[01][0-9]|2[0-3]):(?:00|15|30|45)')
ISO_START = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::00)?(?:Z|[+-]\d{2}:\d{2})')
PRICE_PATTERN = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]{1,6})?|\.[0-9]{1,6})')


class TariffInputError(ValueError):
    pass


def clock_minutes(value, end=False):
    if end and value == '24:00':
        return 1440
    if not value or not CLOCK_PATTERN.fullmatch(value):
        raise TariffInputError('Use 24-hour times on a 15-minute boundary, such as 08:15.')
    hour, minute = map(int, value.split(':'))
    return 60 * hour + minute


def _matches_day(day, selector):
    weekday = day.weekday()
    return selector == 'all' or (
        weekday < 5 if selector == 'weekday' else
        weekday >= 5 if selector == 'weekend' else selector == str(weekday)
    )


def _utc_starts(zone, local_start):
    try:
        local = [zone.localize(local_start, is_dst=None)]
    except pytz.NonExistentTimeError:
        return []
    except pytz.AmbiguousTimeError:
        local = [zone.localize(local_start, is_dst=choice) for choice in (True, False)]
    return sorted(value.astimezone(timezone.utc).replace(tzinfo=None) for value in local)


def expand_profile(rules, zone_name, first_day, last_day):
    days = (last_day - first_day).days + 1
    if not 1 <= days <= MAX_DAYS:
        raise TariffInputError(f'Choose between 1 and {MAX_DAYS} days per generation.')
    zone = pytz.timezone(zone_name)
    rows = []
    missing = 0
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        prices = {}
        for rule in rules:
            if not (_matches_day(day, rule['weekday']) and
                    (rule['date_from'] or date.min) <= day <= (rule['date_to'] or date.max)):
                continue
            for minute in range(rule['start'], rule['end'], 15):
                if minute in prices:
                    hour, part = divmod(minute, 60)
                    raise TariffInputError(f'Rules overlap on {day.isoformat()} at {hour:02d}:{part:02d}.')
                prices[minute] = rule['price']
        midnight = datetime.combine(day, time.min)
        for minute in range(0, 1440, 15):
            starts = _utc_starts(zone, midnight + timedelta(minutes=minute))
            if minute in prices:
                if any(start.minute % 15 or start.second for start in starts):
                    raise TariffInputError('This timezone and date do not align to 15-minute UTC intervals.')
                rows.extend((start, prices[minute]) for start in starts)
            else:
                missing += len(starts)
    if not rows:
        raise TariffInputError('No rules cover the selected dates.')
    return sorted(rows), missing


def parse_tariff_csv(content, currency):
    if not content or len(content) > MAX_CSV_BYTES:
        raise TariffInputError('Upload a CSV file up to 2 MB.')
    try:
        reader = csv.reader(io.StringIO(content.decode('utf-8-sig'), newline=''), strict=True)
        if next(reader, []) != CSV_COLUMNS:
            raise TariffInputError('Use the header start,price_per_mwh,currency.')
        rows = {}
        for line, row in enumerate(reader, 2):
            if not row:
                continue
            if len(row) != len(CSV_COLUMNS) or len(rows) >= MAX_CSV_ROWS:
                raise TariffInputError(f'Line {line}: expected three columns and at most {MAX_CSV_ROWS:,} intervals.')
            start, price, code = (value.strip() for value in row)
            if code != currency:
                raise TariffInputError(f'Line {line}: currency must be {currency}.')
            if not ISO_START.fullmatch(start):
                raise TariffInputError(f'Line {line}: use an ISO timestamp with Z or an explicit UTC offset.')
            try:
                instant = datetime.fromisoformat(start.replace('Z', '+00:00')).astimezone(timezone.utc).replace(tzinfo=None)
            except (ValueError, OverflowError) as exc:
                raise TariffInputError(f'Line {line}: invalid timestamp.') from exc
            if instant.minute % 15 or instant.second or instant in rows:
                raise TariffInputError(f'Line {line}: duplicate timestamp or start outside a 15-minute boundary.')
            if not PRICE_PATTERN.fullmatch(price):
                raise TariffInputError(f'Line {line}: use a decimal price with up to six decimal places.')
            try:
                amount = Decimal(price)
                if abs(amount) >= Decimal('10000000000'):
                    raise InvalidOperation
            except InvalidOperation as exc:
                raise TariffInputError(f'Line {line}: price is outside the supported range.') from exc
            rows[instant] = float(amount)
    except (UnicodeError, csv.Error) as exc:
        raise TariffInputError('The file must be a well-formed UTF-8 CSV.') from exc
    if not rows:
        raise TariffInputError('The CSV has no price intervals.')
    ordered = sorted(rows.items())
    missing = (ordered[-1][0] - ordered[0][0]) // STEP + 1 - len(ordered)
    return ordered, missing

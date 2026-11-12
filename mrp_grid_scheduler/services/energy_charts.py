import math
from datetime import datetime, timedelta, timezone


class PriceDataError(ValueError):
    pass


def _utc_datetime(value):
    if not isinstance(value, str):
        raise PriceDataError('A timestamp is missing.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise PriceDataError('A timestamp is invalid.') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PriceDataError('A timestamp has no timezone.')
    try:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except OverflowError as exc:
        raise PriceDataError('A timestamp is outside the supported UTC range.') from exc


def parse_prices(payload):
    if not isinstance(payload, dict):
        raise PriceDataError('The response is not an object.')
    if payload.get('endpoint') != 'price' or payload.get('bidding_zone') != 'DE-LU':
        raise PriceDataError('The response is not for the DE-LU price market.')
    unit = payload.get('unit')
    if not isinstance(unit, str) or unit.replace(' ', '') != 'EUR/MWh':
        raise PriceDataError('The response has an unexpected currency or unit.')
    if payload.get('interval_minutes') != 15 or payload.get('deprecated') is not False:
        raise PriceDataError('The response has an unsupported interval or endpoint status.')
    license_text = payload.get('license')
    if not isinstance(license_text, str) or 'CC BY 4.0' not in license_text:
        raise PriceDataError('The data license needs review before it can be imported.')
    generated_at = _utc_datetime(payload.get('generated_at'))
    data = payload.get('data')
    if not isinstance(data, list) or not 1 <= len(data) <= 110:
        raise PriceDataError('The response has no usable daily price intervals.')
    slots = []
    missing_starts = []
    seen = set()
    for row in data:
        if not isinstance(row, dict) or not isinstance(row.get('values'), dict):
            raise PriceDataError('A price interval is malformed.')
        start = _utc_datetime(row.get('timestamp'))
        if start.second or start.microsecond or start.minute % 15 or start in seen:
            raise PriceDataError('The response has a duplicate or misaligned interval.')
        seen.add(start)
        price = row['values'].get('day_ahead_price')
        if price is None:
            missing_starts.append(start)
            continue
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise PriceDataError('A price is not a finite number.')
        try:
            price = float(price)
        except OverflowError as exc:
            raise PriceDataError('A price is outside the supported range.') from exc
        if not math.isfinite(price) or abs(price) >= 10_000_000_000:
            raise PriceDataError('A price is outside the supported range.')
        slots.append((start, price))
    if not slots:
        raise PriceDataError('The response contains no prices.')
    first, last = min(seen), max(seen)
    step = timedelta(minutes=15)
    if last - first >= timedelta(hours=25):
        raise PriceDataError('The response spans more than one market day.')
    missing_starts.extend(first + offset * step for offset in range((last - first) // step + 1)
                          if first + offset * step not in seen)
    return {
        'generated_at': generated_at,
        'license': license_text,
        'slots': sorted(slots),
        'missing': len(missing_starts),
        'missing_starts': missing_starts,
    }

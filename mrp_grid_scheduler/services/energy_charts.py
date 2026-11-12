import math
from datetime import datetime, timezone


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
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def parse_prices(payload):
    if not isinstance(payload, dict):
        raise PriceDataError('The response is not an object.')
    if payload.get('endpoint') != 'price' or payload.get('bidding_zone') != 'DE-LU':
        raise PriceDataError('The response is not for the DE-LU price market.')
    if payload.get('unit', '').replace(' ', '') != 'EUR/MWh':
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
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
            raise PriceDataError('A price is not a finite number.')
        slots.append((start, float(price)))
    if not slots:
        raise PriceDataError('The response contains no prices.')
    return {
        'generated_at': generated_at,
        'license': license_text,
        'slots': sorted(slots),
        'missing': len(missing_starts),
        'missing_starts': missing_starts,
    }

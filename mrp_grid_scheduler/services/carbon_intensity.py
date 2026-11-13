from datetime import timedelta

from .energy_charts import PriceDataError, _utc_datetime


def parse_neso_intensity(payload, day):
    rows = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 48:
        raise PriceDataError('NESO returned no usable half-hour intervals.')
    intervals = {}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('intensity'), dict):
            raise PriceDataError('A carbon intensity interval is malformed.')
        start, end = _utc_datetime(row.get('from')), _utc_datetime(row.get('to'))
        if (start.date() != day or start.minute not in (0, 30) or start.second or start.microsecond
                or end - start != timedelta(minutes=30) or start in seen):
            raise PriceDataError('A carbon intensity interval is outside the selected day, misaligned, or repeated.')
        seen.add(start)
        intensity = row['intensity']
        actual, forecast = intensity.get('actual'), intensity.get('forecast')
        for value in (actual, forecast):
            if value is not None and (type(value) not in (int, float) or not 0 <= value <= 10_000):
                raise PriceDataError('A carbon intensity value is outside the supported range.')
        if actual is not None or forecast is not None:
            intervals[start] = {'actual': actual, 'forecast': forecast}
    if not intervals:
        raise PriceDataError('NESO returned no carbon intensity values.')
    return intervals

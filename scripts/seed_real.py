# GridSync real-tariff data loader.
# For development and demos only: it commits data, so never run it on a shared or production database.
# Run from the repository root in PowerShell, against your development database:
#   $o = "C:\Program Files\Odoo 19.0.20260714"
#   $ap = (Select-String -Path "$o\server\odoo.conf" -Pattern '^addons_path').Line -replace '^addons_path\s*=\s*', ''
#   Start-Process "$o\python\python.exe" -ArgumentList "`"$o\server\odoo-bin`"", 'shell', '-c', "`"$o\server\odoo.conf`"", `
#       "`"--addons-path=$ap,$PWD`"", '-d', 'gridsync_dev', '--no-http' -RedirectStandardInput scripts\seed_real.py -NoNewWindow -Wait
# The add-on path is the one from odoo.conf plus this repository, which odoo.conf does not list.
# Do not pipe the file with Get-Content: Windows PowerShell adds a byte-order mark that Python rejects.
#
# Safe to run again: each part is skipped if it already exists. Run it again after 14:00 Berlin time
# (17:00 Dubai) to add the Germany part once tomorrow's day-ahead prices are published.
#
# Sources (check them before presenting, tariffs and fuel surcharges change):
#   ADDC 2025 business tariff, industrial over 1 MW: 27.0 fils/kWh, 36.6 fils/kWh from 10:00 to 22:00, 1 June-30 September
#     https://www.addc.ae/en-US/business/Documents/2025%20Tariff%20(English).pdf
#   DEWA industrial slab tariff: 23 fils/kWh up to 10,000 kWh a month, 38 fils above
#     https://dewa.gov.ae/en/consumer/billing/slab-tariff
#   DEWA electricity fuel surcharge July 2026: 6 fils/kWh (changes monthly)
#   Energy-Charts day-ahead prices, Germany-Luxembourg, CC BY 4.0: https://www.energy-charts.info
#   EUR/AED about 4.10 (27 Oct 2026 mid-market). Update the rate in Accounting > Configuration > Currencies.

from datetime import date, datetime, time, timedelta

import pytz  # zoneinfo has no time-zone data on Windows without the tzdata package

from odoo import Command

EUR_IN_AED = 4.10
USD_IN_AED = 3.6725  # fixed peg

admin = env.ref('base.user_admin')
aenv = env(user=admin)
company = aenv.company
Source = aenv['grid.tariff.source']


def utc(day, hour, minute=0, tz='Asia/Dubai'):
    local = pytz.timezone(tz).localize(datetime.combine(day, time(hour, minute)))
    return local.astimezone(pytz.utc).replace(tzinfo=None)


def exists(name):
    return Source.with_context(active_test=False).search_count([('name', '=', name)]) > 0


def calendar(name, tz):
    found = aenv['resource.calendar'].search([('name', '=', name)], limit=1)
    return found or aenv['resource.calendar'].create({
        'name': name, 'tz': tz, 'company_id': company.id,
        'attendance_ids': [Command.clear()] + [
            Command.create({'name': f'Day {d} {a}-{b}', 'dayofweek': str(d), 'hour_from': a, 'hour_to': b,
                            'day_period': 'morning' if a < 12 else 'afternoon'})
            for d in range(7) for a, b in [(0, 12), (12, 24)]
        ],
    })


def centers(site_name, tariff, cal, machines):
    created = aenv['mrp.workcenter']
    for name, kw, reference in machines:
        created |= aenv['mrp.workcenter'].create({
            'name': name, 'company_id': company.id, 'resource_calendar_id': cal.id,
            'grid_power_kw': kw, 'grid_power_reference': reference,
            'grid_tariff_source_id': tariff.id, 'grid_allow_shift': True,
        })
    aenv['grid.site'].create({'name': site_name, 'company_id': company.id,
                              'workcenter_ids': [Command.set(created.ids)]})
    return created


def product(name):
    return aenv['product.product'].search([('name', '=', name)], limit=1) or \
        aenv['product.product'].create({'name': name})


def order(prod, operations, deadline):
    production = aenv['mrp.production'].create({
        'product_id': prod.id, 'product_qty': 1, 'product_uom_id': prod.uom_id.id,
        'workorder_ids': [Command.create({
            'name': name, 'workcenter_id': wc.id, 'product_uom_id': prod.uom_id.id, 'duration_expected': minutes,
        }) for name, wc, minutes, _start in operations],
    })
    production.action_confirm()
    production.date_deadline = deadline
    for workorder, (_n, _w, minutes, start) in zip(production.workorder_ids.sorted(lambda w: (w.sequence, w.id)),
                                                   operations):
        workorder.write({'date_start': start, 'date_finished': start + timedelta(minutes=minutes)})
    return production


def run(name, start, end, workcenters):
    proposal = aenv['grid.schedule.run'].create({
        'name': name, 'date_from': start, 'date_to': end, 'workcenter_ids': [Command.set(workcenters.ids)],
    })
    try:
        proposal.action_compute()
        return (f'{name}: {proposal.moved_count}/{proposal.job_count} moved, '
                f'{proposal.compared_current_cost:.2f} -> {proposal.compared_proposed_cost:.2f} '
                f'{proposal.currency_id.name} ({proposal.saving_percent:.1f}% saving)')
    except Exception as exc:
        return f'{name}: created, compute it in the app ({exc})'


def ensure_rate(code, units_per_aed):
    """Make a currency usable. units_per_aed: how many units of it one AED buys."""
    currency = env.ref(f'base.{code}')
    currency.active = True
    home = company.currency_id.name
    if home == code:
        return
    per_company = {'AED': units_per_aed, 'USD': units_per_aed * USD_IN_AED}.get(home)
    if per_company is None:
        print(f'Add an exchange rate for {code} yourself (company currency is {home}).')
        return
    rates = env['res.currency.rate']
    if not rates.search_count([('currency_id', '=', currency.id), ('company_id', 'in', [False, company.id])]):
        rates.create({'currency_id': currency.id, 'company_id': company.id,
                      'name': date.today() - timedelta(days=1), 'rate': per_company})


ensure_rate('AED', 1.0)
ensure_rate('EUR', 1 / EUR_IN_AED)
results = []
today = datetime.now(pytz.timezone('Asia/Dubai')).date()
dubai_24h = calendar('24/7 shifts, Dubai', 'Asia/Dubai')

# ---------------------------------------------------------------- ADDC: real UAE time-of-use tariff
ADDC = 'ADDC Industrial over 1 MW (2025 tariff)'
if not exists(ADDC):
    addc = Source.create({
        'name': ADDC, 'source_type': 'manual_tou', 'currency_id': env.ref('base.AED').id,
        'timezone': 'Asia/Dubai', 'valid_from': date(2026, 1, 1), 'valid_to': date(2027, 12, 31),
        'source_reference': 'ADDC 2025 business tariff: industrial over 1 MW, 27.0 fils/kWh, '
                            '36.6 fils/kWh 10:00-22:00 from 1 June to 30 September',
    })
    rules = []
    for year in (2026, 2027):
        summer = (date(year, 6, 1), date(year, 9, 30))
        rules += [
            (date(year, 1, 1), date(year, 5, 31), '00:00', '24:00', 0.270),
            (*summer, '00:00', '10:00', 0.270),
            (*summer, '10:00', '22:00', 0.366),
            (*summer, '22:00', '24:00', 0.270),
            (date(year, 10, 1), date(year, 12, 31), '00:00', '24:00', 0.270),
        ]
    addc.write({'rule_ids': [Command.create({'date_from': a, 'date_to': b, 'start_time': s, 'end_time': e,
                                             'price_per_kwh': p, 'weekday': 'all'}) for a, b, s, e, p in rules]})
    # Prices for a summer week next year, and for the next two weeks (flat in winter).
    week = date(2027, 6, 7)  # a Monday
    addc._generate_profile_intervals(week, week + timedelta(days=7))
    addc._generate_profile_intervals(today, today + timedelta(days=14))

    plant = centers('Abu Dhabi plant (ADDC)', addc, dubai_24h, [
        ('Aluminium extrusion press (ADDC)', 400, 'Typical 2,500-tonne extrusion press, assumed'),
        ('Induction melting furnace (ADDC)', 750, 'Typical 1-tonne induction furnace, assumed'),
        ('Packing line (ADDC)', 40, 'Assumed'),
    ])
    press, furnace, packing = plant
    profile, ingot = product('Aluminium window profile'), product('Aluminium ingot batch')
    deadline = utc(week + timedelta(days=6), 22)
    for offset in (1, 2, 3):
        day = week + timedelta(days=offset)
        order(ingot, [('Melt and cast', furnace, 180, utc(day, 11))], deadline)
        order(profile, [('Extrude', press, 240, utc(day, 13)), ('Pack', packing, 60, utc(day, 17, 30))], deadline)
    results.append(run('ADDC summer week, June 2027 (real tariff)', utc(week, 0), utc(week + timedelta(days=7), 0), plant))

# ---------------------------------------------------------------- DEWA: real flat industrial tariff
DEWA = 'DEWA Industrial over 10,000 kWh/month (flat)'
if not exists(DEWA):
    dewa = Source.create({
        'name': DEWA, 'source_type': 'manual_tou', 'currency_id': env.ref('base.AED').id,
        'timezone': 'Asia/Dubai', 'valid_from': date(2026, 1, 1), 'valid_to': date(2027, 12, 31),
        'source_reference': 'DEWA industrial slab 38 fils/kWh above 10,000 kWh a month '
                            '+ 6 fils/kWh fuel surcharge (July 2026). Same price all day.',
        'rule_ids': [Command.create({'start_time': '00:00', 'end_time': '24:00', 'price_per_kwh': 0.44,
                                     'weekday': 'all'})],
    })
    dewa._generate_profile_intervals(today, today + timedelta(days=14))
    (cnc,) = centers('Dubai plant (DEWA)', dewa, dubai_24h, [('CNC machining line (DEWA)', 60, 'Assumed')])
    tomorrow = today + timedelta(days=1)
    order(product('Steel bracket (DEWA)'), [('Machine', cnc, 180, utc(tomorrow, 13))], utc(tomorrow, 23))
    results.append(run('Dubai, flat tariff (real DEWA rate)', utc(tomorrow, 0),
                       utc(tomorrow + timedelta(days=1), 0), cnc))

# ---------------------------------------------------------------- Energy-Charts: real live market prices
EC = 'Germany-Luxembourg day-ahead (Energy-Charts, live)'
ec = Source.search([('name', '=', EC)], limit=1)
if not ec:
    ec = Source.create({'name': EC, 'source_type': 'energy_charts', 'energy_charts_market': 'DE-LU',
                        'currency_id': env.ref('base.EUR').id, 'timezone': 'Europe/Berlin'})
try:
    print('Energy-Charts:', ec.action_sync_energy_charts().get('params', {}).get('message', 'synced'))
except Exception as exc:
    print('Energy-Charts sync failed:', exc)

berlin_today = datetime.now(pytz.timezone('Europe/Berlin')).date()
berlin_tomorrow = berlin_today + timedelta(days=1)
day_start, day_end = utc(berlin_tomorrow, 0, tz='Europe/Berlin'), utc(berlin_tomorrow + timedelta(days=1), 0, tz='Europe/Berlin')
tomorrow_prices = aenv['grid.tariff.slot'].search_count([('source_id', '=', ec.id), ('start_utc', '>=', day_start),
                                                         ('start_utc', '<', day_end)])
if tomorrow_prices < 96:
    print(f'Tomorrow\'s German prices are not published yet ({tomorrow_prices}/96 intervals). '
          'Run this script again after 14:00 Berlin time (17:00 Dubai) to add the Germany plant.')
elif not aenv['grid.site'].search_count([('name', '=', 'Germany plant (Energy-Charts)')]):
    berlin_24h = calendar('24/7 shifts, Berlin', 'Europe/Berlin')
    plant = centers('Germany plant (Energy-Charts)', ec, berlin_24h, [
        ('Electric arc furnace (DE)', 900, 'Assumed'),
        ('Cold rolling mill (DE)', 300, 'Assumed'),
    ])
    furnace, mill = plant
    coil = product('Steel coil batch (DE)')
    deadline = utc(berlin_tomorrow, 23, 45, tz='Europe/Berlin')
    order(coil, [('Melt', furnace, 120, utc(berlin_tomorrow, 7, tz='Europe/Berlin')),
                 ('Roll', mill, 120, utc(berlin_tomorrow, 18, tz='Europe/Berlin'))], deadline)
    order(coil, [('Melt', furnace, 120, utc(berlin_tomorrow, 18, tz='Europe/Berlin'))], deadline)
    results.append(run('Germany tomorrow (live day-ahead prices)', day_start, day_end, plant))

env.cr.commit()
print('\n' + ('\n'.join(results) if results else 'Nothing new to add.') + '\n')

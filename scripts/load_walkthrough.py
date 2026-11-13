import base64
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import UserError


def load_walkthrough(env):
    if not env['ir.config_parameter'].sudo().get_param('database.is_neutralized'):
        raise UserError('Use a neutralized Odoo test database for the walkthrough.')

    admin = env.ref('base.user_admin')
    company = admin.company_id
    env = env(user=admin.id, context={'allowed_company_ids': [company.id], 'tz': 'UTC'})
    prefix = f'GridSync test {fields.Datetime.now():%Y-%m-%d %H:%M:%S} UTC'
    tariff = env['grid.tariff.source'].create({
        'name': f'{prefix} DE-LU',
        'source_type': 'energy_charts',
        'energy_charts_market': 'DE-LU',
    })
    status = tariff.action_sync_energy_charts()
    if status['params']['type'] != 'success':
        raise UserError(status['params']['message'])

    step = timedelta(minutes=15)
    prices = {
        slot.start_utc: slot.price_per_mwh
        for slot in tariff.slot_ids if slot.is_current
    }
    pairs = {}
    for start, price in sorted(prices.items()):
        following = start + step
        if following in prices and (following + step).date() == start.date():
            pairs.setdefault(start.date(), []).append((price + prices[following], start))

    cheap_start = original_start = None
    for day_pairs in reversed(list(pairs.values())):
        cheap_price, cheap_start = min(day_pairs)
        alternatives = [pair for pair in day_pairs if abs(pair[1] - cheap_start) >= 2 * step]
        expensive_price, original_start = max(alternatives, default=(cheap_price, cheap_start))
        low, high = sorted((cheap_start, original_start))
        slots = (high - low) // step + 2
        if expensive_price > cheap_price and all(low + step * index in prices for index in range(slots)):
            break
    else:
        raise UserError('The published feed has no complete window with two different prices.')

    now = fields.Datetime.now()
    offset = timedelta(days=(now.date() - low.date()).days + 1)
    replay = env['grid.tariff.source'].create({
        'name': f'{prefix} DE-LU replay', 'source_type': 'csv',
        'currency_id': tariff.currency_id.id, 'timezone': 'UTC',
        'source_reference': f'Energy-Charts DE-LU wholesale, {low} to {high + 2 * step} UTC, replayed on {(low + offset).date()}.',
        'attribution': tariff.attribution, 'csv_price_basis': 'provided',
    })
    csv = ['start,price_per_mwh,currency']
    for index in range(slots):
        published = low + index * step
        csv.append(f'{(published + offset).isoformat()}Z,{prices[published]:.6f},{tariff.currency_id.name}')
    env['grid.tariff.load'].create({
        'source_id': replay.id, 'csv_file': base64.b64encode(('\n'.join(csv) + '\n').encode()),
    }).action_load()
    tariff.active = False
    cheap_start += offset
    original_start += offset

    calendar = env['resource.calendar'].create({
        'name': f'{prefix} hours',
        'company_id': company.id,
        'tz': 'UTC',
        'attendance_ids': [Command.clear(), Command.create({
            'name': 'Walkthrough hours', 'dayofweek': str(original_start.weekday()),
            'hour_from': 0, 'hour_to': 24, 'day_period': 'morning',
        })],
    })
    center = env['mrp.workcenter'].create({
        'name': f'{prefix} press', 'company_id': company.id, 'resource_calendar_id': calendar.id,
        'grid_power_kw': 40, 'grid_power_reference': 'Walkthrough assumption',
        'grid_tariff_source_id': replay.id, 'grid_allow_shift': True,
    })
    site = env['grid.site'].create({'name': prefix, 'workcenter_ids': [Command.link(center.id)]})
    product = env['product.product'].create({'name': f'{prefix} part'})
    bom = env['mrp.bom'].create({
        'product_tmpl_id': product.product_tmpl_id.id, 'company_id': company.id,
        'allow_operation_dependencies': True,
    })
    routing = env['mrp.routing.workcenter']
    cut = routing.create({
        'name': 'Cut test part', 'bom_id': bom.id, 'workcenter_id': center.id,
        'sequence': 10, 'time_mode': 'manual', 'time_cycle_manual': 15,
    })
    finish = routing.create({
        'name': 'Finish test part', 'bom_id': bom.id, 'workcenter_id': center.id,
        'sequence': 20, 'time_mode': 'manual', 'time_cycle_manual': 15,
        'blocked_by_operation_ids': [Command.link(cut.id)],
    })
    start = min(cheap_start, original_start)
    end = max(cheap_start, original_start) + 2 * step
    production = env['mrp.production'].create({
        'product_id': product.id, 'bom_id': bom.id, 'product_qty': 1,
        'product_uom_id': product.uom_id.id, 'company_id': company.id,
    })
    production.action_confirm()
    production.date_deadline = end
    cut_order = production.workorder_ids.filtered_domain([('operation_id', '=', cut.id)])
    finish_order = production.workorder_ids.filtered_domain([('operation_id', '=', finish.id)])
    if cut_order != finish_order.blocked_by_workorder_ids:
        raise UserError('Odoo did not create the expected route dependency.')
    cut_order.write({'date_start': original_start, 'date_finished': original_start + step})
    finish_order.write({'date_start': original_start + step, 'date_finished': original_start + 2 * step})

    proposal = env['grid.schedule.run'].create({
        'name': f'{prefix} two-step route', 'date_from': start, 'date_to': end,
        'workcenter_ids': [Command.link(center.id)],
    })
    proposal.action_compute()
    if not proposal.complete or not proposal.has_current_cost or proposal.saving <= 0:
        raise UserError('GridSync could not produce a cheaper complete proposal for the published prices.')

    print(f'Site: {site.name}. Manufacturing order: {production.name}.')
    print(f'Open GridSync > Schedule and look for {proposal.name}.')
    print(f'Proposal URL path: /odoo/grid.schedule.run/{proposal.id}')
    print(f'Reviewed estimate: {proposal.current_cost:.2f} to {proposal.proposed_cost:.2f} {proposal.currency_id.name}.')


load_walkthrough(env)
env.cr.commit()

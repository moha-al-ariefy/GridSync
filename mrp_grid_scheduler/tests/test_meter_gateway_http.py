from datetime import datetime, timedelta

from odoo import Command
from odoo.tests import HttpCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestMeterGatewayHttp(HttpCase):
    def test_gateway_upload_and_retry_over_json2(self):
        company = self.env.company
        gateway = new_test_user(
            self.env, login='grid_gateway_http', groups='mrp_grid_scheduler.group_grid_meter_gateway',
            company_id=company.id, company_ids=[Command.set(company.ids)],
        )
        centers = self.env['mrp.workcenter'].sudo()
        press = centers.create({'name': 'Gateway test press', 'company_id': company.id})
        other = centers.create({'name': 'Unassigned test press', 'company_id': company.id})
        gateway.sudo().grid_meter_workcenter_ids = [Command.set(press.ids)]
        key = self.env['res.users.apikeys'].with_user(gateway)._generate(
            None, 'GridSync HTTP test', datetime.now() + timedelta(days=1),
        )
        endpoint = '/json/2/grid.meter.batch/submit_readings'
        payload = {
            'workcenter_id': press.id,
            'meter_reference': 'Press meter',
            'batch_reference': 'HTTP upload 1',
            'csv_text': 'start,end,kwh\n2030-01-07T13:00Z,2030-01-07T13:15Z,6\n',
        }
        self.assertEqual(self.url_open(endpoint, json=payload).status_code, 401)
        headers = {'Authorization': f'bearer {key}'}
        response = self.url_open(endpoint, json=payload, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        batch_id = response.json()
        self.assertIs(type(batch_id), int)
        retry = self.url_open(endpoint, json=payload, headers=headers)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json(), batch_id)
        denied = self.url_open(endpoint, json=dict(payload, workcenter_id=other.id), headers=headers)
        self.assertEqual(denied.status_code, 403, denied.text)
        batch = self.env['grid.meter.batch'].sudo().browse(batch_id)
        self.assertEqual(batch.workcenter_id, press)
        self.assertEqual(batch.received_by, gateway)

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";

export class GridGuide extends Component {
    static template = "mrp_grid_scheduler.GridGuide";
    static props = { ...standardActionServiceProps };

    setup() {
        this.action = useService("action");
    }

    openSites() {
        return this.action.doAction("mrp_grid_scheduler.action_grid_site");
    }
}

registry.category("actions").add("mrp_grid_scheduler.guide", GridGuide);

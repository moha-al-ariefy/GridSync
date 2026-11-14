import { browser } from "@web/core/browser/browser";
import { user } from "@web/core/user";
import { session } from "@web/session";
import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";

export class GridAbout extends Component {
    static template = "mrp_grid_scheduler.GridAbout";
    static props = { ...standardActionServiceProps };

    setup() {
        this.action = useService("action");
        // The key keeps its original name so users who already closed the tutorial don't see it again.
        this.tutorialKey = ["gridsync.tutorial.v1", session.db, user.userId].join(".");
        this.state = useState({
            tutorialOpen: !browser.localStorage.getItem(this.tutorialKey),
        });
    }

    showTutorial() {
        this.state.tutorialOpen = true;
    }

    closeTutorial() {
        this.state.tutorialOpen = false;
        try {
            browser.localStorage.setItem(this.tutorialKey, "seen");
        } catch {
            // Closing the tutorial still works when browser storage is full.
        }
    }

    openWorkCenters() {
        return this.action.doAction("mrp_grid_scheduler.action_grid_workcenters");
    }

    openSchedule() {
        return this.action.doAction("mrp_grid_scheduler.action_grid_schedule_run");
    }

    openTariffs() {
        return this.action.doAction("mrp_grid_scheduler.action_grid_tariff_source");
    }
}

registry.category("actions").add("mrp_grid_scheduler.about", GridAbout);

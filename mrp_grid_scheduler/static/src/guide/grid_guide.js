import { browser } from "@web/core/browser/browser";
import { user } from "@web/core/user";
import { session } from "@web/session";
import { Component, onMounted, onWillStart, useState } from "@odoo/owl";
import { deserializeDateTime, formatDateTime, serializeDateTime } from "@web/core/l10n/dates";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { formatFloat } from "@web/views/fields/formatters";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";

const { DateTime } = luxon;
const SOURCE_FIELDS = [
    "name", "company_id", "currency_id", "source_type", "last_sync_at", "energy_charts_market", "profile_dirty", "csv_price_basis",
];
const SLOT_FIELDS = ["start_utc", "price_per_mwh", "minutes"];

export class GridGuide extends Component {
    static template = "mrp_grid_scheduler.GridGuide";
    static props = { ...standardActionServiceProps };

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");
        this.tutorialKey = ["gridsync.tutorial.v1", session.db, user.userId].join(".");
        this.state = useState({
            tutorialOpen: !browser.localStorage.getItem(this.tutorialKey),
            sources: [],
            sourceId: null,
            slots: [],
            lastLoad: null,
            historical: false,
            loading: false,
            failed: false,
            refreshFailed: false,
        });
        this.loadSequence = 0;
        onWillStart(() => this.loadSources());
        onMounted(() => this.refreshPrices());
    }

    get source() {
        return this.state.sources.find((source) => source.id === this.state.sourceId);
    }

    get oldMarketSync() {
        return this.source?.source_type === "energy_charts" && this.source.last_sync_at &&
            DateTime.now().diff(deserializeDateTime(this.source.last_sync_at), "minutes").minutes > 15;
    }

    get intervalGap() {
        return this.state.slots.some((slot, index, slots) => index > 0 &&
            deserializeDateTime(slot.start_utc).diff(deserializeDateTime(slots[index - 1].start_utc), "minutes").minutes > 15);
    }

    get timezone() {
        return DateTime.local().zoneName;
    }

    async loadSources() {
        try {
            this.state.failed = false;
            this.state.sources = await this.orm.searchRead("grid.tariff.source", [], SOURCE_FIELDS);
            const source = this.source || this.state.sources[0];
            if (source) {
                await this.selectSource(source.id);
            }
        } catch {
            this.state.failed = true;
        }
    }

    async refreshPrices() {
        if (!this.state.sources.some((source) => source.source_type === "energy_charts")) {
            return;
        }
        try {
            const refreshed = await this.orm.silent.call("grid.tariff.source", "refresh_energy_charts");
            if (refreshed) {
                await this.loadSources();
            } else if (this.source?.source_type === "energy_charts") {
                const sequence = this.loadSequence;
                const lastLoad = await this.readLastLoad(this.source.id);
                if (sequence === this.loadSequence) {
                    this.state.lastLoad = lastLoad;
                }
            }
        } catch {
            this.state.refreshFailed = true;
        }
    }

    async readLastLoad(sourceId) {
        const [lastLoad] = await this.orm.searchRead(
            "grid.tariff.sync", [["source_id", "=", sourceId]], ["state", "missing"], { limit: 1 },
        );
        return lastLoad || null;
    }

    async selectSource(sourceId) {
        const sequence = ++this.loadSequence;
        this.state.sourceId = sourceId;
        this.state.loading = true;
        this.state.failed = false;
        this.state.slots = [];
        this.state.lastLoad = null;
        const now = DateTime.utc();
        const intervalStart = now.startOf("hour").plus({ minutes: Math.floor(now.minute / 15) * 15 });
        const domain = [["source_id", "=", sourceId], ["is_current", "=", true]];
        try {
            const [upcoming, lastLoad] = await Promise.all([
                this.orm.searchRead(
                    "grid.tariff.slot",
                    [...domain, ["start_utc", ">=", serializeDateTime(intervalStart)]],
                    SLOT_FIELDS,
                    { order: "start_utc asc", limit: 8 },
                ),
                this.readLastLoad(sourceId),
            ]);
            let slots = upcoming;
            const historical = slots.length === 0;
            if (historical) {
                slots = (await this.orm.searchRead("grid.tariff.slot", domain, SLOT_FIELDS, {
                    order: "start_utc desc",
                    limit: 8,
                })).reverse();
            }
            if (sequence === this.loadSequence) {
                this.state.slots = slots;
                this.state.lastLoad = lastLoad;
                this.state.historical = historical;
            }
        } catch {
            if (sequence === this.loadSequence) {
                this.state.failed = true;
            }
        } finally {
            if (sequence === this.loadSequence) {
                this.state.loading = false;
            }
        }
    }

    dateTime(value) {
        return formatDateTime(deserializeDateTime(value));
    }

    intervalTime(slot) {
        const start = deserializeDateTime(slot.start_utc);
        const end = start.plus({ minutes: slot.minutes });
        const format = start.hasSame(end, "day") ? DateTime.TIME_SIMPLE : DateTime.DATETIME_SHORT;
        return [start.toLocaleString(DateTime.TIME_SIMPLE), end.toLocaleString(format)].join(" – ");
    }

    intervalDate(slot) {
        return deserializeDateTime(slot.start_utc).toLocaleString(DateTime.DATE_MED);
    }

    price(value) {
        return formatFloat(value, { digits: [16, 6], minDigits: 2 });
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

    openSource() {
        return this.action.doAction("mrp_grid_scheduler.action_grid_tariff_source", {
            viewType: "form",
            props: { resId: this.state.sourceId },
        });
    }
}

registry.category("actions").add("mrp_grid_scheduler.guide", GridGuide);

import { loadBundle } from "@web/core/assets";
import { Component, onMounted, onWillStart, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";
import { deserializeDateTime, formatDateTime, serializeDateTime } from "@web/core/l10n/dates";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { formatFloat, formatMonetary } from "@web/views/fields/formatters";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";

const { DateTime } = luxon;
const SOURCE_FIELDS = [
    "name", "company_id", "currency_id", "source_type", "last_sync_at", "energy_charts_market", "profile_dirty", "csv_price_basis",
];
const SLOT_FIELDS = ["start_utc", "price_per_mwh", "minutes"];
const CHART_HOURS = 24;
// Fallbacks match Odoo's light palette: brand primary, gray-300 gridlines, gray-600 text.
const CHART_FALLBACK = { line: "#71639e", grid: "#dee2e6", text: "#6c757d" };

// Read the line, grid and text colors from the active Odoo theme, so the chart follows
// the database's brand color and the light or dark color scheme.
function chartColors(element) {
    const style = getComputedStyle(element || document.documentElement);
    const read = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
    return {
        line: read("--bs-primary", CHART_FALLBACK.line),
        grid: read("--bs-border-color", CHART_FALLBACK.grid),
        text: read("--bs-secondary-color", CHART_FALLBACK.text),
    };
}

function totalsByCurrency(records, field) {
    const totals = new Map();
    for (const record of records) {
        if (!record.currency_id) {
            continue;
        }
        const [id, name] = record.currency_id;
        const total = totals.get(id) || { id, name, amount: 0 };
        total.amount += record[field] || 0;
        totals.set(id, total);
    }
    return [...totals.values()];
}

export class GridDashboard extends Component {
    static template = "mrp_grid_scheduler.GridDashboard";
    static props = { ...standardActionServiceProps };

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");
        this.chartCanvas = useRef("priceChart");
        this.chart = null;
        this.state = useState({
            sources: [],
            sourceId: null,
            slots: [],
            chartSlots: [],
            chartHistorical: false,
            lastLoad: null,
            historical: false,
            loading: false,
            failed: false,
            refreshFailed: false,
            proposalSavings: [],
            meterSavings: [],
            savingsFailed: false,
        });
        this.loadSequence = 0;
        onWillStart(() => Promise.all([this.loadSources(), this.loadSavings(), loadBundle("web.chartjs_lib")]));
        onMounted(() => this.refreshPrices());
        useEffect(() => this.renderChart(), () => [this.state.chartSlots]);
        onWillUnmount(() => this.chart?.destroy());
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

    get currentPrice() {
        return this.state.slots[0] || null;
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

    async loadSavings() {
        try {
            const [proposals, results] = await Promise.all([
                this.orm.searchRead(
                    "grid.schedule.run",
                    [["state", "=", "applied"], ["has_current_cost", "=", true]],
                    ["saving", "currency_id"],
                ),
                this.orm.searchRead(
                    "grid.energy.result",
                    [["has_baseline", "=", true]],
                    ["estimated_savings", "currency_id"],
                ),
            ]);
            this.state.proposalSavings = totalsByCurrency(proposals, "saving");
            this.state.meterSavings = totalsByCurrency(results, "estimated_savings");
            this.state.savingsFailed = false;
        } catch {
            this.state.savingsFailed = true;
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
        this.state.chartSlots = [];
        this.state.lastLoad = null;
        const now = DateTime.utc();
        const intervalStart = now.startOf("hour").plus({ minutes: Math.floor(now.minute / 15) * 15 });
        const domain = [["source_id", "=", sourceId], ["is_current", "=", true]];
        const upcomingDomain = [...domain, ["start_utc", ">=", serializeDateTime(intervalStart)]];
        try {
            const [upcoming, chartUpcoming, lastLoad] = await Promise.all([
                this.orm.searchRead("grid.tariff.slot", upcomingDomain, SLOT_FIELDS, { order: "start_utc asc", limit: 8 }),
                this.orm.searchRead(
                    "grid.tariff.slot",
                    [...upcomingDomain, ["start_utc", "<", serializeDateTime(intervalStart.plus({ hours: CHART_HOURS }))]],
                    SLOT_FIELDS,
                    { order: "start_utc asc" },
                ),
                this.readLastLoad(sourceId),
            ]);
            let slots = upcoming;
            let chartSlots = chartUpcoming;
            const historical = slots.length === 0;
            if (historical) {
                slots = (await this.orm.searchRead("grid.tariff.slot", domain, SLOT_FIELDS, {
                    order: "start_utc desc",
                    limit: 8,
                })).reverse();
            }
            const chartHistorical = chartSlots.length === 0;
            if (chartHistorical) {
                chartSlots = (await this.orm.searchRead("grid.tariff.slot", domain, SLOT_FIELDS, {
                    order: "start_utc desc",
                    limit: CHART_HOURS * 4,
                })).reverse();
            }
            if (sequence === this.loadSequence) {
                this.state.slots = slots;
                this.state.chartSlots = chartSlots;
                this.state.chartHistorical = chartHistorical;
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

    renderChart() {
        this.chart?.destroy();
        this.chart = null;
        const canvas = this.chartCanvas.el;
        const slots = this.state.chartSlots;
        if (!canvas || !slots.length) {
            return;
        }
        const colors = chartColors(canvas);
        const unit = `${this.source?.currency_id[1] || ""} / MWh`;
        const starts = slots.map((slot) => deserializeDateTime(slot.start_utc));
        const multiDay = !starts[0].hasSame(starts[starts.length - 1], "day");
        this.chart = new Chart(canvas, {
            type: "line",
            data: {
                labels: starts.map((start) => start.toLocaleString(
                    multiDay ? { weekday: "short", hour: "2-digit", minute: "2-digit" } : DateTime.TIME_SIMPLE,
                )),
                datasets: [{
                    label: unit,
                    data: slots.map((slot) => slot.price_per_mwh),
                    borderColor: colors.line,
                    backgroundColor: colors.line,
                    borderWidth: 2,
                    pointRadius: 0,
                    pointHoverRadius: 5,
                    pointHitRadius: 12,
                    tension: 0,
                }],
            },
            options: {
                animation: false,
                maintainAspectRatio: false,
                interaction: { mode: "index", intersect: false },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        displayColors: false,
                        callbacks: {
                            title: (items) => {
                                const slot = slots[items[0].dataIndex];
                                return `${this.intervalDate(slot)} | ${this.intervalTime(slot)}`;
                            },
                            label: (item) => `${this.price(item.raw)} ${unit}`,
                        },
                    },
                },
                scales: {
                    x: {
                        grid: { display: false },
                        border: { color: colors.grid },
                        ticks: { color: colors.text, maxTicksLimit: 8, maxRotation: 0 },
                    },
                    y: {
                        grid: { color: colors.grid },
                        border: { display: false },
                        ticks: { color: colors.text },
                        title: { display: true, text: unit, color: colors.text },
                    },
                },
            },
        });
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

    signed(text, value) {
        return value > 0 ? `+${text}` : text;
    }

    money(total) {
        return this.signed(formatMonetary(total.amount, { currencyId: total.id }), total.amount);
    }

    toneClass(value) {
        return value > 0 ? "text-success" : value < 0 ? "text-danger" : "";
    }

    toneIcon(value) {
        return value > 0 ? "fa fa-arrow-up" : value < 0 ? "fa fa-arrow-down" : "fa fa-minus";
    }

    openTariffs() {
        return this.action.doAction("mrp_grid_scheduler.action_grid_tariff_source");
    }

    openAppliedProposals() {
        return this.action.doAction("mrp_grid_scheduler.action_grid_schedule_run", {
            additionalContext: { search_default_applied: 1 },
        });
    }

    openEnergyResults() {
        return this.action.doAction("mrp_grid_scheduler.action_grid_energy_results");
    }

    openSource() {
        return this.action.doAction("mrp_grid_scheduler.action_grid_tariff_source", {
            viewType: "form",
            props: { resId: this.state.sourceId },
        });
    }
}

registry.category("actions").add("mrp_grid_scheduler.dashboard", GridDashboard);

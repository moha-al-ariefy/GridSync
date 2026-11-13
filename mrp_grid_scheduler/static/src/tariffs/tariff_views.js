import { onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { formView } from "@web/views/form/form_view";
import { listView } from "@web/views/list/list_view";
import { FormStatusIndicator } from "@web/views/form/form_status_indicator/form_status_indicator";

class GridFormStatus extends FormStatusIndicator {
    static template = "mrp_grid_scheduler.FormStatus";
}

class GridFormController extends formView.Controller {
    static template = "mrp_grid_scheduler.FormView";
    static components = { ...formView.Controller.components, GridFormStatus };

    get listViewType() {
        const views = this.env.config.views || [];
        return views.find(([, type]) => type === "list")?.[1]
            || views.find(([, type]) => type === "kanban")?.[1];
    }

    async goToList() {
        if (this.listViewType) {
            await this.actionService.switchView(this.listViewType);
        } else {
            this.env.config.historyBack();
        }
    }

    async save(params) {
        const isNew = this.model.root.isNew;
        const saved = await super.save(params);
        if (saved && isNew && !this.env.inDialog) {
            await this.goToList();
        }
        return saved;
    }
}

registry.category("views").add("grid_record_form", { ...formView, Controller: GridFormController });

function usePriceRefresh(controller, isForm = false) {
    const orm = useService("orm");
    const notification = useService("notification");
    onMounted(async () => {
        const { model } = controller;
        const record = model.root;
        if (isForm && !record.resId) {
            return;
        }
        let refreshed;
        try {
            refreshed = await orm.silent.call(
                "grid.tariff.source",
                "refresh_energy_charts",
                isForm ? [record.resId] : [],
                { context: controller.props.context },
            );
        } catch {
            notification.add(_t("Automatic price refresh is unavailable. Showing stored data."), {
                type: "warning",
            });
            return;
        }
        if (refreshed && (!isForm || (record === model.root && !(await record.isDirty())))) {
            await model.load();
        }
    });
}

class TariffListController extends listView.Controller {
    setup() {
        super.setup();
        usePriceRefresh(this);
    }
}

class TariffFormController extends GridFormController {
    setup() {
        super.setup();
        usePriceRefresh(this, true);
    }
}

registry.category("views").add("grid_tariff_list", { ...listView, Controller: TariffListController });
registry.category("views").add("grid_tariff_form", { ...formView, Controller: TariffFormController });

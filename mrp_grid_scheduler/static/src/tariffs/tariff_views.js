import { onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { formView } from "@web/views/form/form_view";
import { listView } from "@web/views/list/list_view";

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

class TariffFormController extends formView.Controller {
    setup() {
        super.setup();
        usePriceRefresh(this, true);
    }
}

registry.category("views").add("grid_tariff_list", { ...listView, Controller: TariffListController });
registry.category("views").add("grid_tariff_form", { ...formView, Controller: TariffFormController });

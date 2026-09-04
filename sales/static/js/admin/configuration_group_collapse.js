/* Collapse-all toggle for the Quote page's Configuration Groups inline. */
(function () {
    "use strict";

    // Prefix comes from the Quote.configuration_groups related_name.
    var FORMSET_ID = "configuration_groups-data";

    var BUTTON_CLASSES =
        "font-medium inline-flex group items-center gap-1 relative rounded-default " +
        "justify-center whitespace-nowrap cursor-pointer px-3 py-2 text-sm border " +
        "border-base-200 bg-white shadow-xs text-important " +
        "dark:border-base-700 dark:bg-transparent hover:bg-base-100/80 dark:hover:bg-base-800/80";

    var collapsed = false;

    function groupBodies(formset) {
        // Keep each row's <h3>: it holds the title and drag handle.
        var bodies = [];
        formset.querySelectorAll(".form-row").forEach(function (row) {
            Array.prototype.forEach.call(row.children, function (child) {
                if (child.tagName.toLowerCase() !== "h3") {
                    bodies.push(child);
                }
            });
        });
        return bodies;
    }

    function apply(formset, button) {
        groupBodies(formset).forEach(function (body) {
            body.classList.toggle("hidden", collapsed);
        });
        button.textContent = collapsed ? "Expand groups" : "Collapse groups";
    }

    function init() {
        var formset = document.getElementById(FORMSET_ID);
        if (!formset || !formset.parentElement || !formset.parentElement.parentElement) {
            return;
        }

        var button = document.createElement("button");
        button.type = "button";
        button.className = BUTTON_CLASSES;
        button.textContent = "Collapse groups";
        button.addEventListener("click", function () {
            collapsed = !collapsed;
            apply(formset, button);
        });

        var bar = document.createElement("div");
        bar.className = "flex justify-end mb-2";
        bar.appendChild(button);

        var container = formset.parentElement;
        container.parentElement.insertBefore(bar, container);

        // Keep rows added while collapsed in sync.
        new MutationObserver(function () {
            if (collapsed) {
                apply(formset, button);
            }
        }).observe(formset, { childList: true, subtree: true });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();

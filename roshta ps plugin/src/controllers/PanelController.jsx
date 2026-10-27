import React from "react";
import ReactDOM from "react-dom";

export class PanelController {
    constructor(Component, { id, menuItems } = {}) {
        this._Component = Component;
        this._id = id;
        this._menuItems = menuItems || [];
        this._root = null;
        this._attachment = null;

        this.menuItems = this._menuItems.map(menuItem => ({
            id: menuItem.id,
            label: menuItem.label,
            enabled: menuItem.enabled !== undefined ? menuItem.enabled : true,
            checked: menuItem.checked || false
        }));

        ["create", "show", "hide", "destroy", "invokeMenu"].forEach(fn => this[fn] = this[fn].bind(this));
    }

    create(event) {
        console.log("[Roshta] PanelController create", this._id, event);
        if (!this._root) {
            this._root = document.createElement("div");
            this._root.id = "roshta-panel-root";
            this._root.style.width = "100%";
            this._root.style.height = "100%";
            this._root.style.boxSizing = "border-box";
            this._root.style.overflow = "auto";
            this._root.style.padding = "0px";

            let element;
            try {
                const res = typeof this._Component === "function" ? this._Component({ panel: this }) : this._Component;
                element = React.isValidElement(res) ? res : React.createElement(this._Component, { panel: this });
            } catch (err) {
                console.error("[Roshta] Component creation error:", err);
                element = React.createElement(this._Component, { panel: this });
            }

            try {
                ReactDOM.render(element, this._root);
                console.log("[Roshta] React mounted successfully into panel root");
            } catch (renderErr) {
                console.error("[Roshta] ReactDOM.render error:", renderErr);
                this._root.innerHTML = `<div style="color:red;padding:12px;">React Render Error: ${renderErr.message}</div>`;
            }
        }

        const target = (event && event.node) || (event && typeof event.appendChild === "function" ? event : null);
        if (target && this._root && !target.contains(this._root)) {
            this._attachment = target;
            target.appendChild(this._root);
            console.log("[Roshta] Attached in create");
        }

        return this._root;
    }

    show(event) {
        console.log("[Roshta] PanelController show", this._id, event);
        if (!this._root) {
            this.create(event);
        }

        const target = (event && event.node)
            || (event && typeof event.appendChild === "function" ? event : null)
            || (typeof document !== "undefined" ? document.body : null);

        if (target && this._root && !target.contains(this._root)) {
            this._attachment = target;
            target.appendChild(this._root);
            console.log("[Roshta] Attached in show to", target);
        }
    }

    hide(event) {
        console.log("[Roshta] PanelController hide", this._id);
        if (this._attachment && this._root) {
            try {
                if (this._attachment.contains(this._root)) {
                    this._attachment.removeChild(this._root);
                }
            } catch (e) {
                // Ignore
            }
            this._attachment = null;
        }
    }

    destroy() { }

    invokeMenu(id) {
        const menuItem = this._menuItems.find(c => c.id === id);
        if (menuItem) {
            const handler = menuItem.oninvoke;
            if (handler) {
                handler();
            }
        }
    }
}

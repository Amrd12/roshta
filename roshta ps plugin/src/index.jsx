// Environment polyfills
if (typeof global !== "undefined") {
    global.screen = global.screen || {};
}
if (typeof window !== "undefined" && window.WebSocket && window.WebSocket.prototype) {
    window.WebSocket.prototype.OPEN = 1;
}

import React from "react";
import ReactDOM from "react-dom";

import "./ui/styles.css";
import { PanelController } from "./controllers/PanelController.jsx";
import { App } from "./ui/App.jsx";
import { pauseCommand } from "./commands/pause.js";
import { resumeCommand } from "./commands/resume.js";
import { afterFinishCommand } from "./commands/afterFinish.js";

import { loadConfig } from "./core/config.js";
import { poller } from "./core/poller.js";
import { store, A } from "./core/store.js";
import { entrypoints } from "uxp";

const mainPanelController = new PanelController(() => <App />, {
    id: "roshta-main",
    menuItems: [
        { id: "reload", label: "Reload Plugin", enabled: true, checked: false, oninvoke: () => location.reload() },
        {
            id: "show-setup",
            label: "Settings…",
            enabled: true,
            checked: false,
            oninvoke: () => {
                store.dispatch({ type: A.SET_ACTIVE_TAB, payload: "setup" });
            }
        }
    ]
});

const pauseController = { run: pauseCommand };
const resumeController = { run: resumeCommand };
const afterFinishController = { run: afterFinishCommand };

const c = mainPanelController;

entrypoints.setup({
    plugin: {
        async create(plugin) {
            console.log("[Roshta] Plugin lifecycle: create", plugin);
            try {
                const config = await loadConfig();
                store.dispatch({ type: A.SET_SETTINGS, payload: config });
            } catch (err) {
                console.warn("[Roshta] Error loading config on plugin create:", err);
            }
            poller.start();
        },
        destroy() {
            console.log("[Roshta] Plugin lifecycle: destroy");
            poller.stop();
        }
    },
    panels: {
        "roshta-main": {
            create: (e) => c.create(e),
            show: (e) => c.show(e),
            hide: (e) => c.hide(e),
            destroy: () => c.destroy(),
            invokeMenu: (id) => c.invokeMenu(id),
            menuItems: c.menuItems
        }
    },
    commands: {
        "cmd-pause": pauseController,
        "cmd-resume": resumeController,
        "cmd-after-finish": afterFinishController
    }
});

// Self-initialize immediately on script load
(async function init() {
    try {
        const config = await loadConfig();
        store.dispatch({ type: A.SET_SETTINGS, payload: config });
    } catch (e) {
        console.warn("[Roshta] Immediate config load notice:", e);
    }
    poller.start();
})();

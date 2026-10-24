import React from "react";
import ReactDOM from "react-dom";

export class CommandController {
    constructor(Component, { id, ...dialogOpts } = {}) {
        this._Component = Component;
        this._id = id;
        this._root = null;
        this._dialogOpts = Object.assign({}, {
            title: id,
            resize: "none",
            size: {
                width: 480,
                height: 320
            }
        }, dialogOpts);
        [ "run" ].forEach(fn => this[fn] = this[fn].bind(this));
    }

    async run() {
        if (!this._root) {
            this._root = document.createElement("dialog");
            ReactDOM.render(this._Component({ dialog: this._root }), this._root);
        }
        document.body.appendChild(this._root);

        await this._root.showModal(this._dialogOpts);
        this._root.remove();
    }
}

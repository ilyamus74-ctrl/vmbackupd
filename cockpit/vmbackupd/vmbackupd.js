(function () {
    "use strict";

    const api = window.VmbackupApi;
    const notice = document.getElementById("notice");
    const refreshButton = document.getElementById("refresh");

    function text(value) {
        if (value === null || value === undefined || value === "")
            return "—";
        return String(value);
    }

    function bytes(value) {
        if (value === null || value === undefined)
            return "Unknown";
        const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
        let amount = Number(value);
        let unit = 0;
        while (amount >= 1024 && unit < units.length - 1) {
            amount /= 1024;
            unit += 1;
        }
        return `${amount.toLocaleString(undefined, { maximumFractionDigits: 1 })} ${units[unit]} (${value} bytes)`;
    }

    function element(name, value, className) {
        const node = document.createElement(name);
        node.textContent = text(value);
        if (className)
            node.className = className;
        return node;
    }

    function setNotice(message, kind) {
        notice.textContent = message;
        notice.className = `notice ${kind || ""}`.trim();
    }

    function clearViews() {
        document.getElementById("dashboard").replaceChildren();
        document.getElementById("vms").replaceChildren();
        document.getElementById("storage").replaceChildren();
    }

    function renderDashboard(status) {
        const dashboard = document.getElementById("dashboard");
        dashboard.replaceChildren();
        const mutation = status.libvirt_mutation_enabled ? "Mutation enabled" : "Mutation disabled";
        const values = [
            ["Runtime", status.runtime_state],
            ["Version", status.version],
            ["Node", status.node_name],
            ["Node ID", status.node_id],
            ["Daemon instance", status.daemon_instance_id],
            ["Controller owned", status.controller_owned ? "Yes" : "No"],
            ["Database schema", status.database_schema_version],
            ["Libvirt URI", status.libvirt_uri],
            ["Mutation", mutation],
            ["Free backup data", bytes(status.free_backup_data_bytes)],
            ["Non-terminal runs", status.nonterminal_run_count],
            ["Recovery required", status.recovery_required_count],
        ];
        for (const [label, value] of values) {
            dashboard.append(element("dt", label));
            const kind = label === "Runtime" ? (value === "RUNNING" ? "good" : "bad") : "";
            dashboard.append(element("dd", value, kind));
        }
    }

    function renderRows(targetId, rows) {
        const target = document.getElementById(targetId);
        target.replaceChildren(...rows);
    }

    function tableRow(values) {
        const row = document.createElement("tr");
        for (const value of values)
            row.append(element("td", value));
        return row;
    }

    function renderVms(vms) {
        renderRows("vms", vms.map(vm => tableRow([
            vm.name, vm.external_id, vm.uuid, vm.state,
        ])));
    }

    function renderStorage(destinations) {
        renderRows("storage", destinations.map(destination => tableRow([
            destination.name,
            "Local",
            destination.is_default ? "Yes" : "No",
            destination.backup_data_root,
            destination.control_root,
            bytes(destination.free_bytes),
            bytes(destination.minimum_free_bytes),
            `${text(destination.minimum_free_percent)}%`,
        ])));
    }

    function failureMessage(error) {
        if (error instanceof api.ProtocolError)
            return `Malformed API response: ${error.message}`;
        if (error instanceof api.ApiError)
            return `API error ${error.code}: ${error.message}`;
        return "Daemon unavailable or permission denied. The logged-in session must belong to vmbackupd-admin; after enrollment, start a fresh Cockpit login session.";
    }

    async function refresh() {
        refreshButton.disabled = true;
        clearViews();
        setNotice("Loading…", "loading");
        try {
            const [status, vms, storage] = await Promise.all([
                api.request("daemon.status"),
                api.request("vm.discover"),
                api.request("storage.list"),
            ]);
            renderDashboard(status);
            renderVms(vms);
            renderStorage(storage);
            if (status.runtime_state === "RUNNING")
                setNotice("Loaded", "success");
            else
                setNotice(`Daemon runtime is ${text(status.runtime_state)}`, "error");
        } catch (error) {
            setNotice(failureMessage(error), "error");
        } finally {
            refreshButton.disabled = false;
        }
    }

    refreshButton.addEventListener("click", refresh);
    refresh();
}());

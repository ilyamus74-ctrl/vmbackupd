(function () {
    "use strict";

    let recentRunFilter = "ALL";
    let recentRunPage = 0;
    const RECENT_RUN_LIMIT = 5;

    window.VmbackupViews = {
        renderModel(model) {
                        renderSummary(model);

            // TEMP: disable broken recent runs renderer
            // renderRecentRuns(model);

            renderSystemDetails(model);

            const node =
                document.getElementById("daemon-health");

            if (node && model.status) {
                node.textContent =
                    model.status.runtime_state || "unknown";
            }
        },
    };



})();


    function tableCell(value, className) {
        const cell = document.createElement("td");
        if (value instanceof Node)
            cell.append(value);
        else
            cell.textContent = text(value);
        if (className)
            cell.className = className;
        return cell;
    }


    function tableRow(values) {
        const row = document.createElement("tr");
        for (const value of values) {
            if (Array.isArray(value))
                row.append(tableCell(value[0], value[1]));
            else
                row.append(tableCell(value));
        }
        return row;
    }


    function emptyRow(columns, message) {
        const row = document.createElement("tr");
        const cell = tableCell(message, "empty-state");
        cell.colSpan = columns;
        row.append(cell);
        return row;
    }


    function replaceRows(targetId, rows, columns, emptyMessage) {
        document.getElementById(targetId).replaceChildren(
            ...(rows.length ? rows : [emptyRow(columns, emptyMessage)])
        );
    }


    function renderSystemDetails(model) {
        const status = model.status || model;
        
        const target = document.getElementById("system-details");

        if (!target || !status)
            return;

        target.replaceChildren();

        const rows = [
            ["Runtime", status.runtime_state],
            ["Instance", status.daemon_instance_id],
            ["Libvirt URI", status.libvirt_uri],
            ["Database", status.database_path],
            ["Backup root", status.backup_data_root],
            [
                "Free space",
                status.free_backup_data_bytes
                    ? String(status.free_backup_data_bytes)
                    : "—",
            ],
        ];

        for (const [name, value] of rows) {
            const dt = document.createElement("dt");
            dt.textContent = name;

            const dd = document.createElement("dd");
            dd.textContent = value || "—";

            target.append(dt, dd);
        }
    }



    function renderSummary(model) {
        document.getElementById("successful-today").textContent = String(model.successfulToday);
        document.getElementById("failed-today").textContent = String(model.failedToday);
        document.getElementById("active-runs").textContent = String(model.active);
        document.getElementById("recovery-required").textContent = String(model.recoveryRequired);
        const healthy = model.status.runtime_state === "RUNNING";
        const daemonHealth = document.getElementById("daemon-health");
        daemonHealth.textContent = healthy ? "RUNNING" : text(model.status.runtime_state);
        daemonHealth.className = `badge ${healthy ? "status-success" : "status-failed"}`;
        const mutation = document.getElementById("mutation-state");
        mutation.textContent = model.status.libvirt_mutation_enabled ? "Mutation enabled" : "Mutation disabled";
        mutation.className = `badge ${model.status.libvirt_mutation_enabled ? "status-warning" : "status-neutral"}`;
    }


    function statusLabel(run) {
        return run.state || run.status || "UNKNOWN";
    }


    function statusClass(run) {
        const state = statusLabel(run).toLowerCase();

        if (state === "success" || state === "completed")
            return "status-success";

        if (
            state === "failed" ||
            state === "error"
        )
            return "status-failed";

        return "status-neutral";
    }


    function runDuration(run, now) {
        if (!run.started_at || !run.finished_at)
            return "—";

        const seconds =
            Math.max(
                0,
                Math.floor(
                    (new Date(run.finished_at) -
                     new Date(run.started_at)) / 1000
                )
            );

        return `${seconds}s`;
    }


    function runError(run) {
        return run.error ||
               run.failure_reason ||
               "—";
    }


    function localTimestamp(value) {
        if (!value)
            return "—";

        return new Date(value).toLocaleString();
    }


    function vmName(map, id) {
        const vm = map.get(id);
        return vm ? vm.name : id;
    }


    function renderRecentRuns(model) {
        const rows = model.runs.map(run => {
            const job = model.jobById.get(run.job_id);

            return tableRow([
                job ?
                    vmName(model.vmById, job.vm_id) :
                    `Unknown VM (job ${text(run.job_id)})`,
                job ?
                    job.name :
                    `Unknown job (${text(run.job_id)})`,
                [run.planned_kind || "—", "nowrap"],
                [localTimestamp(run.created_at), "nowrap"],
                badge(statusLabel(run), statusClass(run)),
                [runDuration(run, model.now), "nowrap"],
                [runError(run), "error-cell"],
            ]);
        });

        replaceRows(
            "recent-runs",
            rows,
            7,
            recentRunFilter === "ALL" ?
                "No backup runs yet" :
                `No ${recentRunFilter.toLowerCase()} backup runs`,
        );

        const page = model.runPage || {};
        const total = Number(page.total || 0);
        const offset = Number(page.offset || 0);
        const limit = Number(page.limit || RECENT_RUN_LIMIT);

        const first = total > 0 ? offset + 1 : 0;
        const last = Math.min(offset + limit, total);

        document.getElementById(
            "recent-run-page-info"
        ).textContent = `${first}–${last} of ${total}`;

        document.getElementById(
            "recent-run-prev"
        ).disabled = offset <= 0;

        document.getElementById(
            "recent-run-next"
        ).disabled = offset + limit >= total;

        document.getElementById(
            "recent-run-filter"
        ).value = recentRunFilter;
    }

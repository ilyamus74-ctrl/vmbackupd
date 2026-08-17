(function () {
    "use strict";

    const api = window.VmbackupApi;
    const notice = document.getElementById("notice");
    const refreshButton = document.getElementById("refresh");
    const TERMINAL_STATES = new Set(["SUCCESS", "FAILED"]);
    const RECENT_RUN_LIMIT = 20;

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
        return `${amount.toLocaleString(undefined, { maximumFractionDigits: 1 })} ${units[unit]}`;
    }

    function localTimestamp(value) {
        if (!value)
            return "—";
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime()) ? "Invalid timestamp" : parsed.toLocaleString();
    }

    function durationBetween(startValue, endValue) {
        const start = new Date(startValue).getTime();
        const end = new Date(endValue).getTime();
        if (!Number.isFinite(start) || !Number.isFinite(end) || end < start)
            return "—";
        let seconds = Math.floor((end - start) / 1000);
        const days = Math.floor(seconds / 86400);
        seconds %= 86400;
        const hours = Math.floor(seconds / 3600);
        seconds %= 3600;
        const minutes = Math.floor(seconds / 60);
        seconds %= 60;
        if (days)
            return `${days}d ${hours}h`;
        if (hours)
            return `${hours}h ${minutes}m`;
        if (minutes)
            return `${minutes}m ${seconds}s`;
        return `${seconds}s`;
    }

    function runDuration(run, now) {
        const end = TERMINAL_STATES.has(run.state) ? run.updated_at : now.toISOString();
        return durationBetween(run.created_at, end);
    }

    function isToday(value, now) {
        const date = new Date(value);
        return !Number.isNaN(date.getTime()) &&
            date.getFullYear() === now.getFullYear() &&
            date.getMonth() === now.getMonth() &&
            date.getDate() === now.getDate();
    }

    function indexById(values) {
        return new Map(values.map(value => [value.id, value]));
    }

    function groupRunsByJob(runs) {
        const grouped = new Map();
        for (const run of runs) {
            if (!grouped.has(run.job_id))
                grouped.set(run.job_id, []);
            grouped.get(run.job_id).push(run);
        }
        for (const values of grouped.values())
            values.sort((left, right) => new Date(right.created_at) - new Date(left.created_at));
        return grouped;
    }

    function latestRun(runsByJob, jobId) {
        const runs = runsByJob.get(jobId) || [];
        return runs.length ? runs[0] : null;
    }

    function latestSuccessfulRestorePoint(jobId, restorePoints, runsById) {
        return restorePoints
            .filter(point => point.status === "AVAILABLE")
            .filter(point => {
                const run = runsById.get(point.job_run_id);
                return run && run.job_id === jobId;
            })
            .sort((left, right) => new Date(right.created_at) - new Date(left.created_at))[0] || null;
    }

    function runError(run) {
        return run.recovery_reason || run.cleanup_error || run.error || "—";
    }

    function statusClass(run) {
        if (run.recovery_required)
            return "status-recovery";
        if (run.state === "SUCCESS")
            return "status-success";
        if (run.state === "FAILED")
            return "status-failed";
        if (run.state === "CLEANUP")
            return "status-cleanup";
        return "status-active";
    }

    function statusLabel(run) {
        return run.recovery_required ? `RECOVERY: ${text(run.state)}` : text(run.state);
    }

    function element(name, value, className) {
        const node = document.createElement(name);
        node.textContent = text(value);
        if (className)
            node.className = className;
        return node;
    }

    function badge(value, className) {
        return element("span", value, `badge ${className}`);
    }

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

    function vmName(vmById, vmId) {
        const vm = vmById.get(vmId);
        return vm ? vm.name : `Unknown VM (${text(vmId)})`;
    }

    function deriveModel(dataset, now) {
        const vmById = indexById(dataset.registeredVms);
        const jobById = indexById(dataset.jobs);
        const storageById = indexById(dataset.storage);
        const runsById = indexById(dataset.runs);
        const runsByJob = groupRunsByJob(dataset.runs);
        return {
            ...dataset,
            now: now,
            vmById: vmById,
            jobById: jobById,
            storageById: storageById,
            runsById: runsById,
            runsByJob: runsByJob,
            successfulToday: dataset.runs.filter(run =>
                run.state === "SUCCESS" && isToday(run.updated_at, now)).length,
            failedToday: dataset.runs.filter(run =>
                run.state === "FAILED" && isToday(run.updated_at, now)).length,
            active: dataset.runs.filter(run => !TERMINAL_STATES.has(run.state)).length,
            recoveryRequired: dataset.runs.filter(run => run.recovery_required).length,
        };
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

    function renderRecentRuns(model) {
        const sorted = [...model.runs]
            .sort((left, right) => new Date(right.created_at) - new Date(left.created_at))
            .slice(0, RECENT_RUN_LIMIT);
        const rows = sorted.map(run => {
            const job = model.jobById.get(run.job_id);
            return tableRow([
                job ? vmName(model.vmById, job.vm_id) : `Unknown VM (job ${text(run.job_id)})`,
                job ? job.name : `Unknown job (${text(run.job_id)})`,
                [run.planned_kind || "—", "nowrap"],
                [localTimestamp(run.created_at), "nowrap"],
                badge(statusLabel(run), statusClass(run)),
                [runDuration(run, model.now), "nowrap"],
                [runError(run), "error-cell"],
            ]);
        });
        replaceRows("recent-runs", rows, 7, "No backup runs yet");
    }

    function renderJobs(model) {
        const jobs = [...model.jobs].sort((left, right) =>
            vmName(model.vmById, left.vm_id).localeCompare(vmName(model.vmById, right.vm_id)) ||
            left.name.localeCompare(right.name));
        const rows = jobs.map(job => {
            const lastRun = latestRun(model.runsByJob, job.id);
            const successfulPoint = latestSuccessfulRestorePoint(
                job.id, model.restorePoints, model.runsById
            );
            const destination = model.storageById.get(job.storage_destination_id);
            return tableRow([
                vmName(model.vmById, job.vm_id),
                job.name,
                badge(job.enabled ? "Enabled" : "Disabled", job.enabled ? "status-success" : "status-neutral"),
                destination ? destination.name : `Unknown destination (${text(job.storage_destination_id)})`,
                lastRun ? localTimestamp(lastRun.created_at) : "Never",
                lastRun ? badge(statusLabel(lastRun), statusClass(lastRun)) : "—",
                successfulPoint ? localTimestamp(successfulPoint.created_at) : "Never",
                job.next_run_at ? localTimestamp(job.next_run_at) : "Manual / not scheduled",
            ]);
        });
        replaceRows("jobs", rows, 8, "No backup jobs configured");
    }

    function renderStorage(model) {
        const rows = model.storage.map(destination => {
            const usable = destination.free_bytes === null || destination.free_bytes === undefined ?
                null : Math.max(0, destination.free_bytes - destination.minimum_free_bytes);
            return tableRow([
                destination.name,
                ["Local", "nowrap"],
                [destination.is_default ? "Yes" : "No", "nowrap"],
                [bytes(destination.free_bytes), "nowrap"],
                [`${bytes(destination.minimum_free_bytes)} / ${text(destination.minimum_free_percent)}%`, "nowrap"],
                [bytes(usable), "nowrap"],
                [destination.backup_data_root, "path-cell"],
            ]);
        });
        replaceRows("storage", rows, 7, "No storage destinations configured");
    }

    function renderDiscoveredVms(model) {
        const rows = model.discoveredVms.map(vm => tableRow([
            vm.name, vm.external_id, [vm.uuid, "identifier-cell"], badge(vm.state, "status-neutral"),
        ]));
        replaceRows("vms", rows, 4, "No libvirt virtual machines discovered");
    }

    function renderSystemDetails(status) {
        const details = document.getElementById("system-details");
        const values = [
            ["Version", status.version],
            ["Node", status.node_name],
            ["Node ID", status.node_id],
            ["Daemon instance", status.daemon_instance_id],
            ["Controller owned", status.controller_owned ? "Yes" : "No"],
            ["Database schema", status.database_schema_version],
            ["Libvirt URI", status.libvirt_uri],
            ["Mutation", status.libvirt_mutation_enabled ? "Mutation enabled" : "Mutation disabled"],
        ];
        const nodes = [];
        for (const [label, value] of values) {
            nodes.push(element("dt", label));
            nodes.push(element("dd", value));
        }
        details.replaceChildren(...nodes);
    }

    function renderModel(model) {
        renderSummary(model);
        renderRecentRuns(model);
        renderJobs(model);
        renderStorage(model);
        renderDiscoveredVms(model);
        renderSystemDetails(model.status);
    }

    function clearViews() {
        for (const id of ["successful-today", "failed-today", "active-runs", "recovery-required"])
            document.getElementById(id).textContent = "—";
        document.getElementById("daemon-health").textContent = "Unavailable";
        document.getElementById("mutation-state").textContent = "Mutation state unknown";
        for (const id of ["recent-runs", "jobs", "storage", "vms", "system-details"])
            document.getElementById(id).replaceChildren();
    }

    function setNotice(message, kind) {
        notice.textContent = message;
        notice.className = `notice ${kind || ""}`.trim();
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
        setNotice("Loading complete backup status…", "loading");
        try {
            const [status, discoveredVms, registeredVms, storage, jobs, runs,
                restorePoints, recovery] = await Promise.all([
                api.request("daemon.status"),
                api.request("vm.discover"),
                api.request("vm.list"),
                api.request("storage.list"),
                api.request("job.list"),
                api.request("run.list"),
                api.request("restore_point.list"),
                api.request("recovery.list"),
            ]);
            const model = deriveModel({
                status: status,
                discoveredVms: discoveredVms,
                registeredVms: registeredVms,
                storage: storage,
                jobs: jobs,
                runs: runs,
                restorePoints: restorePoints,
                recovery: recovery,
            }, new Date());
            renderModel(model);
            if (status.runtime_state === "RUNNING")
                setNotice("Operational data loaded", "success");
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

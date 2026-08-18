(function () {
    "use strict";

    const api = window.VmbackupApi;
    const notice = document.getElementById("notice");
    const refreshButton = document.getElementById("refresh");
    const addJobButton = document.getElementById("add-job");
    const addStorageButton = document.getElementById("add-storage");
    const jobDialog = document.getElementById("job-dialog");
    const jobForm = document.getElementById("job-form");
    const storageDialog = document.getElementById("storage-dialog");
    const storageForm = document.getElementById("storage-form");
    const TERMINAL_STATES = new Set(["SUCCESS", "FAILED"]);
    const RECENT_RUN_LIMIT = 20;
    const LIVE_REFRESH_INTERVAL_MS = 2000;
    let currentModel = null;
    let editingJobId = null;
    let editingStorageId = null;
    let refreshTimer = null;
    let refreshInFlight = null;
    let pageUnloading = false;

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

    function intervalParts(secondsValue) {
        const seconds = Number(secondsValue);
        if (Number.isInteger(seconds) && seconds > 0) {
            if (seconds % 86400 === 0)
                return { amount: seconds / 86400, unit: 86400 };
            if (seconds % 3600 === 0)
                return { amount: seconds / 3600, unit: 3600 };
            if (seconds % 60 === 0)
                return { amount: seconds / 60, unit: 60 };
            return { amount: seconds, unit: 1 };
        }
        return { amount: 60, unit: 60 };
    }

    function browserTimezone() {
        try {
            const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
            return timezone || "UTC";
        } catch (_error) {
            return "UTC";
        }
    }

    function jobScheduleMode(job) {
        if (!job || !job.next_run_at)
            return "manual";
        return job.schedule_type === "DAILY" ? "daily" : "interval";
    }

    function updateScheduleFields() {
        const mode = document.getElementById("job-schedule").value;
        const intervalFields = document.getElementById("interval-fields");
        const dailyFields = document.getElementById("daily-fields");
        const intervalInput = document.getElementById("job-interval");
        const intervalUnit = document.getElementById("job-interval-unit");
        const dailyTime = document.getElementById("job-daily-time");
        const timezone = document.getElementById("job-schedule-timezone");

        const intervalEnabled = mode === "interval";
        const dailyEnabled = mode === "daily";

        intervalFields.hidden = !intervalEnabled;
        dailyFields.hidden = !dailyEnabled;

        intervalInput.disabled = !intervalEnabled;
        intervalUnit.disabled = !intervalEnabled;
        intervalInput.required = intervalEnabled;

        dailyTime.disabled = !dailyEnabled;
        timezone.disabled = !dailyEnabled;
        dailyTime.required = dailyEnabled;
        timezone.required = dailyEnabled;
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

    function actionButton(label, action, disabled, reason) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.disabled = disabled;
        if (reason)
            button.title = reason;
        button.addEventListener("click", action);
        return button;
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
            const activeForVm = model.runs.some(run => {
                const candidate = model.jobById.get(run.job_id);
                return candidate && candidate.vm_id === job.vm_id &&
                    !TERMINAL_STATES.has(run.state);
            });
            const recoveryForVm = model.runs.some(run => {
                const candidate = model.jobById.get(run.job_id);
                return candidate && candidate.vm_id === job.vm_id && run.recovery_required;
            });
            let runDisabledReason = null;
            if (!model.status.libvirt_mutation_enabled)
                runDisabledReason = "Libvirt mutation is disabled";
            else if (!job.enabled)
                runDisabledReason = "The backup job is disabled";
            else if (recoveryForVm)
                runDisabledReason = "The VM requires recovery";
            else if (activeForVm)
                runDisabledReason = "The VM already has active work";
            const actions = document.createElement("div");
            actions.className = "row-actions";
            actions.append(
                actionButton("Edit", () => openJobDialog(job), false),
                actionButton(job.enabled ? "Disable" : "Enable",
                    () => updateJob(job.id, { enabled: !job.enabled }), false),
                actionButton("Run now", () => runNow(job), Boolean(runDisabledReason),
                    runDisabledReason),
            );
            return tableRow([
                vmName(model.vmById, job.vm_id),
                job.name,
                badge(job.enabled ? "Enabled" : "Disabled", job.enabled ? "status-success" : "status-neutral"),
                destination ? destination.name : `Unknown destination (${text(job.storage_destination_id)})`,
                lastRun ? localTimestamp(lastRun.created_at) : "Never",
                lastRun ? badge(statusLabel(lastRun), statusClass(lastRun)) : "—",
                successfulPoint ? localTimestamp(successfulPoint.created_at) : "Never",
                job.next_run_at ? localTimestamp(job.next_run_at) : "Manual / not scheduled",
                actions,
            ]);
        });
        replaceRows("jobs", rows, 9, "No backup jobs configured");
    }

    function renderStorage(model) {
        const rows = model.storage.map(destination => {
            const usable = destination.free_bytes === null || destination.free_bytes === undefined ?
                null : Math.max(0, destination.free_bytes - destination.minimum_free_bytes);
            const actions = document.createElement("div");
            actions.className = "row-actions";
            actions.append(
                actionButton("Edit", () => openStorageDialog(destination), false),
                actionButton("Test", () => testStoredDestination(destination), false),
            );
            if (!destination.is_default)
                actions.append(actionButton("Set default", () => setDefaultStorage(destination), false));
            return tableRow([
                destination.name,
                ["Local", "nowrap"],
                [destination.is_default ? "Yes" : "No", "nowrap"],
                [bytes(destination.free_bytes), "nowrap"],
                [`${bytes(destination.minimum_free_bytes)} / ${text(destination.minimum_free_percent)}%`, "nowrap"],
                [bytes(usable), "nowrap"],
                [destination.backup_data_root, "path-cell"],
                actions,
            ]);
        });
        replaceRows("storage", rows, 8, "No storage destinations configured");
    }

    function exactByteParts(value) {
        const bytesValue = Number(value);
        for (const unit of [1073741824, 1048576, 1]) {
            if (Number.isSafeInteger(bytesValue) && bytesValue % unit === 0)
                return { value: bytesValue / unit, unit };
        }
        return { value: bytesValue, unit: 1 };
    }

    function minimumFreeBytes() {
        const value = Number(document.getElementById("storage-minimum-value").value);
        const unit = Number(document.getElementById("storage-minimum-unit").value);
        const result = value * unit;
        if (!Number.isSafeInteger(value) || value < 0 || !Number.isSafeInteger(result))
            throw new Error("Minimum free space must be an exact non-negative integer");
        return result;
    }

    function showProbeResult(node, result, failedMessage = null) {
        node.hidden = false;
        node.className = `probe-result ${result && result.ok ? "success" : "error"}`;
        node.textContent = failedMessage ||
            `${result.message}; free ${bytes(result.free_bytes)}. ` +
            "Daemon-side Backup-location filesystem test only; no VM backup was run.";
    }

    function openStorageDialog(destination = null) {
        editingStorageId = destination ? destination.id : null;
        document.getElementById("storage-dialog-title").textContent =
            destination ? "Edit destination" : "Add destination";
        document.getElementById("storage-name").value = destination ? destination.name : "";
        document.getElementById("storage-data-root").value = destination ? destination.backup_data_root : "";
        const reserve = exactByteParts(destination ? destination.minimum_free_bytes : 0);
        document.getElementById("storage-minimum-value").value = reserve.value;
        document.getElementById("storage-minimum-unit").value = String(reserve.unit);
        document.getElementById("storage-minimum-percent").value =
            destination ? destination.minimum_free_percent : 5;
        const defaultCheckbox = document.getElementById("storage-default");
        defaultCheckbox.checked = destination ? destination.is_default : false;
        defaultCheckbox.disabled = Boolean(destination && destination.is_default);
        document.getElementById("storage-default-note").hidden =
            !Boolean(destination && destination.is_default);
        const locked = Boolean(destination && destination.identity_locked);
        document.getElementById("storage-data-root").disabled = locked;
        document.getElementById("storage-identity-note").hidden = !locked;
        document.getElementById("storage-form-error").textContent = "";
        document.getElementById("storage-dialog-test-result").hidden = true;
        storageDialog.showModal();
    }

    function storageFormParams() {
        return {
            name: document.getElementById("storage-name").value.trim(),
            backup_data_root: document.getElementById("storage-data-root").value,
            minimum_free_bytes: minimumFreeBytes(),
            minimum_free_percent: Number(document.getElementById("storage-minimum-percent").value),
            make_default: document.getElementById("storage-default").checked,
        };
    }

    async function saveStorage(event) {
        event.preventDefault();
        const errorNode = document.getElementById("storage-form-error");
        errorNode.textContent = "";
        try {
            const params = storageFormParams();
            if (editingStorageId)
                await api.request("storage.update", { id: editingStorageId, ...params });
            else
                await api.request("storage.create", params);
            storageDialog.close();
            await refresh();
            setNotice("Storage destination saved", "success");
        } catch (error) {
            errorNode.textContent = failureMessage(error);
        }
    }

    async function testStoredDestination(destination) {
        const resultNode = document.getElementById("storage-test-result");
        try {
            resultNode.hidden = false;
            resultNode.className = "probe-result";
            resultNode.textContent = `Testing Local destination ${destination.name}…`;
            const result = await api.request("storage.test", { id: destination.id });
            showProbeResult(resultNode, result);
        } catch (error) {
            showProbeResult(resultNode, null, failureMessage(error));
        }
    }

    async function testStorageCandidate() {
        const resultNode = document.getElementById("storage-dialog-test-result");
        resultNode.hidden = true;
        try {
            const params = storageFormParams();
            delete params.name;
            delete params.make_default;
            const result = await api.request("storage.test", params);
            showProbeResult(resultNode, result);
        } catch (error) {
            showProbeResult(resultNode, null, failureMessage(error));
        }
    }

    async function setDefaultStorage(destination) {
        try {
            setNotice(`Setting ${destination.name} as default…`, "loading");
            await api.request("storage.set_default", { id: destination.id });
            await refresh();
            setNotice(`${destination.name} is now the default destination`, "success");
        } catch (error) {
            setNotice(failureMessage(error), "error");
        }
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
        currentModel = model;
        renderSummary(model);
        renderRecentRuns(model);
        renderJobs(model);
        renderStorage(model);
        renderDiscoveredVms(model);
        renderSystemDetails(model.status);
    }

    function populateJobOptions(editingJob) {
        const vmSelect = document.getElementById("job-vm");
        const storageSelect = document.getElementById("job-storage");
        const registeredByUuid = new Map(currentModel.registeredVms
            .filter(vm => vm.libvirt_domain_uuid)
            .map(vm => [vm.libvirt_domain_uuid, vm]));
        const options = [];
        for (const vm of currentModel.registeredVms)
            options.push({ value: `registered:${vm.id}`, label: vm.name, registered: true });
        currentModel.discoveredVms.forEach((vm, index) => {
            if (!registeredByUuid.has(vm.uuid))
                options.push({ value: `discovered:${index}`, label: `${vm.name} (will register)`, registered: false });
        });
        vmSelect.replaceChildren(...options.map(item => {
            const option = element("option", item.label);
            option.value = item.value;
            return option;
        }));
        storageSelect.replaceChildren(...currentModel.storage.map(destination => {
            const option = element("option", destination.name);
            option.value = destination.id;
            return option;
        }));
        if (editingJob) {
            vmSelect.value = `registered:${editingJob.vm_id}`;
            storageSelect.value = editingJob.storage_destination_id;
        }
        vmSelect.disabled = Boolean(editingJob);
        updateRegistrationNote();
    }

    function updateRegistrationNote() {
        document.getElementById("registration-note").hidden =
            !document.getElementById("job-vm").value.startsWith("discovered:");
    }

    function openJobDialog(job = null) {
        editingJobId = job ? job.id : null;
        document.getElementById("job-dialog-title").textContent = job ? "Edit backup job" : "Add backup job";
        populateJobOptions(job);
        document.getElementById("job-name").value = job ? job.name : "";
        document.getElementById("job-enabled").checked = job ? job.enabled : true;
        document.getElementById("job-retain").value = job ? job.restore_points_to_retain : 7;
        document.getElementById("job-full-chains").value =
            job ? job.full_chains_to_retain : 2;
        document.getElementById("job-minimum-chains").value =
            job ? job.minimum_full_chains : 1;
        document.getElementById("job-reclaim-mode").value =
            job ? job.space_reclaim_mode : "SAFE";
        document.getElementById("job-schedule").value = jobScheduleMode(job);

        const interval = intervalParts(job ? job.interval_seconds : 3600);
        document.getElementById("job-interval").value = interval.amount;
        document.getElementById("job-interval-unit").value = String(interval.unit);

        document.getElementById("job-daily-time").value =
            job && job.daily_time ? job.daily_time : "01:00";
        document.getElementById("job-schedule-timezone").value =
            job && job.schedule_timezone ?
                job.schedule_timezone : browserTimezone();

        const nextRun = document.getElementById("job-next-run");
        nextRun.hidden = !(job && job.next_run_at);
        nextRun.textContent = job && job.next_run_at ?
            `Current next run: ${localTimestamp(job.next_run_at)}. Recalculated after save.` : "";

        updateScheduleFields();
        document.getElementById("job-form-error").textContent = "";
        jobDialog.showModal();
    }

    async function updateJob(id, params) {
        try {
            setNotice("Updating backup job…", "loading");
            await api.request("job.update", { id: id, ...params });
            await refresh();
        } catch (error) {
            setNotice(failureMessage(error), "error");
        }
    }

    async function runNow(job) {
        try {
            setNotice(`Requesting backup for ${job.name}…`, "loading");
            const result = await api.request("backup.run", { job_id: job.id });
            await refresh();
            setNotice(`Backup run ${text(result.run_id)} created in ${text(result.state)}`, "success");
        } catch (error) {
            setNotice(failureMessage(error), "error");
        }
    }

    async function saveJob(event) {
        event.preventDefault();
        const errorNode = document.getElementById("job-form-error");
        errorNode.textContent = "";
        const scheduleMode = document.getElementById("job-schedule").value;
        const params = {
            name: document.getElementById("job-name").value.trim(),
            storage_destination_id: document.getElementById("job-storage").value,
            enabled: document.getElementById("job-enabled").checked,
            schedule_enabled: scheduleMode !== "manual",
            restore_points_to_retain: Number(document.getElementById("job-retain").value),
            full_chains_to_retain:
                Number(document.getElementById("job-full-chains").value),
            minimum_full_chains:
                Number(document.getElementById("job-minimum-chains").value),
            space_reclaim_mode:
                document.getElementById("job-reclaim-mode").value,
        };

        if (scheduleMode === "interval") {
            params.schedule_type = "INTERVAL";
            params.interval_seconds =
                Number(document.getElementById("job-interval").value) *
                Number(document.getElementById("job-interval-unit").value);
        } else if (scheduleMode === "daily") {
            params.schedule_type = "DAILY";
            params.daily_time =
                document.getElementById("job-daily-time").value;
            params.schedule_timezone =
                document.getElementById("job-schedule-timezone").value.trim();
        }
        let registeredVm = null;
        try {
            if (editingJobId) {
                await api.request("job.update", { id: editingJobId, ...params });
            } else {
                const selection = document.getElementById("job-vm").value;
                let vmId;
                if (selection.startsWith("registered:")) {
                    vmId = selection.slice("registered:".length);
                } else {
                    const discovered = currentModel.discoveredVms[Number(selection.split(":")[1])];
                    registeredVm = await api.request("vm.register", {
                        external_id: discovered.external_id, name: discovered.name,
                    });
                    vmId = registeredVm.id;
                }
                await api.request("job.create", {
                    vm_id: vmId, max_incrementals_per_chain: 0, ...params,
                });
            }
            jobDialog.close();
            await refresh();
            setNotice("Backup job saved", "success");
        } catch (error) {
            errorNode.textContent = registeredVm ?
                `VM registration succeeded, but job creation failed: ${failureMessage(error)}` :
                failureMessage(error);
        }
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

    function hasActiveRuns() {
        return Boolean(
            currentModel &&
            currentModel.runs.some(run => !TERMINAL_STATES.has(run.state))
        );
    }

    function stopLiveRefresh() {
        if (refreshTimer === null)
            return;
        window.clearTimeout(refreshTimer);
        refreshTimer = null;
    }

    function scheduleLiveRefresh() {
        stopLiveRefresh();
        if (pageUnloading || !hasActiveRuns())
            return;

        refreshTimer = window.setTimeout(() => {
            refreshTimer = null;
            void refresh({ background: true });
        }, LIVE_REFRESH_INTERVAL_MS);
    }

    async function refresh(options) {
        const background = Boolean(options && options.background);

        if (refreshInFlight) {
            if (background)
                return refreshInFlight;
            await refreshInFlight;
        }

        stopLiveRefresh();
        refreshButton.disabled = true;

        if (!background) {
            clearViews();
            setNotice("Loading complete backup status…", "loading");
        }

        const operation = (async () => {
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

                return true;
            } catch (error) {
                setNotice(failureMessage(error), "error");
                return false;
            } finally {
                refreshButton.disabled = false;
            }
        })();

        refreshInFlight = operation;
        const succeeded = await operation;

        if (refreshInFlight === operation)
            refreshInFlight = null;

        if (succeeded)
            scheduleLiveRefresh();

        return succeeded;
    }

    refreshButton.addEventListener("click", refresh);
    addJobButton.addEventListener("click", () => openJobDialog());
    addStorageButton.addEventListener("click", () => openStorageDialog());
    document.getElementById("job-cancel").addEventListener("click", () => jobDialog.close());
    document.getElementById("job-vm").addEventListener("change", updateRegistrationNote);
    document.getElementById("job-schedule").addEventListener("change", updateScheduleFields);
    jobForm.addEventListener("submit", saveJob);
    document.getElementById("storage-cancel").addEventListener("click", () => storageDialog.close());
    document.getElementById("storage-test-candidate").addEventListener("click", testStorageCandidate);
    storageForm.addEventListener("submit", saveStorage);
    window.addEventListener("beforeunload", () => {
        pageUnloading = true;
        stopLiveRefresh();
    });
    refresh();
}());

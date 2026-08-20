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
    const storageDeleteButton =
        document.getElementById("storage-delete");
    const receiverDialog =
        document.getElementById("receiver-dialog");
    const receiverOpenButton =
        document.getElementById("receiver-open");
    const clientIdentityDialog =
        document.getElementById("client-identity-dialog");
    const clientIdentityOpenButton =
        document.getElementById("client-identity-open");
    const sshDialog = document.getElementById("ssh-dialog");
    const TERMINAL_STATES = new Set(["SUCCESS", "FAILED"]);
    const RECENT_RUN_LIMIT = 5;
    const LIVE_REFRESH_INTERVAL_MS = 2000;
    let recentRunOffset = 0;
    let recentRunFilter = "ALL";
    const openBackupJobs = new Set();
    const backupCache = new Map();
    let currentModel = null;
    let editingJobId = null;
    let editingStorageId = null;
    let sshSetupDestination = null;
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
        const runPage = dataset.runPage;
        const summary = runPage && runPage.summary ? runPage.summary : {};

        return {
            ...dataset,
            runs: Array.isArray(runPage && runPage.items) ?
                runPage.items : [],
            now: now,
            vmById: vmById,
            jobById: jobById,
            storageById: storageById,
            successfulToday: Number(summary.successful_today || 0),
            failedToday: Number(summary.failed_today || 0),
            active: Number(summary.active || 0),
            recoveryRequired: Number(summary.recovery_required || 0),
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

    function backupDestinationText(model, location) {
        const destination =
            model.storageById.get(location.destination_id);

        if (!destination)
            return `Unknown destination (${text(location.destination_id)})`;

        if (storageType(destination) === "SSH") {
            const remoteStorage = destination.remote_storage_id ?
                ` / storage ${destination.remote_storage_id}` : "";

            return `${destination.name} — ${sshTarget(destination)}${remoteStorage}`;
        }

        return `${destination.name} — ${text(destination.backup_data_root)}`;
    }

    function renderBackupList(model, points, target) {
        if (!Array.isArray(points) || points.length === 0) {
            target.replaceChildren(
                element(
                    "div",
                    "No AVAILABLE backups for this job",
                    "backup-empty",
                )
            );
            return;
        }

        const items = points.map(point => {
            const item = document.createElement("div");
            item.className = "backup-list-item";

            const heading = document.createElement("div");
            heading.className = "backup-list-heading";

            heading.append(
                element(
                    "strong",
                    `${localTimestamp(point.created_at)} · ${text(point.kind)}`,
                ),
                badge(
                    text(point.status),
                    point.status === "AVAILABLE" ?
                        "status-success" :
                        "status-neutral",
                ),
            );

            item.append(heading);

            if (
                point.kind === "INCREMENTAL" &&
                Number(point.sequence) > 0
            ) {
                item.append(
                    element(
                        "div",
                        `Incremental #${point.sequence}`,
                        "backup-secondary",
                    )
                );
            }

            const locations = Array.isArray(point.locations) ?
                point.locations : [];

            if (!locations.length) {
                item.append(
                    element(
                        "div",
                        "No storage locations recorded",
                        "backup-secondary",
                    )
                );
            } else {
                for (const location of locations) {
                    const role =
                        location.role === "PRIMARY" ?
                            "Primary" :
                            location.role === "REPLICA" ?
                                "Replica" :
                                text(location.role);

                    const row = document.createElement("div");
                    row.className = "backup-location";

                    row.append(
                        element(
                            "span",
                            `${role}: ${backupDestinationText(model, location)}`,
                        ),
                        badge(
                            text(location.state),
                            location.state === "AVAILABLE" ?
                                "status-success" :
                                "status-neutral",
                        ),
                    );

                    item.append(row);
                }
            }

            return item;
        });

        target.replaceChildren(...items);
    }

    function jobBackupDetails(job, model) {
        const overview = job.overview || {};
        const count = Number(overview.backup_count || 0);

        const details = document.createElement("details");
        details.className = "backup-details";
        details.open = openBackupJobs.has(job.id);

        const summary = document.createElement("summary");
        summary.textContent = `Backups (${count})`;

        const content = document.createElement("div");
        content.className = "backup-list";
        content.textContent = "Open to load backups";

        details.append(summary, content);

        async function load() {
            const cached = backupCache.get(job.id);

            if (
                cached &&
                cached.expectedCount === count
            ) {
                renderBackupList(
                    model,
                    cached.points,
                    content,
                );
                return;
            }

            content.textContent = "Loading backups…";

            try {
                const points = await api.request(
                    "restore_point.list",
                    {
                        job_id: job.id,
                        include_locations: true,
                    },
                );

                backupCache.set(
                    job.id,
                    {
                        expectedCount: count,
                        points: points,
                    },
                );

                renderBackupList(
                    model,
                    points,
                    content,
                );
            } catch (error) {
                content.textContent =
                    failureMessage(error);
                content.className =
                    "backup-list error-cell";
            }
        }

        details.addEventListener("toggle", () => {
            if (details.open) {
                openBackupJobs.add(job.id);
                void load();
            } else {
                openBackupJobs.delete(job.id);
            }
        });

        if (details.open)
            void load();

        return details;
    }

    function renderJobs(model) {
        const jobs = [...model.jobs].sort((left, right) =>
            vmName(
                model.vmById,
                left.vm_id
            ).localeCompare(
                vmName(model.vmById, right.vm_id)
            ) ||
            left.name.localeCompare(right.name)
        );

        const rows = jobs.map(job => {
            const overview = job.overview || {};
            const lastRun = overview.last_run || null;
            const successfulPoint =
                overview.latest_available_restore_point || null;

            const destination =
                model.storageById.get(
                    job.storage_destination_id
                );

            const activeForVm =
                overview.active_for_vm === true;

            const recoveryForVm =
                overview.recovery_for_vm === true;

            let runDisabledReason = null;

            if (!model.status.libvirt_mutation_enabled)
                runDisabledReason =
                    "Libvirt mutation is disabled";
            else if (!job.enabled)
                runDisabledReason =
                    "The backup job is disabled";
            else if (recoveryForVm)
                runDisabledReason =
                    "The VM requires recovery";
            else if (activeForVm)
                runDisabledReason =
                    "The VM already has active work";

            const actions = document.createElement("div");
            actions.className = "row-actions";

            actions.append(
                actionButton(
                    "Edit",
                    () => openJobDialog(job),
                    false,
                ),
                actionButton(
                    job.enabled ? "Disable" : "Enable",
                    () => updateJob(
                        job.id,
                        { enabled: !job.enabled },
                    ),
                    false,
                ),
                actionButton(
                    "Run now",
                    () => runNow(job),
                    Boolean(runDisabledReason),
                    runDisabledReason,
                ),
                jobBackupDetails(job, model),
            );

            return tableRow([
                vmName(model.vmById, job.vm_id),
                job.name,
                badge(
                    job.enabled ? "Enabled" : "Disabled",
                    job.enabled ?
                        "status-success" :
                        "status-neutral",
                ),
                destination ?
                    destination.name :
                    `Unknown destination (${text(job.storage_destination_id)})`,
                lastRun ?
                    localTimestamp(lastRun.created_at) :
                    "Never",
                lastRun ?
                    badge(
                        statusLabel(lastRun),
                        statusClass(lastRun),
                    ) :
                    "—",
                successfulPoint ?
                    localTimestamp(
                        successfulPoint.created_at
                    ) :
                    "Never",
                job.next_run_at ?
                    localTimestamp(job.next_run_at) :
                    "Manual / not scheduled",
                actions,
            ]);
        });

        replaceRows(
            "jobs",
            rows,
            9,
            "No backup jobs configured",
        );
    }

    const sshStorageProbeResults = new Map();

    function storageFreeText(destination) {
        if (storageType(destination) !== "SSH")
            return bytes(destination.free_bytes);

        const probe =
            sshStorageProbeResults.get(destination.id);

        if (!probe)
            return "Not checked";

        if (
            probe.free_bytes === null ||
            probe.free_bytes === undefined
        )
            return "Unknown";

        return bytes(probe.free_bytes);
    }

    function storageType(destination) {
        if (destination && destination.storage_type)
            return destination.storage_type;
        return destination && destination.type === "SSH" ? "SSH" : "LOCAL";
    }

    function sshTarget(destination) {
        const host = text(destination.ssh_host);
        const port = Number(destination.ssh_port);
        if (!destination.ssh_host || !Number.isInteger(port))
            return "Incomplete endpoint";
        const displayedHost = destination.ssh_host.includes(":") &&
            !destination.ssh_host.startsWith("[") ?
            `[${destination.ssh_host}]` : destination.ssh_host;
        return `${displayedHost}:${port}`;
    }

    function storageDestinationCell(destination) {
        const container = document.createElement("div");
        const primary = element(
            "div",
            storageType(destination) === "SSH" ?
                (
                    destination.remote_storage_id ?
                        `Remote storage ${destination.remote_storage_id}` :
                        `Legacy remote path ${destination.ssh_remote_root || "unknown"}`
                ) :
                destination.backup_data_root,
            "storage-primary-path",
        );
        container.append(primary);

        if (storageType(destination) === "SSH") {
            container.append(element(
                "div",
                "Staging managed automatically",
                "storage-secondary",
            ));
        }

        return container;
    }

    function renderStorage(model) {
        const rows = model.storage.map(destination => {
            const type = storageType(destination);
            const isSSH = type === "SSH";

            const actions = document.createElement("div");
            actions.className = "row-actions";
            actions.append(
                actionButton("Edit", () => openStorageDialog(destination), false),
            );

            actions.append(
                actionButton(
                    "Test",
                    () => testStoredDestination(destination),
                    false,
                ),
            );

            if (isSSH)
                actions.append(
                    actionButton(
                        "SSH setup",
                        () => openSSHSetup(destination),
                        false,
                    ),
                );

            if (!isSSH && !destination.is_default)
                actions.append(
                    actionButton("Set default", () => setDefaultStorage(destination), false),
                );

            const target = isSSH ?
                sshTarget(destination) :
                text(model.status.node_name);

            const reserve = isSSH ?
                `Remote: ${bytes(destination.minimum_free_bytes)} / ${text(destination.minimum_free_percent)}%` :
                `${bytes(destination.minimum_free_bytes)} / ${text(destination.minimum_free_percent)}%`;

            return tableRow([
                destination.name,
                badge(
                    isSSH ? "SSH" : "Local",
                    isSSH ? "status-active" : "status-neutral",
                ),
                [target, "nowrap"],
                storageDestinationCell(destination),
                [destination.is_default ? "Yes" : "No", "nowrap"],
                [storageFreeText(destination), "nowrap"],
                [reserve, "nowrap"],
                actions,
            ]);
        });

        replaceRows(
            "storage",
            rows,
            8,
            "No storage destinations configured",
        );
    }

    function exactByteParts(value) {
        const bytesValue = Number(value);
        for (const unit of [1073741824, 1048576, 1]) {
            if (Number.isSafeInteger(bytesValue) && bytesValue % unit === 0)
                return { value: bytesValue / unit, unit };
        }
        return { value: bytesValue, unit: 1 };
    }

    function cleanPath(value) {
        const path = String(value ?? "").trim();

        if (!path || path === "/")
            return path;

        return path.replace(/\/+$/, "");
    }

    function minimumFreeBytes() {
        const value = Number(document.getElementById("storage-minimum-value").value);
        const unit = Number(document.getElementById("storage-minimum-unit").value);
        const result = value * unit;
        if (!Number.isSafeInteger(value) || value < 0 || !Number.isSafeInteger(result))
            throw new Error("Minimum free space must be an exact non-negative integer");
        return result;
    }

    function localStorageProbeSummary(result) {
        const state = result.ok ?
            (result.will_create ? "Ready to create" : "Ready") :
            "Not ready";

        let value =
            `${state}. ` +
            `Total ${bytes(result.total_bytes)}; ` +
            `free ${bytes(result.free_bytes)}; ` +
            `required reserve ${bytes(result.required_reserve_bytes)}; ` +
            `usable after reserve ${bytes(result.usable_after_reserve_bytes)}.`;

        if (Array.isArray(result.errors) && result.errors.length)
            value += ` ${result.errors.join("; ")}`;

        return value;
    }

    function showProbeResult(
        node,
        result,
        failedMessage = null,
    ) {
        node.hidden = false;

        if (failedMessage) {
            node.className = "probe-result error";
            node.textContent = failedMessage;
            return;
        }

        node.className = `probe-result ${result && result.ok ? "success" : "error"}`;

        if (
            result &&
            result.probe_type === "LOCAL" &&
            Object.prototype.hasOwnProperty.call(
                result,
                "total_bytes",
            )
        ) {
            node.textContent = localStorageProbeSummary(result);
            return;
        }


        if (result && result.storage_type === "SSH") {
            const values = [
                `authenticated ${result.authenticated === true ? "yes" : "no"}`,
                `host key verified ${result.host_key_verified === true ? "yes" : "no"}`,
                `receiver preflight ${result.preflight_ready === true ? "ready" : "not ready"}`,
            ];

            if (result.backup_root)
                values.push(`root ${text(result.backup_root)}`);

            if (result.writable !== undefined)
                values.push(`writable ${result.writable ? "yes" : "no"}`);

            if (result.free_bytes !== undefined)
                values.push(`free ${bytes(result.free_bytes)}`);

            if (result.total_bytes !== undefined)
                values.push(`total ${bytes(result.total_bytes)}`);

            values.push(
                `backup transfer ${
                    result.transport_ready === true ?
                        "ready" :
                        "not enabled yet"
                }`
            );

            node.textContent =
                `SSH preflight ${result.ok ? "passed" : "failed"}; ` +
                `${values.join("; ")}.`;
            return;
        }

        node.textContent =
            `${result.message}; free ${bytes(result.free_bytes)}. ` +
            "Daemon-side Backup-location filesystem test only; " +
            "no VM backup was run.";
    }

    function updateStorageTransportFields() {
        const type = document.getElementById("storage-type").value;
        const isSSH = type === "SSH";

        const sshFields = document.getElementById("storage-ssh-fields");
        sshFields.hidden = !isSSH;

        for (const id of [
            "storage-ssh-host",
            "storage-ssh-port",
            "storage-ssh-user",
        ]) {
            const field = document.getElementById(id);
            field.required = isSSH;
        }

        const dataRootLabel =
            document.getElementById("storage-data-root-label");
        const dataRoot =
            document.getElementById("storage-data-root");

        dataRootLabel.hidden = isSSH;
        dataRoot.required = !isSSH;

        document.getElementById("storage-data-root-title").textContent =
            "Destination path";
        document.getElementById("storage-data-root-help").textContent =
            "Stores the complete VM backup bundle on this node.";

        document.getElementById("storage-reserve-note").textContent =
            isSSH ?
                "SSH Test evaluates reserve against receiver capacity. Local staging is managed automatically." :
                "Reserve applies to the local destination filesystem.";

        const testButton = document.getElementById("storage-test-candidate");
        testButton.hidden = isSSH;

        const defaultCheckbox = document.getElementById("storage-default");
        const existingDestination = currentModel && editingStorageId ?
            currentModel.storage.find(value => value.id === editingStorageId) : null;

        defaultCheckbox.disabled =
            isSSH || Boolean(existingDestination && existingDestination.is_default);

        if (isSSH && !existingDestination)
            defaultCheckbox.checked = false;

        const resultNode = document.getElementById(
            "storage-dialog-test-result"
        );

        if (isSSH) {
            resultNode.hidden = true;
        } else {
            resultNode.hidden = true;
        }

        document.getElementById(
            "storage-test-candidate"
        ).hidden = isSSH;

        updateStorageSaveState();
    }

    let storageSSHDiscoverySignature = null;
    let storageSSHDiscoveryStorages = [];
    let storageSSHInitialRemoteStorageId = null;

    function storageSSHEndpoint() {
        return {
            host:
                document.getElementById(
                    "storage-ssh-host"
                ).value.trim(),
            port:
                Number(
                    document.getElementById(
                        "storage-ssh-port"
                    ).value
                ),
            user:
                document.getElementById(
                    "storage-ssh-user"
                ).value.trim(),
        };
    }

    function storageSSHEndpointSignature() {
        const endpoint = storageSSHEndpoint();

        if (
            !endpoint.host ||
            !Number.isInteger(endpoint.port) ||
            endpoint.port < 1 ||
            endpoint.port > 65535 ||
            !endpoint.user
        )
            return null;

        return JSON.stringify([
            endpoint.host,
            endpoint.port,
            endpoint.user,
        ]);
    }

    function discoveryByteText(value) {
        if (value === null || value === undefined)
            return "unknown";

        const bytes = Number(value);

        if (!Number.isFinite(bytes) || bytes < 0)
            return "unknown";

        const units = [
            "B", "KiB", "MiB", "GiB", "TiB", "PiB",
        ];

        let amount = bytes;
        let unit = 0;

        while (
            amount >= 1024 &&
            unit < units.length - 1
        ) {
            amount /= 1024;
            unit += 1;
        }

        const precision =
            unit === 0 ? 0 :
            amount >= 100 ? 0 :
            amount >= 10 ? 1 : 2;

        return `${amount.toFixed(precision)} ${units[unit]}`;
    }

    function updateStorageSaveState() {
        const submit = document.querySelector(
            "#storage-form button[type='submit']"
        );

        if (!submit)
            return;

        const type =
            document.getElementById(
                "storage-type"
            ).value;

        if (type !== "SSH") {
            submit.disabled = false;
            return;
        }

        const signature =
            storageSSHEndpointSignature();

        const select =
            document.getElementById(
                "storage-ssh-remote-storage"
            );

        const selected = storageSSHDiscoveryStorages.find(
            item => item.id === select.value
        );

        submit.disabled = !(
            signature &&
            signature === storageSSHDiscoverySignature &&
            selected &&
            selected.ready === true
        );
    }

    function resetStorageSSHDiscovery(
        message = "Check connection to load remote storage."
    ) {
        storageSSHDiscoverySignature = null;
        storageSSHDiscoveryStorages = [];

        const select = document.getElementById(
            "storage-ssh-remote-storage"
        );

        select.replaceChildren();

        const option =
            document.createElement("option");

        option.value = "";
        option.textContent = message;

        select.append(option);
        select.disabled = true;

        const result = document.getElementById(
            "storage-ssh-discovery-result"
        );

        result.hidden = true;
        result.className = "probe-result";
        result.textContent = "";

        updateStorageSaveState();
    }

    function renderStorageSSHDiscovery(
        storages,
        preferredId = null
    ) {
        storageSSHDiscoveryStorages = storages;

        const select = document.getElementById(
            "storage-ssh-remote-storage"
        );

        select.replaceChildren();

        const placeholder =
            document.createElement("option");

        placeholder.value = "";
        placeholder.textContent =
            storages.length ?
                "Select remote storage" :
                "No remote storage exposed";

        select.append(placeholder);

        for (const storage of storages) {
            const option =
                document.createElement("option");

            option.value = storage.id;

            const state =
                storage.ready ? "Ready" : "Not ready";

            option.textContent =
                `${storage.name} — ${state} — ` +
                `${discoveryByteText(storage.free_bytes)} free`;

            option.disabled = storage.ready !== true;

            if (
                preferredId &&
                storage.id === preferredId &&
                storage.ready === true
            )
                option.selected = true;

            select.append(option);
        }

        select.disabled =
            storages.length === 0 ||
            document.getElementById(
                "storage-ssh-host"
            ).disabled;

        const result = document.getElementById(
            "storage-ssh-discovery-result"
        );

        result.hidden = false;
        result.className = "probe-result";

        const readyCount = storages.filter(
            item => item.ready === true
        ).length;

        result.textContent =
            `Receiver returned ${storages.length} storage ` +
            `destination(s); ${readyCount} ready.`;

        updateStorageSaveState();
    }

    async function refreshStorageSSHHostTrust() {
        const status = document.getElementById(
            "storage-ssh-trust-status"
        );

        const endpoint = storageSSHEndpoint();

        if (
            !endpoint.host ||
            !Number.isInteger(endpoint.port) ||
            endpoint.port < 1 ||
            endpoint.port > 65535
        ) {
            status.textContent =
                "Enter a valid host and SSH port.";
            return;
        }

        status.textContent =
            "Checking host trust…";

        try {
            const trust = await api.request(
                "ssh.hostkey.endpoint.show",
                {
                    host: endpoint.host,
                    port: endpoint.port,
                },
            );

            if (trust.trusted) {
                status.textContent =
                    `Trusted host key: ${text(trust.fingerprint)}`;
            } else {
                status.textContent =
                    "Host key is not trusted. Paste the verified receiver public key and click Trust host key.";
            }
        } catch (error) {
            status.textContent =
                failureMessage(error);
        }
    }

    async function trustStorageSSHHostKey() {
        const errorNode = document.getElementById(
            "storage-form-error"
        );

        errorNode.textContent = "";

        const endpoint = storageSSHEndpoint();
        const key = document.getElementById(
            "storage-ssh-hostkey"
        ).value.trim();

        if (!key) {
            errorNode.textContent =
                "Receiver host public key is required.";
            return;
        }

        try {
            const trust = await api.request(
                "ssh.hostkey.endpoint.add",
                {
                    host: endpoint.host,
                    port: endpoint.port,
                    key: key,
                },
            );

            document.getElementById(
                "storage-ssh-hostkey"
            ).value = "";

            document.getElementById(
                "storage-ssh-trust-status"
            ).textContent =
                `Trusted host key: ${text(trust.fingerprint)}`;

            resetStorageSSHDiscovery();
        } catch (error) {
            errorNode.textContent =
                failureMessage(error);
        }
    }

    async function discoverStorageSSH() {
        const errorNode = document.getElementById(
            "storage-form-error"
        );

        const result = document.getElementById(
            "storage-ssh-discovery-result"
        );

        errorNode.textContent = "";

        const signature =
            storageSSHEndpointSignature();

        if (!signature) {
            errorNode.textContent =
                "Remote host, port and user must be valid.";
            resetStorageSSHDiscovery();
            return;
        }

        const endpoint = storageSSHEndpoint();

        resetStorageSSHDiscovery(
            "Checking receiver…"
        );

        result.hidden = false;
        result.className = "probe-result";
        result.textContent =
            "Checking SSH connection and remote storage catalog…";

        try {
            const discovery = await api.request(
                "ssh.storage.discover",
                endpoint,
            );

            if (
                signature !==
                storageSSHEndpointSignature()
            ) {
                resetStorageSSHDiscovery(
                    "Endpoint changed. Check connection again."
                );
                return;
            }

            storageSSHDiscoverySignature =
                signature;

            const storages =
                Array.isArray(discovery.storages) ?
                    discovery.storages : [];

            renderStorageSSHDiscovery(
                storages,
                storageSSHInitialRemoteStorageId,
            );

            await refreshStorageSSHHostTrust();
        } catch (error) {
            resetStorageSSHDiscovery();

            result.hidden = false;
            result.className =
                "probe-result status-error";
            result.textContent =
                failureMessage(error);

            errorNode.textContent =
                failureMessage(error);
        }
    }

    function openStorageDialog(destination = null) {
        editingStorageId = destination ? destination.id : null;

        storageDeleteButton.hidden = !destination;
        storageDeleteButton.disabled = Boolean(
            destination && destination.is_default
        );
        storageDeleteButton.title =
            destination && destination.is_default ?
                "Set another destination as default before deleting this one." :
                "";

        document.getElementById("storage-dialog-title").textContent =
            destination ? "Edit destination" : "Add destination";

        document.getElementById("storage-name").value =
            destination ? destination.name : "";

        const type = destination ? storageType(destination) : "LOCAL";
        const typeSelect = document.getElementById("storage-type");
        typeSelect.value = type;
        typeSelect.disabled = Boolean(destination);

        document.getElementById("storage-type-note").hidden =
            !Boolean(destination);

        document.getElementById("storage-data-root").value =
            destination ? destination.backup_data_root : "";

        document.getElementById("storage-ssh-host").value =
            destination && destination.ssh_host ?
                destination.ssh_host : "";

        document.getElementById("storage-ssh-port").value =
            destination && destination.ssh_port ?
                destination.ssh_port : 22;

        document.getElementById("storage-ssh-user").value =
            destination && destination.ssh_user ?
                destination.ssh_user : "vmbackupd-transfer";

        const reserve = exactByteParts(
            destination ? destination.minimum_free_bytes : 0
        );

        document.getElementById("storage-minimum-value").value =
            reserve.value;

        document.getElementById("storage-minimum-unit").value =
            String(reserve.unit);

        document.getElementById("storage-minimum-percent").value =
            destination ? destination.minimum_free_percent : 5;

        const defaultCheckbox =
            document.getElementById("storage-default");

        defaultCheckbox.checked =
            destination ? destination.is_default : false;

        defaultCheckbox.disabled =
            Boolean(destination && destination.is_default);

        document.getElementById("storage-default-note").hidden =
            !Boolean(destination && destination.is_default);

        const locked = Boolean(
            destination && destination.identity_locked
        );

        document.getElementById("storage-data-root").disabled = locked;

        for (const id of [
            "storage-ssh-host",
            "storage-ssh-port",
            "storage-ssh-user",
        ])
            document.getElementById(id).disabled = locked;


        document.getElementById("storage-identity-note").hidden =
            !locked;

        document.getElementById("storage-form-error").textContent = "";

        storageSSHInitialRemoteStorageId =
            destination && destination.remote_storage_id ?
                destination.remote_storage_id : null;

        for (const id of [
            "storage-ssh-host",
            "storage-ssh-port",
            "storage-ssh-user",
        ]) {
            document.getElementById(id).oninput = () => {
                storageSSHInitialRemoteStorageId = null;

                document.getElementById(
                    "storage-ssh-trust-status"
                ).textContent =
                    "Endpoint changed; host trust must be checked again.";

                resetStorageSSHDiscovery(
                    "Endpoint changed. Check connection again."
                );
            };
        }

        document.getElementById(
            "storage-ssh-remote-storage"
        ).onchange = updateStorageSaveState;

        document.getElementById(
            "storage-ssh-check"
        ).onclick = discoverStorageSSH;

        document.getElementById(
            "storage-ssh-trust-key"
        ).onclick = trustStorageSSHHostKey;

        resetStorageSSHDiscovery();

        updateStorageTransportFields();

        storageDialog.showModal();

        if (type === "SSH")
            void refreshStorageSSHHostTrust();
    }

    function storageFormParams() {
        const type = document.getElementById("storage-type").value;

        const params = {
            name: document.getElementById("storage-name").value.trim(),
            minimum_free_bytes: minimumFreeBytes(),
            minimum_free_percent:
                Number(document.getElementById("storage-minimum-percent").value),
            make_default:
                document.getElementById("storage-default").checked,
        };

        if (type === "LOCAL") {
            params.backup_data_root = cleanPath(
                document.getElementById("storage-data-root").value
            );
        } else {
            params.storage_type = "SSH";
            params.ssh_host =
                document.getElementById("storage-ssh-host").value.trim();
            params.ssh_port =
                Number(document.getElementById("storage-ssh-port").value);
            params.ssh_user =
                document.getElementById("storage-ssh-user").value.trim();

            const signature =
                storageSSHEndpointSignature();

            const selectedId =
                document.getElementById(
                    "storage-ssh-remote-storage"
                ).value;

            const selected =
                storageSSHDiscoveryStorages.find(
                    item => item.id === selectedId
                );

            if (
                !signature ||
                signature !== storageSSHDiscoverySignature ||
                !selected ||
                selected.ready !== true
            )
                throw new Error(
                    "Check connection and select a ready remote storage before saving."
                );

            params.remote_storage_id =
                selected.id;

            // Explicitly clears a legacy path when editing an old
            // SSH destination into the v11 stable-ID contract.
            params.ssh_remote_root = null;
        }

        return params;
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
            const typeLabel =
                storageType(destination) === "SSH" ? "SSH" : "Local";
            resultNode.textContent =
                `Testing ${typeLabel} destination ${destination.name}…`;
            const result = await api.request(
                "storage.test",
                { id: destination.id },
            );

            if (storageType(destination) === "SSH") {
                sshStorageProbeResults.set(
                    destination.id,
                    result,
                );

                if (currentModel)
                    renderStorage(currentModel);
            }

            showProbeResult(resultNode, result);
        } catch (error) {
            if (storageType(destination) === "SSH") {
                sshStorageProbeResults.delete(
                    destination.id
                );

                if (currentModel)
                    renderStorage(currentModel);
            }
            showProbeResult(
                resultNode,
                null,
                failureMessage(error),
            );
        }
    }

    async function testStorageCandidate() {
        const resultNode =
            document.getElementById("storage-dialog-test-result");

        if (document.getElementById("storage-type").value === "SSH") {
            resultNode.hidden = false;
            resultNode.className = "probe-result";
            resultNode.textContent =
                "Save this SSH destination first, then use Test in the Storage table to run receiver preflight.";
            return;
        }

        resultNode.hidden = true;

        try {
            const params = storageFormParams();
            delete params.name;
            delete params.make_default;
            delete params.storage_type;

            const result = await api.request(
                "storage.test",
                params,
            );

            showProbeResult(resultNode, result);
        } catch (error) {
            showProbeResult(
                resultNode,
                null,
                failureMessage(error),
            );
        }
    }

    function setClientIdentityError(message) {
        document.getElementById(
            "client-identity-error"
        ).textContent = message || "";
    }

    function renderClientIdentity(identity) {
        const exists = Boolean(identity && identity.exists);

        document.getElementById(
            "client-identity-status"
        ).textContent = exists ? "Generated" : "Not generated";

        document.getElementById(
            "client-identity-fingerprint"
        ).textContent =
            exists ? text(identity.fingerprint) : "—";

        document.getElementById(
            "client-identity-public-key"
        ).value =
            exists ? text(identity.public_key) : "";

        document.getElementById(
            "client-identity-generate"
        ).hidden = exists;

        document.getElementById(
            "client-identity-rotate"
        ).hidden = !exists;
    }

    async function refreshClientIdentity() {
        setClientIdentityError("");

        try {
            const identity = await api.request("ssh.identity.show");
            renderClientIdentity(identity);
        } catch (error) {
            setClientIdentityError(failureMessage(error));
        }
    }

    async function openClientIdentity() {
        renderClientIdentity({
            exists: false,
            fingerprint: null,
            public_key: null,
        });

        setClientIdentityError("");
        clientIdentityDialog.showModal();
        await refreshClientIdentity();
    }

    async function generateClientIdentity() {
        setClientIdentityError("");

        try {
            await api.request("ssh.identity.generate");
            await refreshClientIdentity();
            setNotice(
                "Shared Client SSH identity generated",
                "success",
            );
        } catch (error) {
            setClientIdentityError(failureMessage(error));
        }
    }

    async function rotateClientIdentity() {
        if (!window.confirm(
            "Rotate this SSH client identity? " +
            "It is shared by all outgoing SSH destinations. " +
            "Every receiver authorized with the current public key " +
            "must be updated."
        ))
            return;

        setClientIdentityError("");

        try {
            await api.request("ssh.identity.rotate");
            await refreshClientIdentity();
            setNotice(
                "Shared Client SSH identity rotated",
                "success",
            );
        } catch (error) {
            setClientIdentityError(failureMessage(error));
        }
    }

    function setSSHSetupError(message) {
        document.getElementById("ssh-setup-error").textContent =
            message || "";
    }

    function renderSSHIdentity(result) {
        const exists = Boolean(result && result.exists);

        document.getElementById("ssh-identity-status").replaceChildren(
            badge(
                exists ? "Generated" : "Not generated",
                exists ? "status-success" : "status-warning",
            ),
        );

        document.getElementById("ssh-identity-fingerprint").textContent =
            exists ? text(result.fingerprint) : "—";

        document.getElementById("ssh-identity-public-key").value =
            exists ? text(result.public_key) : "";

        document.getElementById("ssh-identity-generate").hidden = exists;
        document.getElementById("ssh-identity-rotate").hidden = !exists;
    }

    function renderSSHHostKey(result) {
        const trusted = Boolean(result && result.trusted);

        document.getElementById("ssh-hostkey-status").replaceChildren(
            badge(
                trusted ? "Trusted" : "Not trusted",
                trusted ? "status-success" : "status-warning",
            ),
        );

        document.getElementById("ssh-hostkey-endpoint").textContent =
            result ? text(result.host_token) : "—";

        document.getElementById("ssh-hostkey-type").textContent =
            trusted ? text(result.key_type) : "—";

        document.getElementById("ssh-hostkey-fingerprint").textContent =
            trusted ? text(result.fingerprint) : "—";

        document.getElementById("ssh-hostkey-public-key").value =
            trusted ? text(result.public_key) : "";

        document.getElementById("ssh-hostkey-input-label").hidden =
            trusted;

        document.getElementById("ssh-hostkey-add").hidden =
            trusted;

        document.getElementById("ssh-hostkey-revoke").hidden =
            !trusted;
    }

    async function refreshSSHSetup() {
        if (!sshSetupDestination)
            return;

        setSSHSetupError("");

        try {
            const [identity, hostkey] = await Promise.all([
                api.request(
                    "ssh.identity.show",
                    { destination_id: sshSetupDestination.id },
                ),
                api.request(
                    "ssh.hostkey.show",
                    { destination_id: sshSetupDestination.id },
                ),
            ]);

            renderSSHIdentity(identity);
            renderSSHHostKey(hostkey);
        } catch (error) {
            setSSHSetupError(failureMessage(error));
        }
    }

    async function openSSHSetup(destination) {
        if (storageType(destination) !== "SSH") {
            setNotice(
                "SSH setup is available only for SSH destinations.",
                "error",
            );
            return;
        }

        sshSetupDestination = destination;

        document.getElementById("ssh-dialog-title").textContent =
            `SSH setup — ${destination.name}`;

        document.getElementById("ssh-destination-summary").textContent =
            destination.remote_storage_id ?
                `${sshTarget(destination)} → remote storage ${text(destination.remote_storage_id)}` :
                `${sshTarget(destination)} → legacy path ${text(destination.ssh_remote_root)}`;

        document.getElementById("ssh-hostkey-input").value = "";
        setSSHSetupError("");

        renderSSHIdentity({
            exists: false,
            public_key: null,
            fingerprint: null,
        });

        renderSSHHostKey({
            host_token: sshTarget(destination),
            trusted: false,
            key_type: null,
            public_key: null,
            fingerprint: null,
        });

        sshDialog.showModal();
        await refreshSSHSetup();
    }

    async function generateSSHIdentity() {
        if (!sshSetupDestination)
            return;

        setSSHSetupError("");

        try {
            await api.request(
                "ssh.identity.generate",
                { destination_id: sshSetupDestination.id },
            );

            await refreshSSHSetup();

            setNotice(
                "Shared Client SSH identity generated",
                "success",
            );
        } catch (error) {
            setSSHSetupError(failureMessage(error));
        }
    }

    async function rotateSSHIdentity() {
        if (!sshSetupDestination)
            return;

        if (!window.confirm(
            "Rotate this SSH client identity? It is shared by all outgoing SSH destinations. Every receiver authorized with the current public key must be updated."
        ))
            return;

        setSSHSetupError("");

        try {
            await api.request(
                "ssh.identity.rotate",
                { destination_id: sshSetupDestination.id },
            );

            await refreshSSHSetup();

            setNotice(
                "Shared Client SSH identity rotated",
                "success",
            );
        } catch (error) {
            setSSHSetupError(failureMessage(error));
        }
    }

    async function addSSHHostKey() {
        if (!sshSetupDestination)
            return;

        const key =
            document.getElementById("ssh-hostkey-input").value.trim();

        if (!key) {
            setSSHSetupError(
                "Paste the server host public key before trusting it."
            );
            return;
        }

        setSSHSetupError("");

        try {
            await api.request(
                "ssh.hostkey.add",
                {
                    destination_id: sshSetupDestination.id,
                    key: key,
                },
            );

            document.getElementById("ssh-hostkey-input").value = "";
            await refreshSSHSetup();

            setNotice(
                `SSH server host key trusted for ${sshSetupDestination.name}`,
                "success",
            );
        } catch (error) {
            setSSHSetupError(failureMessage(error));
        }
    }

    async function revokeSSHHostKey() {
        if (!sshSetupDestination)
            return;

        if (!window.confirm(
            "Revoke trust for this SSH server host key? SSH connections will fail closed until a host key is explicitly trusted again."
        ))
            return;

        setSSHSetupError("");

        try {
            await api.request(
                "ssh.hostkey.revoke",
                { destination_id: sshSetupDestination.id },
            );

            await refreshSSHSetup();

            setNotice(
                `SSH server host trust revoked for ${sshSetupDestination.name}`,
                "success",
            );
        } catch (error) {
            setSSHSetupError(failureMessage(error));
        }
    }

    function setReceiverSetupError(message) {
        document.getElementById("receiver-setup-error").textContent =
            message || "";
    }

    function renderReceiverInfo(info) {
        document.getElementById("receiver-account").textContent =
            text(info.account);
        document.getElementById("receiver-port").textContent =
            text(info.port);
        document.getElementById("receiver-backup-root").textContent =
            text(info.backup_root);

        document.getElementById("receiver-hostkey-status").textContent =
            info.host_key_exists ? "Available" : "Not generated";

        document.getElementById(
            "receiver-hostkey-fingerprint"
        ).textContent = info.host_fingerprint || "—";

        document.getElementById(
            "receiver-host-public-key"
        ).value = info.host_public_key || "";
    }

    function renderReceiverSources(sources) {
        for (const bodyId of [
            "receiver-sources-summary",
            "receiver-sources",
        ]) {
            const body = document.getElementById(bodyId);
            if (!body)
                continue;

            const rows = [];

            for (const source of sources) {
                const row = document.createElement("tr");

                const label = document.createElement("td");
                label.textContent = source.label;

                const fingerprint = document.createElement("td");
                fingerprint.textContent = source.fingerprint;

                const actions = document.createElement("td");
                const revoke = document.createElement("button");
                revoke.type = "button";
                revoke.textContent = "Revoke";
                revoke.addEventListener("click", () => {
                    void revokeReceiverSource(source);
                });

                actions.appendChild(revoke);
                row.append(label, fingerprint, actions);
                rows.push(row);
            }

            if (rows.length === 0) {
                const row = document.createElement("tr");
                const cell = document.createElement("td");
                cell.colSpan = 3;
                cell.textContent = "No authorized sources";
                row.appendChild(cell);
                rows.push(row);
            }

            body.replaceChildren(...rows);
        }
    }

    async function refreshReceiverSourcesSummary() {
        try {
            const sources = await api.request("receiver.key.list");
            renderReceiverSources(sources);
        } catch (error) {
            const body =
                document.getElementById("receiver-sources-summary");
            if (!body)
                return;

            const row = document.createElement("tr");
            const cell = document.createElement("td");
            cell.colSpan = 3;
            cell.textContent =
                `Unable to load authorized sources: ${failureMessage(error)}`;
            row.appendChild(cell);
            body.replaceChildren(row);
        }
    }

    async function refreshReceiverSetup() {
        setReceiverSetupError("");

        try {
            const [info, sources] = await Promise.all([
                api.request("receiver.info"),
                api.request("receiver.key.list"),
            ]);

            renderReceiverInfo(info);
            renderReceiverSources(sources);
        } catch (error) {
            setReceiverSetupError(failureMessage(error));
        }
    }

    async function openReceiverSetup() {
        document.getElementById("receiver-source-label").value = "";
        document.getElementById("receiver-source-key").value = "";
        setReceiverSetupError("");

        receiverDialog.showModal();
        await refreshReceiverSetup();
    }

    async function addReceiverSource() {
        const label =
            document.getElementById("receiver-source-label").value.trim();
        const key =
            document.getElementById("receiver-source-key").value.trim();

        if (!label) {
            setReceiverSetupError("Enter a source name.");
            return;
        }

        if (!key) {
            setReceiverSetupError(
                "Paste the source Client identity public key."
            );
            return;
        }

        setReceiverSetupError("");

        try {
            await api.request(
                "receiver.key.add",
                {
                    label: label,
                    key: key,
                },
            );

            document.getElementById(
                "receiver-source-label"
            ).value = "";
            document.getElementById(
                "receiver-source-key"
            ).value = "";

            await refreshReceiverSetup();
            setNotice(`Authorized SSH source ${label}`, "success");
        } catch (error) {
            setReceiverSetupError(failureMessage(error));
        }
    }

    async function revokeReceiverSource(source) {
        if (!window.confirm(
            `Revoke SSH authorization for ${source.label}?`
        ))
            return;

        setReceiverSetupError("");

        try {
            await api.request(
                "receiver.key.revoke",
                { fingerprint: source.fingerprint },
            );

            await refreshReceiverSetup();
            setNotice(
                `Revoked SSH source ${source.label}`,
                "success",
            );
        } catch (error) {
            setReceiverSetupError(failureMessage(error));
        }
    }

    async function deleteStorageDestination() {
        const errorNode =
            document.getElementById("storage-form-error");

        errorNode.textContent = "";

        if (!editingStorageId) {
            errorNode.textContent =
                "No destination is selected for deletion.";
            return;
        }

        const name =
            document.getElementById("storage-name").value.trim();

        const path =
            document.getElementById("storage-data-root").value.trim();

        if (!window.confirm(
            `Remove destination "${name}" from vmbackupd?\n\n` +
            `Filesystem path: ${path}\n\n` +
            "Only the vmbackupd catalog entry will be removed. " +
            "The directory and all files inside it will be preserved."
        ))
            return;

        storageDeleteButton.disabled = true;

        try {
            await api.request(
                "storage.delete",
                { id: editingStorageId },
            );

            storageDialog.close();
            await refresh();

            setNotice(
                `${name} was removed from the vmbackupd catalog. ` +
                "Filesystem contents were preserved.",
                "success",
            );
        } catch (error) {
            errorNode.textContent = failureMessage(error);
        } finally {
            storageDeleteButton.disabled = false;
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
        void refreshReceiverSourcesSummary();
    }

    function populateJobOptions(editingJob) {
        const vmSelect = document.getElementById("job-vm");
        const storageSelect = document.getElementById("job-storage");
        const replicaContainer = document.getElementById("job-replicas");

        const registeredByUuid = new Map(currentModel.registeredVms
            .filter(vm => vm.libvirt_domain_uuid)
            .map(vm => [vm.libvirt_domain_uuid, vm]));

        const options = [];

        for (const vm of currentModel.registeredVms)
            options.push({
                value: `registered:${vm.id}`,
                label: vm.name,
                registered: true,
            });

        currentModel.discoveredVms.forEach((vm, index) => {
            if (!registeredByUuid.has(vm.uuid))
                options.push({
                    value: `discovered:${index}`,
                    label: `${vm.name} (will register)`,
                    registered: false,
                });
        });

        vmSelect.replaceChildren(...options.map(item => {
            const option = element("option", item.label);
            option.value = item.value;
            return option;
        }));

        const primaryDestinations = currentModel.storage.filter(
            destination => storageType(destination) !== "SSH"
        );

        storageSelect.replaceChildren(
            ...primaryDestinations.map(destination => {
                const option = element(
                    "option",
                    destination.name,
                );
                option.value = destination.id;
                return option;
            })
        );

        const selectedReplicas = new Set(
            editingJob &&
            Array.isArray(editingJob.replica_destination_ids) ?
                editingJob.replica_destination_ids :
                []
        );

        replicaContainer.replaceChildren(
            ...currentModel.storage.map(destination => {
                const label = document.createElement("label");
                label.className = "replica-option";

                const checkbox = document.createElement("input");
                checkbox.type = "checkbox";
                checkbox.value = destination.id;
                checkbox.checked = selectedReplicas.has(
                    destination.id
                );

                const description = document.createElement("span");
                description.textContent =
                    storageType(destination) === "SSH" ?
                        `${destination.name} (SSH)` :
                        `${destination.name} (Local)`;

                label.append(
                    checkbox,
                    description,
                );

                return label;
            })
        );

        if (editingJob) {
            vmSelect.value =
                `registered:${editingJob.vm_id}`;
            storageSelect.value =
                editingJob.storage_destination_id;
        }

        vmSelect.disabled = Boolean(editingJob);

        updateJobReplicaOptions();
        updateRegistrationNote();
    }

    function updateJobReplicaOptions() {
        const primaryId =
            document.getElementById("job-storage").value;

        for (const checkbox of document.querySelectorAll(
            '#job-replicas input[type="checkbox"]'
        )) {
            const matchesPrimary =
                checkbox.value === primaryId;

            checkbox.disabled = matchesPrimary;

            if (matchesPrimary)
                checkbox.checked = false;
        }
    }

    function selectedJobReplicaIds() {
        return [
            ...document.querySelectorAll(
                '#job-replicas input[type="checkbox"]:checked'
            ),
        ].map(checkbox => checkbox.value);
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
            replica_destination_ids: selectedJobReplicaIds(),
            max_incrementals_per_chain: 0,
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
                    vm_id: vmId,
                    ...params,
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
        if (error instanceof api.TransportError)
            return `Cockpit administrative API channel failed: ${error.message}. Enable Administrative access and retry.`;
        const detail = error && error.message ? `: ${error.message}` : "";
        return `Unexpected frontend error${detail}`;
    }

    function hasActiveRuns() {
        return Boolean(
            currentModel &&
            Number(currentModel.active) > 0
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

    function localTodayStartIso() {
        const now = new Date();

        return new Date(
            now.getFullYear(),
            now.getMonth(),
            now.getDate(),
            0,
            0,
            0,
            0,
        ).toISOString();
    }

    function recentRunParams() {
        return {
            limit: RECENT_RUN_LIMIT,
            offset: recentRunOffset,
            result: recentRunFilter,
            summary_since: localTodayStartIso(),
        };
    }

    async function requestRecentRunPage() {
        let page = await api.request(
            "run.list",
            recentRunParams(),
        );

        const total = Number(page.total || 0);

        if (
            total > 0 &&
            recentRunOffset >= total
        ) {
            recentRunOffset =
                Math.floor(
                    (total - 1) / RECENT_RUN_LIMIT
                ) * RECENT_RUN_LIMIT;

            page = await api.request(
                "run.list",
                recentRunParams(),
            );
        }

        return page;
    }

    async function refreshRecentRunPage() {
        if (!currentModel)
            return refresh({ background: true });

        try {
            const runPage =
                await requestRecentRunPage();

            currentModel.runPage = runPage;
            currentModel.runs =
                Array.isArray(runPage.items) ?
                    runPage.items : [];

            const summary =
                runPage.summary || {};

            currentModel.successfulToday =
                Number(summary.successful_today || 0);
            currentModel.failedToday =
                Number(summary.failed_today || 0);
            currentModel.active =
                Number(summary.active || 0);
            currentModel.recoveryRequired =
                Number(summary.recovery_required || 0);
            currentModel.now = new Date();

            renderSummary(currentModel);
            renderRecentRuns(currentModel);
            scheduleLiveRefresh();

            return true;
        } catch (error) {
            setNotice(
                failureMessage(error),
                "error",
            );
            return false;
        }
    }

    async function refresh(options) {
        const background =
            Boolean(options && options.background);

        if (refreshInFlight) {
            if (background)
                return refreshInFlight;

            await refreshInFlight;
        }

        stopLiveRefresh();
        refreshButton.disabled = true;

        if (!background) {
            clearViews();
            setNotice(
                "Loading complete backup status…",
                "loading",
            );
        }

        const operation = (async () => {
            try {
                const [
                    status,
                    discoveredVms,
                    registeredVms,
                    storage,
                    jobs,
                    runPage,
                    recovery,
                ] = await Promise.all([
                    api.request("daemon.status"),
                    api.request("vm.discover"),
                    api.request("vm.list"),
                    api.request("storage.list"),
                    api.request(
                        "job.list",
                        { overview: true },
                    ),
                    requestRecentRunPage(),
                    api.request("recovery.list"),
                ]);

                const model = deriveModel(
                    {
                        status: status,
                        discoveredVms: discoveredVms,
                        registeredVms: registeredVms,
                        storage: storage,
                        jobs: jobs,
                        runPage: runPage,
                        recovery: recovery,
                    },
                    new Date(),
                );

                renderModel(model);

                if (
                    status.runtime_state === "RUNNING"
                ) {
                    setNotice(
                        "Operational data loaded",
                        "success",
                    );
                } else {
                    setNotice(
                        `Daemon runtime is ${text(status.runtime_state)}`,
                        "error",
                    );
                }

                return true;
            } catch (error) {
                setNotice(
                    failureMessage(error),
                    "error",
                );
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

    document.getElementById(
        "recent-run-filter"
    ).addEventListener("change", event => {
        recentRunFilter = event.target.value;
        recentRunOffset = 0;
        void refreshRecentRunPage();
    });

    document.getElementById(
        "recent-run-prev"
    ).addEventListener("click", () => {
        recentRunOffset = Math.max(
            0,
            recentRunOffset - RECENT_RUN_LIMIT,
        );
        void refreshRecentRunPage();
    });

    document.getElementById(
        "recent-run-next"
    ).addEventListener("click", () => {
        if (!currentModel || !currentModel.runPage)
            return;

        const total =
            Number(currentModel.runPage.total || 0);

        if (
            recentRunOffset + RECENT_RUN_LIMIT <
            total
        ) {
            recentRunOffset += RECENT_RUN_LIMIT;
            void refreshRecentRunPage();
        }
    });

    refreshButton.addEventListener("click", refresh);
    addJobButton.addEventListener("click", () => openJobDialog());
    addStorageButton.addEventListener("click", () => openStorageDialog());
    document.getElementById("job-cancel").addEventListener("click", () => jobDialog.close());
    document.getElementById("job-vm").addEventListener("change", updateRegistrationNote);
    document.getElementById("job-schedule").addEventListener("change", updateScheduleFields);
    document.getElementById("job-storage").addEventListener(
        "change",
        updateJobReplicaOptions,
    );
    jobForm.addEventListener("submit", saveJob);
    document.getElementById("storage-cancel").addEventListener("click", () => storageDialog.close());
    document.getElementById("storage-test-candidate").addEventListener("click", testStorageCandidate);
    storageDeleteButton.addEventListener(
        "click",
        deleteStorageDestination,
    );
    document.getElementById("storage-type").addEventListener(
        "change",
        updateStorageTransportFields,
    );
    storageForm.addEventListener("submit", saveStorage);

    clientIdentityOpenButton.addEventListener(
        "click",
        openClientIdentity,
    );

    document.getElementById(
        "client-identity-close"
    ).addEventListener(
        "click",
        () => clientIdentityDialog.close(),
    );

    document.getElementById(
        "client-identity-generate"
    ).addEventListener(
        "click",
        generateClientIdentity,
    );

    document.getElementById(
        "client-identity-rotate"
    ).addEventListener(
        "click",
        rotateClientIdentity,
    );

    receiverOpenButton.addEventListener(
        "click",
        openReceiverSetup,
    );

    document.getElementById("receiver-close").addEventListener(
        "click",
        () => receiverDialog.close(),
    );

    document.getElementById("receiver-source-add").addEventListener(
        "click",
        addReceiverSource,
    );

    document.getElementById("ssh-close").addEventListener(
        "click",
        () => sshDialog.close(),
    );

    sshDialog.addEventListener(
        "close",
        () => {
            sshSetupDestination = null;
            setSSHSetupError("");
        },
    );

    document.getElementById("ssh-identity-generate").addEventListener(
        "click",
        generateSSHIdentity,
    );

    document.getElementById("ssh-identity-rotate").addEventListener(
        "click",
        rotateSSHIdentity,
    );

    document.getElementById("ssh-hostkey-add").addEventListener(
        "click",
        addSSHHostKey,
    );

    document.getElementById("ssh-hostkey-revoke").addEventListener(
        "click",
        revokeSSHHostKey,
    );
    window.addEventListener("beforeunload", () => {
        pageUnloading = true;
        stopLiveRefresh();
    });
    refresh();
}());

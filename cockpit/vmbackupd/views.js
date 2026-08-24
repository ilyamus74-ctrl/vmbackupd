let recentRunFilter = "ALL";
let recentRunPage = 0;
const RECENT_RUN_LIMIT = 5;


function setNotice(message, kind) {
    const notice = document.getElementById("notice");

    if (!notice)
        return;

    notice.textContent = message;
    notice.className =
        `notice ${kind || ""}`.trim();
}


(function () {
    "use strict";

    const api = window.VmbackupApi;
    const storageDialog = document.getElementById("storage-dialog");
    const storageForm = document.getElementById("storage-form");
    const storageDeleteButton = document.getElementById("storage-delete");
    const clientIdentityDialog = document.getElementById("client-identity-dialog");
    const receiverDialog = document.getElementById("receiver-dialog");
    const sshDialog = document.getElementById("ssh-dialog");
    let currentModel = null;
    let editingStorageId = null;
    let sshSetupDestination = null;
    let refreshCallback = async () => {};
    let runPageCallback = async () => {};
    // Presentation-only state. A persisted SSH destination is valid before any
    // probe has run, so an absent entry is an ordinary "not tested" state.
    const sshStorageProbeResults = new Map();
    const sshStorageProbeLastStarted = new Map();
    const sshStorageProbeInflight = new Set();
    const SSH_STORAGE_AUTO_PROBE_INTERVAL_MS = 30000;
    const jobBackupState = new Map();
    let mutationToggleBusy = false;
    let selectedReceivedRestorePoint = null;


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


    async function refresh() {
        return refreshCallback();
    }


    window.VmbackupViews = {
        configure({ refresh: configuredRefresh, changeRunPage }) {
            if (typeof configuredRefresh === "function")
                refreshCallback = configuredRefresh;
            if (typeof changeRunPage === "function")
                runPageCallback = changeRunPage;
        },
        renderModel(model) {
            currentModel = model;
                        renderSummary(model);

            renderDiscoveredVms(model);
            renderStorage(model);
            renderReceived(model);
            renderJobs(model);

            try {
                renderRecentRuns(model);
            } catch (e) {
                console.error("RECENT RUNS FAILED", e);
            }

            renderSystemDetails(model);
            void refreshReceiverSourcesSummary();

            setNotice(
                "Ready",
                "success",
            );

            const node =
                document.getElementById("daemon-health");

            if (node && model.status) {
                node.textContent =
                    model.status.runtime_state || "unknown";
            }
        },
    };




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
                destination.ssh_port : 22022;

        document.getElementById("storage-ssh-user").value =
            destination && destination.ssh_user ?
                destination.ssh_user : "vmbackupd-transfer";

        document.getElementById("storage-ssh-remote-root").value =
            destination && destination.ssh_remote_root ?
                destination.ssh_remote_root : "";

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
            "storage-ssh-fetch-key"
        ).onclick = fetchStorageSSHHostKey;

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
            const remoteRoot = cleanPath(
                document.getElementById("storage-ssh-remote-root").value
            );

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

            if (!selected && !remoteRoot && (
                !signature ||
                signature !== storageSSHDiscoverySignature ||
                !selected
            ))
                throw new Error(
                    "Enter a remote root or check connection and select a ready remote storage before saving."
                );

            if (
                signature &&
                signature === storageSSHDiscoverySignature &&
                selected &&
                selected.ready === true
            ) {
                params.remote_storage_id = selected.id;
                params.ssh_remote_root = null;
            } else if (remoteRoot) {
                params.ssh_remote_root = remoteRoot;
                params.remote_storage_id = null;
            } else {
                throw new Error(
                    "Check connection and select a ready remote storage before saving."
                );
            }
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
                    { status: "success", result },
                );

                if (currentModel)
                    renderStorage(currentModel);
            }

            showProbeResult(resultNode, result);
        } catch (error) {
            if (storageType(destination) === "SSH") {
                sshStorageProbeResults.set(
                    destination.id,
                    {
                        status: "failed",
                        error: failureMessage(error),
                    },
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


    function element(tag, content, className) {
        const node = document.createElement(tag);

        if (content !== undefined && content !== null) {
            node.textContent = content;
        }

        if (className) {
            node.className = className;
        }

        return node;
    }

    function text(value) {
        return value === undefined || value === null
            ? ""
            : String(value);
    }




    function badge(value, className) {
        const span = document.createElement("span");
        span.className = `badge ${className}`;
        span.textContent = text(value);
        return span;
    }




    const ACTIVE_STATES = new Set([
        "PREPARING", "BACKING_UP", "VERIFYING", "FINALIZING", "TRANSFERRING"
    ]);

    function activeStatus(value, progress = null) {
        const state = String(value || "UNKNOWN").toUpperCase();
        if (!ACTIVE_STATES.has(state))
            return badge(value, "status-neutral");

        const processed = Number(progress && (progress.bytes_processed ?? progress.processed));
        const total = Number(progress && (progress.bytes_total ?? progress.total));
        const determinate = Number.isFinite(processed) && Number.isFinite(total) && total > 0;
        const percent = determinate ? Math.max(0, Math.min(100, processed / total * 100)) : null;

        const wrapper = document.createElement("span");
        wrapper.className = "active-status";
        wrapper.title = determinate
            ? `${state}: ${bytes(processed)} of ${bytes(total)} (${percent.toFixed(1)}%)`
            : `${state}: operation is active`;

        const fill = document.createElement("span");
        fill.className = `active-status-fill${determinate ? " determinate" : ""}`;
        if (determinate)
            fill.style.width = `${percent}%`;

        const label = document.createElement("span");
        label.className = "active-status-label";
        label.textContent = determinate ? `${state} ${Math.round(percent)}%` : state;

        wrapper.append(fill, label);
        return wrapper;
    }

    function statusNode(value, className, progress = null) {
        const state = String(value || "UNKNOWN").toUpperCase();
        if (ACTIVE_STATES.has(state))
            return activeStatus(state, progress);
        return badge(value, className);
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



    function renderDiscoveredVms(model) {
        console.log(
            "VM STATES",
            model.discoveredVms.map(vm => ({
               name: vm.name,
               state: vm.state
              }))
         );
        console.log(
            "RENDER DISCOVERED VMS",
            model.discoveredVms,
        );

        const rows = model.discoveredVms.map(vm => tableRow([
            vm.name,
            vm.external_id,
            [vm.uuid, "identifier-cell"],
            badge(vm.state, "status-neutral"),
        ]));

        replaceRows(
            "vms",
            rows,
            4,
            "No libvirt virtual machines discovered"
        );
    }



    async function autoProbeStoredSSHDestination(destination) {
        if (!destination || storageType(destination) !== "SSH" || !destination.id)
            return;

        const now = Date.now();
        const lastStarted = sshStorageProbeLastStarted.get(destination.id) || 0;
        if (sshStorageProbeInflight.has(destination.id) ||
            now - lastStarted < SSH_STORAGE_AUTO_PROBE_INTERVAL_MS)
            return;

        sshStorageProbeLastStarted.set(destination.id, now);
        sshStorageProbeInflight.add(destination.id);
        try {
            const result = await api.request(
                "storage.test",
                { id: destination.id },
            );
            sshStorageProbeResults.set(
                destination.id,
                { status: "success", result },
            );
        } catch (error) {
            sshStorageProbeResults.set(
                destination.id,
                {
                    status: "failed",
                    error: failureMessage(error),
                },
            );
        } finally {
            sshStorageProbeInflight.delete(destination.id);
            if (currentModel)
                renderStorage(currentModel);
        }
    }

    function scheduleStoredSSHStorageProbes(destinations) {
        for (const destination of destinations || []) {
            if (storageType(destination) === "SSH")
                void autoProbeStoredSSHDestination(destination);
        }
    }


    function storageFreeText(destination) {
        if (storageType(destination) !== "SSH")
            return bytes(destination.free_bytes);

        const probe =
            sshStorageProbeResults.get(destination.id);

        if (!probe) {
            const remote = remoteCatalogStorage(destination);
            return remote ? bytes(remote.free_bytes) : "Not tested";
        }

        if (probe.status === "failed")
            return "Connection error";

        const result = probe.result;

        if (
            !result ||
            result.free_bytes === null ||
            result.free_bytes === undefined
        )
            return "Unknown";

        return bytes(result.free_bytes);
    }

    function remoteCatalogStorage(destination) {
        if (!destination || !destination.remote_storage_id)
            return null;
        const discovered = storageSSHDiscoveryStorages.find(
            item => item.id === destination.remote_storage_id
        );
        if (discovered)
            return discovered;

        const probe = sshStorageProbeResults.get(destination.id);
        const result = probe && probe.status === "success" ? probe.result : null;
        if (!result || result.remote_storage_id !== destination.remote_storage_id)
            return null;

        return {
            id: result.remote_storage_id,
            name: result.remote_storage_name,
            path: result.remote_storage_path,
            ready: result.ready === true,
            free_bytes: result.free_bytes,
            total_bytes: result.total_bytes,
            minimum_free_bytes: result.remote_minimum_free_bytes,
            minimum_free_percent: result.remote_minimum_free_percent,
            required_reserve_bytes: result.remote_required_reserve_bytes,
            usable_after_reserve_bytes: result.remote_usable_after_reserve_bytes,
        };
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
        const remote = remoteCatalogStorage(destination);
        const primary = element(
            "div",
            storageType(destination) === "SSH" ?
                (
                    destination.remote_storage_id ?
                        `Remote storage: ${remote ? remote.name : destination.remote_storage_id}` :
                        `Legacy remote path ${destination.ssh_remote_root || "unknown"}`
                ) :
                destination.backup_data_root,
            "storage-primary-path",
        );
        container.append(primary);

        if (storageType(destination) === "SSH") {
            const probe = sshStorageProbeResults.get(destination.id);
            const secondary = probe && probe.status === "failed" ?
                "Connection error" :
                (destination.remote_storage_id ?
                    `Destination path: ${remote ? remote.path : "unknown until catalog refresh"}` :
                    "Staging managed automatically");
            container.append(element(
                "div",
                secondary,
                "storage-secondary",
            ));
        }

        return container;
    }

    function renderStorage(model) {
        currentModel = model;
        scheduleStoredSSHStorageProbes(model.storage);
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

            const remote = isSSH ? remoteCatalogStorage(destination) : null;
            const reserve = isSSH ?
                `Remote: ${bytes(remote ? remote.minimum_free_bytes : destination.minimum_free_bytes)} / ${text(remote ? remote.minimum_free_percent : destination.minimum_free_percent)}%` :
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

        const remoteRoot = cleanPath(
            document.getElementById("storage-ssh-remote-root").value
        );

        submit.disabled = !(remoteRoot || (
            signature &&
            signature === storageSSHDiscoverySignature &&
            selected &&
            selected.ready === true
        ));
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

    async function fetchStorageSSHHostKey() {
        const errorNode = document.getElementById("storage-form-error");
        const status = document.getElementById("storage-ssh-trust-status");
        const endpoint = storageSSHEndpoint();
        errorNode.textContent = "";

        if (!endpoint.host || endpoint.host.startsWith("-") ||
            !Number.isInteger(endpoint.port) || endpoint.port < 1 || endpoint.port > 65535) {
            errorNode.textContent = "Enter a valid receiver host and port.";
            return;
        }

        status.textContent = `Fetching ed25519 host key from ${endpoint.host}:${endpoint.port}…`;

        try {
            const output = await window.cockpit.spawn(
                ["ssh-keyscan", "-T", "5", "-p", String(endpoint.port),
                    "-t", "ed25519", endpoint.host],
                { err: "message" },
            );
            const line = output.split("\n").find(value =>
                value && !value.startsWith("#") && value.includes(" ssh-ed25519 ")
            );
            if (!line)
                throw new Error("receiver returned no ed25519 host key");
            const parts = line.trim().split(/\s+/);
            const publicKey = `${parts[1]} ${parts[2]}`;
            const raw = Uint8Array.from(window.atob(parts[2]), char => char.charCodeAt(0));
            const digest = new Uint8Array(
                await window.crypto.subtle.digest("SHA-256", raw)
            );
            const fingerprint = "SHA256:" + window.btoa(
                String.fromCharCode(...digest)
            ).replace(/=+$/, "");

            document.getElementById("storage-ssh-hostkey").value = publicKey;
            status.textContent =
                `Fetched ${fingerprint} from ${endpoint.host}:${endpoint.port}. ` +
                "Verify it out of band, then click Trust host key.";
        } catch (error) {
            errorNode.textContent = failureMessage(error);
            status.textContent = "Host key fetch failed; trust was not changed.";
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
            const remoteRoot = cleanPath(
                document.getElementById("storage-ssh-remote-root").value
            );

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

            if (!selected && !remoteRoot && (
                !signature ||
                signature !== storageSSHDiscoverySignature ||
                !selected
            ))
                throw new Error(
                    "Enter a remote root or check connection and select a ready remote storage before saving."
                );

            if (
                signature &&
                signature === storageSSHDiscoverySignature &&
                selected &&
                selected.ready === true
            ) {
                params.remote_storage_id = selected.id;
                params.ssh_remote_root = null;
            } else if (remoteRoot) {
                params.ssh_remote_root = remoteRoot;
                params.remote_storage_id = null;
            } else {
                throw new Error(
                    "Check connection and select a ready remote storage before saving."
                );
            }
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
                    { status: "success", result },
                );

                if (currentModel)
                    renderStorage(currentModel);
            }

            showProbeResult(resultNode, result);
        } catch (error) {
            if (storageType(destination) === "SSH") {
                sshStorageProbeResults.set(
                    destination.id,
                    {
                        status: "failed",
                        error: failureMessage(error),
                    },
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
        document.getElementById("receiver-service-status").textContent =
            info.service_running ? "Running" : "Stopped";
        document.getElementById("receiver-restricted-status").textContent =
            info.restricted_shell_configured ? "Configured" : "Needs repair";

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
        document.getElementById("receiver-authorized-count").textContent =
            String(sources.length);
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

    async function repairReceiver() {
        setReceiverSetupError("");
        try {
            await window.cockpit.spawn(
                ["systemctl", "enable", "--now",
                    "vmbackupd-receiver-catalog.socket",
                    "vmbackupd-receiver-resolver.socket",
                    "vmbackupd-receiver-sshd.service"],
                { superuser: "require", err: "message" },
            );
            await refreshReceiverSetup();
            setNotice("SSH receiver initialized and verified", "success");
        } catch (error) {
            setReceiverSetupError(failureMessage(error));
        }
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

    async function registerVM(vm) {
        try {
            setNotice(`Registering ${vm.name}…`, "loading");
            await api.request("vm.register", {
                external_id: vm.external_id,
                name: vm.name,
            });
            await refresh();
            setNotice(`Virtual machine ${vm.name} registered`, "success");
        } catch (error) {
            setNotice(failureMessage(error), "error");
        }
    }

    function renderDiscoveredVms(model) {
        const registeredExternalIds = new Set(
            model.registeredVms.map(vm => vm.external_id)
        );
        const rows = model.discoveredVms.map(vm => {
            const registered = registeredExternalIds.has(vm.external_id);
            return tableRow([
                vm.name, vm.external_id, [vm.uuid, "identifier-cell"],
                badge(
                    vm.state,
                    String(vm.state || "").toLowerCase() === "running" ?
                        "status-success" : "status-neutral",
                ),
                registered ? badge("Registered", "status-success") :
                    actionButton("Register", () => registerVM(vm), false),
            ]);
        });
        replaceRows("vms", rows, 5, "No libvirt virtual machines discovered");
    }

    let editingJobId = null;

    function populateJobSelect(select, values, selectedId, label) {
        select.replaceChildren(...values.map(value => {
            const option = document.createElement("option");
            option.value = value.id;
            option.textContent = label(value);
            option.selected = value.id === selectedId;
            return option;
        }));
        select.value = selectedId || (values[0] ? values[0].id : "");
    }

    function isJobDestination(destination) {
        return destination.name !== "__vmbackupd_ssh_identity__";
    }

    function selectedJobReplicaIds() {
        const result = [];
        for (const label of document.getElementById("job-replicas").children) {
            const checkbox = label.children[0];
            if (checkbox && checkbox.checked && !checkbox.disabled)
                result.push(checkbox.value);
        }
        return result;
    }

    function renderJobReplicaOptions(selectedIds = null) {
        const primaryId = document.getElementById("job-storage").value;
        const selected = new Set(selectedIds || selectedJobReplicaIds());
        const candidates = currentModel.storage.filter(destination =>
            isJobDestination(destination) && destination.id !== primaryId
        );
        document.getElementById("job-replicas").replaceChildren(
            ...candidates.map(destination => {
                const label = document.createElement("label");
                label.className = "replica-option";
                const checkbox = document.createElement("input");
                checkbox.type = "checkbox";
                checkbox.value = destination.id;
                checkbox.checked = selected.has(destination.id);
                const description = document.createElement("span");
                const remote = destination.remote_storage_name ||
                    destination.remote_storage_id;
                description.textContent = storageType(destination) === "SSH" ?
                    `${destination.name} (SSH${remote ? ` → ${remote}` : ""})` :
                    `${destination.name} (Local)`;
                label.append(checkbox, description);
                return label;
            })
        );
    }

    function updateIncrementalFrequencyFields() {
        const twice = document.getElementById("job-incremental-frequency").value === "2";
        document.getElementById("job-incremental-time-2-label").hidden = !twice;
        document.getElementById("job-incremental-time-2").disabled = !twice;
    }

    function updateScheduleFields() {
        const mode = document.getElementById("job-schedule").value;
        const intervalFields = document.getElementById("interval-fields");
        const dailyFields = document.getElementById("daily-fields");
        const chainFields = document.getElementById("chain-schedule-fields");
        const intervalEnabled = mode === "interval";
        const dailyEnabled = mode === "daily";
        const chainEnabled = mode === "chain";
        intervalFields.hidden = !intervalEnabled;
        dailyFields.hidden = !dailyEnabled;
        chainFields.hidden = !chainEnabled;
        document.getElementById("job-interval").disabled = !intervalEnabled;
        document.getElementById("job-interval-unit").disabled = !intervalEnabled;
        document.getElementById("job-daily-time").disabled = !dailyEnabled;
        document.getElementById("job-schedule-timezone").disabled = !dailyEnabled;
        for (const id of ["job-chain-timezone", "job-full-weekday", "job-full-time",
                          "job-incremental-frequency", "job-incremental-time-1"])
            document.getElementById(id).disabled = !chainEnabled;
        updateIncrementalFrequencyFields();
        if (!chainEnabled)
            document.getElementById("job-incremental-time-2").disabled = true;
    }

    function openJobDialog(job = null) {
        editingJobId = job ? job.id : null;
        document.getElementById("job-dialog-title").textContent =
            job ? "Edit backup job" : "Add backup job";
        populateJobSelect(
            document.getElementById("job-vm"), currentModel.registeredVms,
            job ? job.vm_id : null, vm => vm.name,
        );
        populateJobSelect(
            document.getElementById("job-storage"),
            currentModel.storage.filter(isJobDestination),
            job ? job.storage_destination_id : null,
            destination => `${destination.name} (${storageType(destination)})`,
        );
        renderJobReplicaOptions(job ? job.replica_destination_ids : []);
        document.getElementById("job-vm").disabled = Boolean(job);
        document.getElementById("job-name").value = job ? job.name : "";
        document.getElementById("job-enabled").checked = job ? job.enabled : true;
        const scheduleMode = job && job.chain_schedule ? "chain" :
            (!job || !job.next_run_at ? "manual" :
                (job.schedule_type === "DAILY" ? "daily" : "interval"));
        document.getElementById("job-schedule").value = scheduleMode;
        const intervalSeconds = Number(job && job.interval_seconds || 3600);
        let intervalUnit = 1;
        if (intervalSeconds % 86400 === 0) intervalUnit = 86400;
        else if (intervalSeconds % 3600 === 0) intervalUnit = 3600;
        else if (intervalSeconds % 60 === 0) intervalUnit = 60;
        document.getElementById("job-interval-unit").value = String(intervalUnit);
        document.getElementById("job-interval").value =
            String(intervalSeconds / intervalUnit);
        document.getElementById("job-daily-time").value =
            job && job.daily_time ? job.daily_time : "01:00";
        document.getElementById("job-schedule-timezone").value =
            job && job.schedule_timezone ? job.schedule_timezone :
                (Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
        const chain = job && job.chain_schedule || null;
        document.getElementById("job-chain-timezone").value =
            chain && chain.timezone ? chain.timezone :
                (Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
        document.getElementById("job-full-weekday").value =
            String(chain && Number.isInteger(chain.full_weekday) ? chain.full_weekday : 6);
        document.getElementById("job-full-time").value =
            chain && chain.full_time ? chain.full_time : "02:00";
        const incrementalTimes = chain && Array.isArray(chain.incremental_times) ?
            chain.incremental_times : ["02:00"];
        document.getElementById("job-incremental-frequency").value =
            incrementalTimes.length > 1 ? "2" : "1";
        document.getElementById("job-incremental-time-1").value = incrementalTimes[0] || "02:00";
        document.getElementById("job-incremental-time-2").value = incrementalTimes[1] || "14:00";
        updateScheduleFields();
        document.getElementById("job-max-incrementals").value =
            job ? job.max_incrementals_per_chain : 0;
        document.getElementById("job-retain").value =
            job ? job.restore_points_to_retain : 7;
        document.getElementById("job-full-chains").value =
            job ? job.full_chains_to_retain : 2;
        document.getElementById("job-minimum-chains").value =
            job ? job.minimum_full_chains : 1;
        document.getElementById("job-reclaim-mode").value =
            job ? job.space_reclaim_mode : "SAFE";
        document.getElementById("job-form-error").textContent = "";
        document.getElementById("job-dialog").showModal();
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
            schedule_enabled: scheduleMode !== "manual" && scheduleMode !== "chain",
            chain_schedule_enabled: scheduleMode === "chain",
            max_incrementals_per_chain: Number(document.getElementById("job-max-incrementals").value),
            restore_points_to_retain: Number(document.getElementById("job-retain").value),
            full_chains_to_retain: Number(document.getElementById("job-full-chains").value),
            minimum_full_chains: Number(document.getElementById("job-minimum-chains").value),
            space_reclaim_mode: document.getElementById("job-reclaim-mode").value,
            replica_destination_ids: selectedJobReplicaIds(),
        };
        if (scheduleMode === "interval") {
            params.schedule_type = "INTERVAL";
            params.interval_seconds =
                Number(document.getElementById("job-interval").value) *
                Number(document.getElementById("job-interval-unit").value);
            params.daily_time = null;
            params.schedule_timezone = null;
        } else if (scheduleMode === "daily") {
            params.schedule_type = "DAILY";
            params.daily_time = document.getElementById("job-daily-time").value;
            params.schedule_timezone =
                document.getElementById("job-schedule-timezone").value.trim();
        } else if (scheduleMode === "chain") {
            params.chain_schedule_timezone =
                document.getElementById("job-chain-timezone").value.trim();
            params.full_weekday = Number(document.getElementById("job-full-weekday").value);
            params.full_time = document.getElementById("job-full-time").value;
            params.incremental_times = [
                document.getElementById("job-incremental-time-1").value,
            ];
            if (document.getElementById("job-incremental-frequency").value === "2")
                params.incremental_times.push(
                    document.getElementById("job-incremental-time-2").value
                );
        }
        try {
            if (editingJobId)
                await api.request("job.update", { id: editingJobId, ...params });
            else
                await api.request("job.create", {
                    vm_id: document.getElementById("job-vm").value,
                    ...params,
                });
            document.getElementById("job-dialog").close();
            await refresh();
            setNotice("Backup job saved", "success");
        } catch (error) {
            errorNode.textContent = failureMessage(error);
        }
    }

    async function updateJob(id, params) {
        try {
            await api.request("job.update", { id, ...params });
            await refresh();
        } catch (error) {
            setNotice(failureMessage(error), "error");
        }
    }

    async function runNow(job, kind = "AUTO") {
        try {
            setNotice(`Requesting backup for ${job.name}…`, "loading");
            const result = await api.request("backup.run", { job_id: job.id, kind });
            await refresh();
            setNotice(`Run ${result.run_id}: ${result.state}`, "success");
        } catch (error) {
            setNotice(failureMessage(error), "error");
        }
    }

    function jobBackupStateFor(jobId) {
        if (!jobBackupState.has(jobId)) {
            jobBackupState.set(jobId, {
                expanded: false,
                loading: false,
                error: null,
                items: null,
            });
        }
        return jobBackupState.get(jobId);
    }

    async function loadJobBackups(job) {
        const state = jobBackupStateFor(job.id);
        state.loading = true;
        state.error = null;
        if (currentModel)
            renderJobs(currentModel);
        try {
            state.items = await api.request(
                "restore_point.list",
                { job_id: job.id, details: true },
            );
        } catch (error) {
            state.error = failureMessage(error);
            state.items = [];
        } finally {
            state.loading = false;
            if (currentModel)
                renderJobs(currentModel);
        }
    }

    async function toggleJobBackups(job) {
        const state = jobBackupStateFor(job.id);
        state.expanded = !state.expanded;
        if (currentModel)
            renderJobs(currentModel);
        if (state.expanded && state.items === null && !state.loading)
            await loadJobBackups(job);
    }

    async function retryReplicaChain(job, point, replica) {
        try {
            setNotice(`Retrying replica chain for ${job.name}…`, "loading");
            const result = await api.request("replica.retry", {
                restore_point_id: point.id,
                destination_id: replica.destination_id,
            });
            const state = jobBackupStateFor(job.id);
            state.items = null;
            await loadJobBackups(job);
            await refresh();
            const count = Array.isArray(result.reset_restore_point_ids)
                ? result.reset_restore_point_ids.length : 0;
            setNotice(`Replica retry queued for ${count} restore point${count === 1 ? "" : "s"}.`, "success");
        } catch (error) {
            setNotice(failureMessage(error), "error");
        }
    }

    async function deleteJobBackup(job, point) {
        const path = point.bundle_object_id || "unknown";
        if (!window.confirm(
            `Delete backup permanently?\n\n` +
            `Job: ${job.name}\n` +
            `Created: ${localTimestamp(point.created_at)}\n` +
            `Storage: ${point.storage_name || point.storage_destination_id}\n` +
            `Path: ${path}\n\n` +
            "Backup files will be removed permanently. Run history will be preserved."
        ))
            return;

        try {
            await api.request(
                "restore_point.delete",
                { id: point.id, job_id: job.id },
            );
            const state = jobBackupStateFor(job.id);
            state.items = null;
            await loadJobBackups(job);
            await refresh();
            setNotice("Backup deleted. Run history was preserved.", "success");
        } catch (error) {
            setNotice(failureMessage(error), "error");
        }
    }

    function renderJobBackupsRow(job) {
        const state = jobBackupStateFor(job.id);
        const row = document.createElement("tr");
        row.className = "job-backups-row";
        const cell = document.createElement("td");
        cell.colSpan = 9;

        if (state.loading) {
            cell.textContent = "Loading backups…";
            row.append(cell);
            return row;
        }
        if (state.error) {
            cell.textContent = state.error;
            row.append(cell);
            return row;
        }
        if (!state.items || state.items.length === 0) {
            cell.textContent = "No available backups for this job.";
            row.append(cell);
            return row;
        }

        const table = document.createElement("table");
        table.className = "operational-table job-backups-table";
        const head = document.createElement("thead");
        const headRow = document.createElement("tr");
        for (const label of ["Created", "Type / inheritance", "Storage", "Path", "Size", "Status", "Replicas", "Actions"])
            headRow.append(tableCell(label));
        head.append(headRow);
        table.append(head);

        const byId = new Map(state.items.map(point => [point.id, point]));

        // Parent links are the dependency authority.  Older compact V2
        // incrementals may carry a wrong chain_id from the early migration,
        // while still pointing at the correct FULL parent.  Group those with
        // the proven FULL chain instead of showing a false orphan.  If the
        // parent is genuinely absent, keep the original chain_id and show the
        // missing-base warning below.
        function effectiveChainId(point) {
            let current = point;
            const seen = new Set();
            while (current && !seen.has(current.id)) {
                seen.add(current.id);
                if (String(current.kind || "").toUpperCase() === "FULL")
                    return current.chain_id || current.id;
                if (!current.parent_restore_point_id)
                    break;
                current = byId.get(current.parent_restore_point_id) || null;
            }
            return point.chain_id || point.id;
        }

        const chains = new Map();
        for (const point of state.items) {
            const chainId = effectiveChainId(point);
            if (!chains.has(chainId))
                chains.set(chainId, []);
            chains.get(chainId).push(point);
        }
        const chainGroups = Array.from(chains.entries()).map(([chainId, points]) => {
            const base = points.find(point =>
                String(point.kind || "").toUpperCase() === "FULL"
            ) || null;
            points.sort((a, b) => {
                const aFull = String(a.kind || "").toUpperCase() === "FULL";
                const bFull = String(b.kind || "").toUpperCase() === "FULL";
                if (aFull !== bFull) return aFull ? -1 : 1;
                const aSeq = Number.isFinite(Number(a.sequence)) && a.sequence !== null ? Number(a.sequence) : null;
                const bSeq = Number.isFinite(Number(b.sequence)) && b.sequence !== null ? Number(b.sequence) : null;
                if (aSeq !== null && bSeq !== null && aSeq !== bSeq) return aSeq - bSeq;
                return String(a.created_at || "").localeCompare(String(b.created_at || ""));
            });
            const anchor = base || points[0];
            return { chainId, points, base, anchor };
        });
        chainGroups.sort((a, b) =>
            String(b.anchor?.created_at || "").localeCompare(String(a.anchor?.created_at || ""))
        );

        const body = document.createElement("tbody");
        for (const group of chainGroups) {
            const chainHeader = document.createElement("tr");
            chainHeader.className = "backup-chain-header";
            const chainCell = document.createElement("td");
            chainCell.colSpan = 8;
            const shortChain = String(group.chainId || "").slice(0, 8);
            const incrementals = group.points.filter(point =>
                String(point.kind || "").toUpperCase() === "INCREMENTAL"
            ).length;
            const baseLabel = group.base
                ? `FULL ${localTimestamp(group.base.created_at)}`
                : "base FULL missing";
            chainCell.textContent = `Chain ${shortChain} · ${baseLabel} · ${incrementals} incremental${incrementals === 1 ? "" : "s"}`;
            chainHeader.append(chainCell);
            body.append(chainHeader);

            for (const point of group.points) {
                const actions = document.createElement("div");
                actions.className = "row-actions";
                actions.append(actionButton(
                    "Delete",
                    () => deleteJobBackup(job, point),
                    false,
                ));
                const replicaNode = document.createElement("div");
                replicaNode.className = "replica-status-list";
                const replicas = Array.isArray(point.replicas) ? point.replicas : [];
                if (!replicas.length) {
                    replicaNode.textContent = "—";
                } else {
                    for (const replica of replicas) {
                        const line = document.createElement("div");
                        const state = replica.state || "PENDING";
                        line.append(
                            document.createTextNode(`${replica.destination_name || replica.destination_id}: `),
                            statusNode(
                                state,
                                state === "SUCCESS" ? "status-success" :
                                state === "FAILED" ? "status-error" : "status-neutral",
                                {
                                    bytes_processed: replica.bytes_processed,
                                    bytes_total: replica.bytes_total,
                                },
                            ),
                        );
                        if (replica.last_error) {
                            const error = document.createElement("div");
                            error.className = "replica-status-error";
                            error.textContent = replica.last_error;
                            line.append(error);
                        }
                        if (state === "FAILED" || state === "BLOCKED") {
                            const retry = actionButton(
                                "Retry chain",
                                () => retryReplicaChain(job, point, replica),
                                false,
                            );
                            retry.classList.add("replica-retry-action");
                            line.append(retry);
                        }
                        replicaNode.append(line);
                    }
                }

                const kind = String(point.kind || "").toUpperCase();
                let relation;
                if (kind === "FULL") {
                    relation = "FULL — Base of chain";
                } else if (kind === "INCREMENTAL") {
                    const orderedIncrementals = group.points.filter(item =>
                        String(item.kind || "").toUpperCase() === "INCREMENTAL"
                    );
                    const inferredSequence = orderedIncrementals.indexOf(point) + 1;
                    const seq = point.sequence !== null && point.sequence !== undefined
                        ? Number(point.sequence) : inferredSequence;
                    const parent = point.parent_restore_point_id ? byId.get(point.parent_restore_point_id) : null;
                    let parentLabel;
                    if (parent) {
                        if (String(parent.kind || "").toUpperCase() === "FULL") {
                            parentLabel = "FULL";
                        } else {
                            const parentSequence = parent.sequence !== null && parent.sequence !== undefined
                                ? Number(parent.sequence)
                                : Math.max(1, inferredSequence - 1);
                            parentLabel = `INC #${parentSequence}`;
                        }
                    } else if (point.parent_restore_point_id) {
                        parentLabel = `missing parent ${String(point.parent_restore_point_id).slice(0, 8)}`;
                    } else if (!group.base) {
                        parentLabel = "missing FULL base";
                    } else {
                        parentLabel = "parent unknown";
                    }
                    relation = `↳ INC #${seq} ← ${parentLabel}`;
                } else {
                    relation = `${kind || "UNKNOWN"} — inheritance unknown`;
                }

                body.append(tableRow([
                    localTimestamp(point.created_at),
                    relation,
                    point.storage_name || point.storage_destination_id || "—",
                    point.bundle_object_id || "—",
                    bytes(point.size_bytes),
                    badge(point.status || "—", point.status === "AVAILABLE" ? "status-success" : "status-neutral"),
                    replicaNode,
                    actions,
                ]));
            }
        }
        table.append(body);
        cell.append(table);
        row.append(cell);
        return row;
    }

    function latestRestoreForPoint(model, pointId) {
        const values = Array.isArray(model.restores) ? model.restores : [];
        return values.filter(item => item.restore_point_id === pointId)
            .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")))[0] || null;
    }

    function openReceivedRestore(point) {
        if (typeof api.adminAllowed === "function" && !api.adminAllowed()) {
            setNotice("Cockpit Administrative access is required to restore a VM.", "error");
            return;
        }
        if (!currentModel || !currentModel.status || !currentModel.status.libvirt_mutation_enabled) {
            setNotice("Enable Mutation before restoring a VM.", "error");
            return;
        }
        selectedReceivedRestorePoint = point;
        document.getElementById("received-restore-source").textContent =
            `${point.vm_name || "received-vm"} · ${point.kind || "—"} · ${point.storage_name || point.storage_destination_id}`;
        document.getElementById("received-restore-name").value = `${point.vm_name || "received-vm"}-restored`;
        document.getElementById("received-restore-root").value = "";
        document.getElementById("received-restore-start").checked = false;
        document.getElementById("received-restore-error").textContent = "";
        document.getElementById("received-restore-dialog").showModal();
    }

    async function submitReceivedRestore(event) {
        event.preventDefault();
        if (!selectedReceivedRestorePoint)
            return;
        const error = document.getElementById("received-restore-error");
        const button = document.getElementById("received-restore-submit");
        button.disabled = true;
        error.textContent = "";
        try {
            await api.request("received.restore.create", {
                restore_point_id: selectedReceivedRestorePoint.id,
                target_vm_name: document.getElementById("received-restore-name").value.trim(),
                target_root: document.getElementById("received-restore-root").value.trim(),
                start_after_restore: document.getElementById("received-restore-start").checked,
            });
            document.getElementById("received-restore-dialog").close();
            setNotice("Restore queued. The received FULL/INC chain will be materialized locally.", "success");
            await refresh();
        } catch (exc) {
            error.textContent = failureMessage(exc);
        } finally {
            button.disabled = false;
        }
    }

    function renderReceived(model) {
        const values = Array.isArray(model.received) ? model.received : [];
        const rows = values.map(point => {
            const operation = latestRestoreForPoint(model, point.id);
            const actions = document.createElement("div");
            actions.className = "row-actions";
            const canRestore = point.status === "AVAILABLE";
            actions.append(actionButton("Restore", () => openReceivedRestore(point), !canRestore));
            if (operation) {
                actions.append(badge(
                    operation.state || "—",
                    operation.state === "SUCCESS" ? "status-success" :
                        operation.state === "FAILED" || operation.state === "RECOVERY_REQUIRED" ? "status-error" : "status-warning"
                ));
            }
            return tableRow([
                point.vm_name || "received-vm",
                localTimestamp(point.created_at),
                point.kind || "—",
                point.storage_name || point.storage_destination_id,
                badge(point.status || "UNKNOWN", point.status === "AVAILABLE" ? "status-success" : "status-neutral"),
                (point.origin && point.origin.received_via) || "SSH_REPLICA",
                point.bundle_object_id || "—",
                actions,
            ]);
        });
        replaceRows("received-backups", rows, 8, "No received backups discovered");
    }

    function renderJobs(model) {
        const rows = [];
        for (const job of model.jobs) {
            const vm = model.vmById.get(job.vm_id);
            const destination = model.storage.find(item =>
                item.id === job.storage_destination_id
            );
            const lastRun = model.runs.find(run => run.job_id === job.id);
            const backupState = jobBackupStateFor(job.id);
            const actions = document.createElement("div");
            actions.className = "row-actions";
            actions.append(
                actionButton("Edit", () => openJobDialog(job), false),
                actionButton(job.enabled ? "Disable" : "Enable",
                    () => updateJob(job.id, { enabled: !job.enabled }), false),
                actionButton("Run next", () => runNow(job, "AUTO"), !job.enabled),
                actionButton("Run FULL", () => runNow(job, "FULL"), !job.enabled),
                ...(Number(job.max_incrementals_per_chain || 0) > 0 ? [
                    actionButton("Run INC", () => runNow(job, "INCREMENTAL"), !job.enabled),
                ] : []),
                actionButton(
                    backupState.expanded ? "Hide backups" : "Show backups",
                    () => toggleJobBackups(job),
                    false,
                ),
            );
            rows.push(tableRow([
                vm ? vm.name : job.vm_id, job.name,
                badge(job.enabled ? "Enabled" : "Disabled",
                    job.enabled ? "status-success" : "status-neutral"),
                destination ? destination.name : job.storage_destination_id,
                lastRun ? localTimestamp(lastRun.created_at) : "Never",
                lastRun ? statusNode(statusLabel(lastRun), statusClass(lastRun), lastRun.progress) : "—",
                "—", job.next_run_at ? localTimestamp(job.next_run_at) : "Manual",
                actions,
            ]));
            if (backupState.expanded)
                rows.push(renderJobBackupsRow(job));
        }
        replaceRows("jobs", rows, 9, "No backup jobs configured");
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



    function renderMutationControl(model) {
        const enabled = Boolean(model && model.status && model.status.libvirt_mutation_enabled);
        const mutation = document.getElementById("mutation-state");
        mutation.textContent = enabled ? "Mutation enabled" : "Mutation disabled";
        mutation.className = `badge ${enabled ? "status-warning" : "status-neutral"}`;

        const button = document.getElementById("mutation-toggle");
        if (!button)
            return;
        const administrative = typeof api.adminAllowed === "function" && api.adminAllowed();
        button.textContent = mutationToggleBusy ? "Applying…" : (enabled ? "Disable" : "Enable");
        button.disabled = mutationToggleBusy || !administrative;
        button.title = administrative ?
            (enabled ? "Disable libvirt mutation and restart vmbackupd" : "Enable libvirt mutation and restart vmbackupd") :
            "Cockpit Administrative access is required";
    }


    async function toggleMutation() {
        if (!currentModel || mutationToggleBusy)
            return;
        if (typeof api.adminAllowed !== "function" || !api.adminAllowed()) {
            setNotice("Administrative access is required to change Mutation.", "error");
            renderMutationControl(currentModel);
            return;
        }
        const enabled = !Boolean(currentModel.status && currentModel.status.libvirt_mutation_enabled);
        mutationToggleBusy = true;
        renderMutationControl(currentModel);
        setNotice(`${enabled ? "Enabling" : "Disabling"} Mutation and restarting vmbackupd…`, "loading");
        try {
            await api.setMutation(enabled);
            await new Promise(resolve => window.setTimeout(resolve, 300));
            await refresh();
            setNotice(`Mutation ${enabled ? "enabled" : "disabled"}.`, "success");
        } catch (error) {
            setNotice(failureMessage(error), "error");
        } finally {
            mutationToggleBusy = false;
            if (currentModel)
                renderMutationControl(currentModel);
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
        renderMutationControl(model);
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


    function formatBytes(value) {
        const bytes = Number(value);
        if (!Number.isFinite(bytes) || bytes < 0)
            return "unknown";
        const gib = bytes / (1024 ** 3);
        return `${gib.toFixed(gib >= 10 ? 1 : 2)} GiB`;
    }


    function runError(run, model) {
        const raw = run.error || run.failure_reason || "";
        const match = raw.match(
            /INSUFFICIENT_STORAGE_CAPACITY:\s*free=(\d+),\s*required=(\d+),\s*reserve=(\d+)/
        );
        if (!match)
            return raw || "—";

        const free = Number(match[1]);
        const required = Number(match[2]);
        const reserve = Number(match[3]);
        const totalRequired = required + reserve;
        const missing = Math.max(0, totalRequired - free);
        const destination = (model.storage || []).find(
            item => item.id === run.storage_destination_id
        );
        const name = destination ? destination.name : run.storage_destination_id;
        return [
            "Insufficient storage capacity",
            `Free: ${formatBytes(free)}`,
            `Estimated backup: ${formatBytes(required)}`,
            `Required reserve: ${formatBytes(reserve)}`,
            `Required total: ${formatBytes(totalRequired)}`,
            `Missing: ${formatBytes(missing)}`,
            `Destination: ${name || "unknown"}`,
        ].join("\n");
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
                statusNode(statusLabel(run), statusClass(run), run.progress),
                [runDuration(run, model.now), "nowrap"],
                [runError(run, model), "error-cell"],
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



    document.getElementById("recent-run-prev").addEventListener(
        "click", () => {
            const page = currentModel && currentModel.runPage || {};
            const limit = Number(page.limit || RECENT_RUN_LIMIT);
            const offset = Number(page.offset || 0);
            void runPageCallback({
                offset: Math.max(0, offset - limit),
                result: recentRunFilter,
            });
        }
    );
    document.getElementById("recent-run-next").addEventListener(
        "click", () => {
            const page = currentModel && currentModel.runPage || {};
            const limit = Number(page.limit || RECENT_RUN_LIMIT);
            const offset = Number(page.offset || 0);
            const total = Number(page.total || 0);
            if (offset + limit < total)
                void runPageCallback({
                    offset: offset + limit,
                    result: recentRunFilter,
                });
        }
    );
    document.getElementById("recent-run-filter").addEventListener(
        "change", event => {
            recentRunFilter = event.target.value || "ALL";
            void runPageCallback({ offset: 0, result: recentRunFilter });
        }
    );

    document.getElementById("add-storage").addEventListener(
        "click", () => openStorageDialog()
    );
    document.getElementById("add-job").addEventListener(
        "click", () => openJobDialog()
    );
    document.getElementById("job-cancel").addEventListener(
        "click", () => document.getElementById("job-dialog").close()
    );
    document.getElementById("job-form").addEventListener("submit", saveJob);
    document.getElementById("job-schedule").addEventListener(
        "change", updateScheduleFields
    );
    document.getElementById("job-incremental-frequency").addEventListener(
        "change", updateIncrementalFrequencyFields
    );
    document.getElementById("job-storage").addEventListener(
        "change", () => renderJobReplicaOptions()
    );
    document.getElementById("storage-cancel").addEventListener(
        "click", () => storageDialog.close()
    );
    document.getElementById("storage-test-candidate").addEventListener(
        "click", testStorageCandidate
    );
    storageDeleteButton.addEventListener("click", deleteStorageDestination);
    document.getElementById("storage-type").addEventListener(
        "change", updateStorageTransportFields
    );
    storageForm.addEventListener("submit", saveStorage);

    document.getElementById("client-identity-open").addEventListener(
        "click", openClientIdentity
    );
    document.getElementById("client-identity-generate").addEventListener(
        "click", generateClientIdentity
    );
    document.getElementById("client-identity-rotate").addEventListener(
        "click", rotateClientIdentity
    );
    document.getElementById("client-identity-close").addEventListener(
        "click", () => clientIdentityDialog.close()
    );

    document.getElementById("receiver-open").addEventListener(
        "click", openReceiverSetup
    );
    document.getElementById("receiver-refresh").addEventListener(
        "click", refreshReceiverSetup
    );
    document.getElementById("receiver-repair").addEventListener(
        "click", repairReceiver
    );
    document.getElementById("receiver-source-add").addEventListener(
        "click", addReceiverSource
    );
    document.getElementById("receiver-close").addEventListener(
        "click", () => receiverDialog.close()
    );

    document.getElementById("ssh-identity-generate").addEventListener(
        "click", generateSSHIdentity
    );
    document.getElementById("ssh-identity-rotate").addEventListener(
        "click", rotateSSHIdentity
    );
    document.getElementById("ssh-hostkey-add").addEventListener(
        "click", addSSHHostKey
    );
    document.getElementById("ssh-hostkey-revoke").addEventListener(
        "click", revokeSSHHostKey
    );
    document.getElementById("ssh-close").addEventListener(
        "click", () => sshDialog.close()
    );

    Object.assign(window.VmbackupViews, {
        openStorageDialog,
        renderStorage,
        testStoredDestination,
        failureMessage,
        openClientIdentity,
        openReceiverSetup,
        refreshReceiverSetup,
        fetchStorageSSHHostKey,
        openJobDialog,
        renderJobs,
        registerVM,
        runNow,
        openReceivedRestore,
    });


    const receivedRestoreForm = document.getElementById("received-restore-form");
    if (receivedRestoreForm)
        receivedRestoreForm.addEventListener("submit", submitReceivedRestore);
    const receivedRestoreCancel = document.getElementById("received-restore-cancel");
    if (receivedRestoreCancel)
        receivedRestoreCancel.addEventListener("click", () => document.getElementById("received-restore-dialog").close());

    const mutationToggle = document.getElementById("mutation-toggle");
    if (mutationToggle)
        mutationToggle.addEventListener("click", () => { void toggleMutation(); });
    if (typeof api.onAdminChanged === "function") {
        api.onAdminChanged(() => {
            if (currentModel)
                renderMutationControl(currentModel);
        });
    }

})();

from test_cockpit_storage_frontend import run_node


def test_vm_register_job_save_and_run_handlers_are_reachable():
    run_node(r"""
const calls = [];
let refreshCount = 0;
request = async (method, params) => {
    calls.push([method, params]);
    if (method === "vm.register") return {
        id: "vm-1", external_id: "win10", name: "win10",
    };
    if (method === "job.create") return { id: "job-1", ...params };
    if (method === "backup.run") return { run_id: "run-1", state: "SCHEDULED" };
    if (method === "receiver.key.list") return [];
    throw new Error(`unexpected method ${method}`);
};
(async () => {
    const views = context.window.VmbackupViews;
    views.configure({ refresh: async () => { refreshCount += 1; } });
    const model = {
        status: { node_name: "maker", libvirt_mutation_enabled: true },
        discoveredVms: [{
            external_id: "win10", name: "win10", uuid: "uuid", state: "shut off",
        }],
        registeredVms: [],
        storage: [
            { id: "local", name: "local-root", storage_type: "LOCAL", is_default: true },
            { id: "ssh", name: "ssh-server-kiev-netasist", storage_type: "SSH",
              remote_storage_id: "c097d776-eb93-4d93-9f33-0daa5ac05d08" },
        ],
        jobs: [{ id: "job-1", vm_id: "vm-1", name: "existing",
                 storage_destination_id: "ssh", enabled: true }],
        runs: [], runPage: { total: 0, limit: 5, offset: 0 }, recovery: [],
        vmById: new Map([["vm-1", { id: "vm-1", name: "win10" }]]),
        jobById: new Map(), now: new Date(),
    };
    views.renderModel(model);

    const register = buttons.find(button => button.textContent === "Register");
    if (!register) throw new Error("VM registration action missing");
    await register.listeners.click();
    if (!calls.some(([method]) => method === "vm.register"))
        throw new Error("VM registration API call missing");
    if (refreshCount !== 1)
        throw new Error("registration did not refresh the model");

    model.registeredVms = [{ id: "vm-1", external_id: "win10", name: "win10" }];
    views.renderModel(model);

    views.openJobDialog();
    if (!nodes.get("job-dialog").open) throw new Error("job dialog did not open");
    if (nodes.get("job-vm").value !== "vm-1")
        throw new Error("registered VM missing from job dialog");
    if (nodes.get("job-storage").value !== "local")
        throw new Error("local primary was not selected");
    const replicaLabels = nodes.get("job-replicas").children;
    const sshReplica = replicaLabels.find(label => label.children[0].value === "ssh");
    if (!sshReplica) throw new Error("SSH replica destination missing");
    if (replicaLabels.some(label => label.children[0].value === "local"))
        throw new Error("primary destination exposed as its own replica");
    sshReplica.children[0].checked = true;
    nodes.get("job-name").value = "local-with-ssh-replica";
    await nodes.get("job-form").listeners.submit({ preventDefault() {} });
    const create = calls.find(([method]) => method === "job.create");
    if (!create || create[1].storage_destination_id !== "local" ||
        JSON.stringify(create[1].replica_destination_ids) !== JSON.stringify(["ssh"]))
        throw new Error(`job destination identity lost: ${JSON.stringify(create)}`);

    await views.runNow(model.jobs[0]);
    if (!calls.some(([method, params]) =>
        method === "backup.run" && params.job_id === "job-1"))
        throw new Error("manual run handler missing");
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_job_edit_preserves_destination_selection():
    run_node(r"""
const views = context.window.VmbackupViews;
views.configure({ refresh: async () => {} });
views.renderModel({
    status: { libvirt_mutation_enabled: true },
    registeredVms: [{ id: "vm-1", name: "win10" }],
    storage: [
        { id: "local", name: "local-root", storage_type: "LOCAL" },
        { id: "ssh", name: "ssh-server-kiev-netasist", storage_type: "SSH" },
    ],
    discoveredVms: [], jobs: [], runs: [], recovery: [],
    runPage: { total: 0, limit: 5, offset: 0 },
    vmById: new Map([["vm-1", { name: "win10" }]]), jobById: new Map(),
    now: new Date(),
});
views.openJobDialog({
    id: "job-1", vm_id: "vm-1", name: "ssh-job", enabled: true,
    storage_destination_id: "ssh", restore_points_to_retain: 7,
    full_chains_to_retain: 2, minimum_full_chains: 1,
    space_reclaim_mode: "SAFE", next_run_at: null,
    replica_destination_ids: ["local"],
});
if (nodes.get("job-storage").value !== "ssh")
    throw new Error("edit did not preserve destination ID");
const replicaLabels = nodes.get("job-replicas").children;
if (replicaLabels.some(label => label.children[0].value === "ssh"))
    throw new Error("SSH primary exposed as its own replica");
const localReplica = replicaLabels.find(label => label.children[0].value === "local");
if (!localReplica || !localReplica.children[0].checked)
    throw new Error("edit did not restore checked replica");
""")


def test_recent_run_paginator_and_capacity_error_are_wired():
    run_node(r"""
(async () => {
const pageCalls = [];
const views = context.window.VmbackupViews;
views.configure({
    refresh: async () => {},
    changeRunPage: async params => { pageCalls.push(params); },
});
const model = {
    status: {}, discoveredVms: [], registeredVms: [],
    storage: [{ id: "local", name: "dir-test", storage_type: "LOCAL" }],
    jobs: [{ id: "job", vm_id: "vm", name: "job" }],
    runs: [{
        id: "run", job_id: "job", storage_destination_id: "local",
        state: "FAILED", created_at: "2026-08-23T12:00:00+00:00",
        error: "LibvirtExecutionSafetyError: INSUFFICIENT_STORAGE_CAPACITY: free=66821279744, required=59909534516, reserve=13181206118",
    }],
    runPage: { total: 11, limit: 5, offset: 0 }, recovery: [],
    vmById: new Map([["vm", { id: "vm", name: "win10" }]]),
    jobById: new Map([["job", { id: "job", vm_id: "vm", name: "job" }]]),
    now: new Date(),
};
views.renderModel(model);
const errorCell = nodes.get("recent-runs").children[0].children[6];
if (!errorCell.textContent.includes("Insufficient storage capacity") ||
    !errorCell.textContent.includes("Destination: dir-test"))
    throw new Error(`capacity error was not humanized: ${errorCell.textContent}`);
await nodes.get("recent-run-next").listeners.click();
if (pageCalls.length !== 1 || pageCalls[0].offset !== 5)
    throw new Error(`next pagination not wired: ${JSON.stringify(pageCalls)}`);
model.runPage = { total: 11, limit: 5, offset: 5 };
views.renderModel(model);
await nodes.get("recent-run-prev").listeners.click();
if (pageCalls.length !== 2 || pageCalls[1].offset !== 0)
    throw new Error(`previous pagination not wired: ${JSON.stringify(pageCalls)}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_job_schedule_mode_persists_in_frontend_payload_and_edit():
    run_node(r"""
(async () => {
const calls = [];
request = async (method, params) => { calls.push([method, params]); return {}; };
const views = context.window.VmbackupViews;
views.configure({ refresh: async () => {} });
views.renderModel({
    status: { libvirt_mutation_enabled: true },
    registeredVms: [{ id: "vm-1", name: "win10" }],
    storage: [{ id: "local", name: "local-root", storage_type: "LOCAL" }],
    discoveredVms: [], jobs: [], runs: [], recovery: [],
    runPage: { total: 0, limit: 5, offset: 0 },
    vmById: new Map([["vm-1", { id: "vm-1", name: "win10" }]]),
    jobById: new Map(), now: new Date(),
});
views.openJobDialog();
nodes.get("job-name").value = "scheduled";
nodes.get("job-schedule").value = "interval";
await nodes.get("job-schedule").listeners.change();
nodes.get("job-interval").value = "15";
nodes.get("job-interval-unit").value = "60";
await nodes.get("job-form").listeners.submit({ preventDefault() {} });
let create = calls.find(([method]) => method === "job.create");
if (!create || create[1].schedule_enabled !== true ||
    create[1].schedule_type !== "INTERVAL" || create[1].interval_seconds !== 900)
    throw new Error(`interval schedule payload lost: ${JSON.stringify(create)}`);

views.openJobDialog({
    id: "job-d", vm_id: "vm-1", name: "daily", enabled: true,
    storage_destination_id: "local", restore_points_to_retain: 7,
    full_chains_to_retain: 2, minimum_full_chains: 1,
    space_reclaim_mode: "SAFE", next_run_at: "2026-08-24T05:30:00+00:00",
    schedule_type: "DAILY", interval_seconds: 3600,
    daily_time: "07:30", schedule_timezone: "Europe/Berlin",
    replica_destination_ids: [],
});
if (nodes.get("job-schedule").value !== "daily" ||
    nodes.get("job-daily-time").value !== "07:30" ||
    nodes.get("job-schedule-timezone").value !== "Europe/Berlin")
    throw new Error("daily schedule was not restored in edit dialog");
await nodes.get("job-form").listeners.submit({ preventDefault() {} });
const update = calls.find(([method]) => method === "job.update");
if (!update || update[1].schedule_enabled !== true ||
    update[1].schedule_type !== "DAILY" || update[1].daily_time !== "07:30" ||
    update[1].schedule_timezone !== "Europe/Berlin")
    throw new Error(`daily schedule payload lost: ${JSON.stringify(update)}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_job_backup_spoiler_lists_and_deletes_by_restore_point_id():
    run_node(r"""
(async () => {
const calls = [];
request = async (method, params) => {
    calls.push([method, params]);
    if (method === "restore_point.list") return [{
        id: "rp-1", job_run_id: "run-1", kind: "FULL", status: "AVAILABLE",
        created_at: "2026-08-23T12:00:00+00:00", storage_destination_id: "local",
        storage_name: "dir-test", storage_type: "LOCAL",
        bundle_object_id: "/mnt/dir-test/vm/run-1", size_bytes: 1234, artifact_count: 3,
    }];
    if (method === "restore_point.delete") return { deleted: true };
    if (method === "receiver.key.list") return [];
    throw new Error(`unexpected method ${method}`);
};
const views = context.window.VmbackupViews;
views.configure({ refresh: async () => {} });
const model = {
    status: {}, discoveredVms: [], registeredVms: [],
    storage: [{ id: "local", name: "dir-test", storage_type: "LOCAL" }],
    jobs: [{ id: "job-1", vm_id: "vm-1", name: "job", enabled: true,
             storage_destination_id: "local", next_run_at: null }],
    runs: [], runPage: { total: 0, limit: 5, offset: 0 }, recovery: [],
    vmById: new Map([["vm-1", { id: "vm-1", name: "win10" }]]),
    jobById: new Map(), now: new Date(),
};
views.renderModel(model);
const show = buttons.find(button => button.textContent === "Show backups");
if (!show) throw new Error("Show backups action missing");
await show.listeners.click();
if (!calls.some(([method, params]) => method === "restore_point.list" &&
    params.job_id === "job-1" && params.details === true))
    throw new Error("job backup list API call missing");
const del = buttons.find(button => button.textContent === "Delete");
if (!del) throw new Error("backup Delete action missing");
context.window.confirm = () => true;
await del.listeners.click();
if (!calls.some(([method, params]) => method === "restore_point.delete" &&
    params.id === "rp-1" && params.job_id === "job-1"))
    throw new Error(`delete did not use restore point ID: ${JSON.stringify(calls)}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_mutation_toggle_requires_admin_and_persists_through_api_helper():
    run_node(r"""
(async () => {
    let requested = null;
    let refreshCount = 0;
    context.window.setTimeout = setTimeout;
    context.window.VmbackupApi.adminAllowed = () => true;
    context.window.VmbackupApi.setMutation = async value => {
        requested = value;
        return { libvirt_mutation_enabled: value };
    };
    const views = context.window.VmbackupViews;
    views.configure({ refresh: async () => { refreshCount += 1; } });
    views.renderModel({
        status: { runtime_state: "RUNNING", libvirt_mutation_enabled: false },
        discoveredVms: [], registeredVms: [], storage: [], jobs: [], runs: [],
        runPage: {total:0, limit:5, offset:0}, recovery: [],
        vmById: new Map(), jobById: new Map(), now: new Date(),
        successfulToday: 0, failedToday: 0, active: 0, recoveryRequired: 0,
    });
    const toggle = nodes.get("mutation-toggle");
    if (toggle.disabled || toggle.textContent !== "Enable")
        throw new Error(`admin mutation toggle not enabled: ${toggle.textContent}`);
    await toggle.listeners.click();
    await new Promise(resolve => setTimeout(resolve, 350));
    if (requested !== true) throw new Error("mutation enable was not persisted");
    if (refreshCount !== 1) throw new Error("mutation change did not refresh model");
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

def test_chain_schedule_frontend_payload_and_manual_kind_buttons():
    run_node(r"""
(async () => {
const calls=[];
request=async (method,params)=>{ calls.push([method,params]); if(method==='job.update')return {}; if(method==='backup.run')return {run_id:'r',state:'SCHEDULED'}; return {}; };
const views=context.window.VmbackupViews;
views.configure({refresh:async()=>{}});
const job={id:'job-1',vm_id:'vm-1',name:'chain-job',enabled:true,storage_destination_id:'local',
  max_incrementals_per_chain:6,restore_points_to_retain:7,full_chains_to_retain:2,minimum_full_chains:1,
  space_reclaim_mode:'SAFE',next_run_at:'2026-08-24T00:00:00+00:00',replica_destination_ids:[],
  chain_schedule:{enabled:true,timezone:'Europe/Berlin',full_weekday:6,full_time:'02:00',incremental_times:['02:00','14:00'],
                  next_full_at:'2026-08-30T02:00:00+02:00',next_incremental_at:'2026-08-24T02:00:00+02:00'}};
views.renderModel({status:{libvirt_mutation_enabled:true},registeredVms:[{id:'vm-1',name:'win10'}],discoveredVms:[],
 storage:[{id:'local',name:'dir-test',storage_type:'LOCAL'}],jobs:[job],runs:[],recovery:[],runPage:{total:0,limit:5,offset:0},
 vmById:new Map([['vm-1',{id:'vm-1',name:'win10'}]]),jobById:new Map([['job-1',job]]),now:new Date()});
views.openJobDialog(job);
if(nodes.get('job-schedule').value!=='chain') throw new Error('chain mode not restored');
if(nodes.get('job-full-weekday').value!=='6' || nodes.get('job-full-time').value!=='02:00') throw new Error('FULL calendar not restored');
if(nodes.get('job-incremental-frequency').value!=='2' || nodes.get('job-incremental-time-2').value!=='14:00') throw new Error('incremental times not restored');
await nodes.get('job-form').listeners.submit({preventDefault(){}});
const update=calls.find(([m])=>m==='job.update');
if(!update || update[1].chain_schedule_enabled!==true || update[1].schedule_enabled!==false ||
   update[1].full_weekday!==6 || JSON.stringify(update[1].incremental_times)!==JSON.stringify(['02:00','14:00']))
  throw new Error(`chain schedule payload lost: ${JSON.stringify(update)}`);
await views.runNow(job,'FULL');
await views.runNow(job,'INCREMENTAL');
const kinds=calls.filter(([m])=>m==='backup.run').map(([,p])=>p.kind);
if(JSON.stringify(kinds)!==JSON.stringify(['FULL','INCREMENTAL'])) throw new Error(`manual kinds lost: ${JSON.stringify(kinds)}`);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_job_backup_spoiler_groups_incrementals_under_full_chain():
    run_node(r"""
(async () => {
request = async (method, params) => {
    if (method === "restore_point.list") return [
        {
            id: "inc-2", job_run_id: "run-inc-2", kind: "INCREMENTAL", status: "AVAILABLE",
            created_at: "2026-08-23T14:00:00+00:00", storage_destination_id: "local",
            storage_name: "dir-test", bundle_object_id: "/backup/inc-2", size_bytes: 1024,
            chain_id: "chain-a", sequence: 2, parent_restore_point_id: "inc-1", replicas: [],
        },
        {
            id: "full", job_run_id: "run-full", kind: "FULL", status: "AVAILABLE",
            created_at: "2026-08-23T12:00:00+00:00", storage_destination_id: "local",
            storage_name: "dir-test", bundle_object_id: "/backup/full", size_bytes: 4096,
            chain_id: "chain-a", sequence: 0, parent_restore_point_id: null, replicas: [],
        },
        {
            id: "inc-1", job_run_id: "run-inc-1", kind: "INCREMENTAL", status: "AVAILABLE",
            created_at: "2026-08-23T13:00:00+00:00", storage_destination_id: "local",
            storage_name: "dir-test", bundle_object_id: "/backup/inc-1", size_bytes: 512,
            chain_id: "chain-a", sequence: 1, parent_restore_point_id: "full", replicas: [],
        },
    ];
    if (method === "receiver.key.list") return [];
    throw new Error(`unexpected method ${method}`);
};
const views = context.window.VmbackupViews;
views.configure({ refresh: async () => {} });
views.renderModel({
    status: {}, discoveredVms: [], registeredVms: [],
    storage: [{ id: "local", name: "dir-test", storage_type: "LOCAL" }],
    jobs: [{ id: "job-1", vm_id: "vm-1", name: "job", enabled: true,
             storage_destination_id: "local", next_run_at: null }],
    runs: [], runPage: { total: 0, limit: 5, offset: 0 }, recovery: [], received: [],
    vmById: new Map([["vm-1", { id: "vm-1", name: "win10" }]]),
    jobById: new Map(), now: new Date(),
});
const show = buttons.find(button => button.textContent === "Show backups");
await show.listeners.click();
function allText(node) {
    if (!node) return "";
    return String(node.textContent || "") + " " + (node.children || []).map(allText).join(" ");
}
const text = allText(nodes.get("jobs"));
if (!text.includes("Chain chain-a") || !text.includes("Base of chain") ||
    !text.includes("INC #1") || !text.includes("← FULL") ||
    !text.includes("INC #2") || !text.includes("← INC #1"))
    throw new Error(`chain hierarchy missing: ${text}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_job_backup_spoiler_does_not_promote_orphan_incremental_to_full():
    run_node(r"""
(async () => {
request = async (method, params) => {
    if (method === "restore_point.list") return [{
        id: "inc-orphan", job_run_id: "run-inc", kind: "INCREMENTAL", status: "AVAILABLE",
        created_at: "2026-08-23T17:50:04+00:00", storage_destination_id: "local",
        storage_name: "dir-test", bundle_object_id: "/backup/inc", size_bytes: 22334668,
        chain_id: "chain-old", sequence: null,
        parent_restore_point_id: "deleted-full", replicas: [],
    }];
    if (method === "receiver.key.list") return [];
    throw new Error(`unexpected method ${method}`);
};
const views = context.window.VmbackupViews;
views.configure({ refresh: async () => {} });
views.renderModel({
    status: {}, discoveredVms: [], registeredVms: [],
    storage: [{ id: "local", name: "dir-test", storage_type: "LOCAL" }],
    jobs: [{ id: "job-1", vm_id: "vm-1", name: "job", enabled: true,
             storage_destination_id: "local", next_run_at: null }],
    runs: [], runPage: { total: 0, limit: 5, offset: 0 }, recovery: [], received: [],
    vmById: new Map([["vm-1", { id: "vm-1", name: "win10" }]]),
    jobById: new Map(), now: new Date(),
});
const show = buttons.find(button => button.textContent === "Show backups");
await show.listeners.click();
function allText(node) {
    if (!node) return "";
    return String(node.textContent || "") + " " + (node.children || []).map(allText).join(" ");
}
const text = allText(nodes.get("jobs"));
if (!text.includes("base FULL missing") || !text.includes("INC #1") ||
    !text.includes("missing parent deleted-"))
    throw new Error(`orphan chain not rendered honestly: ${text}`);
if (text.includes("FULL — Base of chain"))
    throw new Error(`orphan incremental was promoted to FULL: ${text}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_backup_chain_view_prefers_parent_links_over_legacy_chain_id():
    source = open("cockpit/vmbackupd/views.js", encoding="utf-8").read()
    assert "function effectiveChainId(point)" in source
    assert "current.parent_restore_point_id" in source
    assert "return current.chain_id || current.id" in source


def test_running_vm_uses_success_badge():
    from pathlib import Path
    javascript = (Path(__file__).parents[1] / "cockpit/vmbackupd/views.js").read_text()
    assert 'String(vm.state || "").toLowerCase() === "running"' in javascript
    assert '"status-success" : "status-neutral"' in javascript


def test_received_backup_restore_dialog_submits_target_folder_and_start_flag():
    run_node(r"""
(async () => {
const calls = [];
request = async (method, params) => {
    calls.push([method, params]);
    if (method === "received.restore.create") return { id: "restore-1", state: "PLANNED" };
    throw new Error(`unexpected method ${method}`);
};
context.window.VmbackupApi.adminAllowed = () => true;
const views = context.window.VmbackupViews;
views.configure({ refresh: async () => {} });
views.renderModel({
    status: { libvirt_mutation_enabled: true },
    received: [{
        id: "received-1", vm_name: "win10", kind: "INCREMENTAL", status: "AVAILABLE",
        storage_destination_id: "stor", storage_name: "STOR_HDD",
        created_at: "2026-08-24T07:00:00+00:00", bundle_object_id: "/STOR_HDD/vmbackupd/vms/x",
        origin: { received_via: "SSH_REPLICA" },
    }],
    restores: [], discoveredVms: [], registeredVms: [], storage: [{
        id: "restore-storage", name: "NVME_1", storage_type: "LOCAL", type: "Local",
        backup_data_root: "/NVME_1/vms", is_default: true,
    }], jobs: [], runs: [], recovery: [],
    runPage: { total: 0, limit: 5, offset: 0 }, vmById: new Map(), jobById: new Map(), now: new Date(),
});
const restore = buttons.find(button => button.textContent === "Restore");
if (!restore) throw new Error("received Restore action missing");
restore.listeners.click();
nodes.get("received-restore-name").value = "win10-recovered";
nodes.get("received-restore-storage").value = "restore-storage";
nodes.get("received-restore-subfolder").value = "restored/win10-recovered";
nodes.get("received-restore-start").checked = true;
await nodes.get("received-restore-form").listeners.submit({ preventDefault() {} });
const call = calls.find(([method]) => method === "received.restore.create");
if (!call) throw new Error("received.restore.create missing");
if (call[1].restore_point_id !== "received-1" || call[1].target_vm_name !== "win10-recovered" ||
    call[1].target_destination_id !== "restore-storage" || call[1].target_subfolder !== "restored/win10-recovered" ||
    call[1].start_after_restore !== true)
    throw new Error(`restore payload mismatch: ${JSON.stringify(call[1])}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_active_states_use_blue_live_progress_indicator():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    source = (root / "cockpit" / "vmbackupd" / "views.js").read_text()
    css = (root / "cockpit" / "vmbackupd" / "vmbackupd.css").read_text()
    assert '"BACKING_UP"' in source
    assert '"TRANSFERRING"' in source
    assert 'active-status-fill' in source
    assert 'bytes_processed: replica.bytes_processed' in source
    assert '.active-status' in css
    assert '@keyframes vmbackupd-progress-slide' in css


def test_expanded_backup_list_live_refreshes_without_closing_spoiler():
    run_node(r"""
(async () => {
let generation = 0;
request = async (method, params) => {
    if (method === "restore_point.list") {
        generation += 1;
        return [{
            id: `rp-${generation}`, job_run_id: `run-${generation}`, kind: "FULL",
            status: "AVAILABLE", created_at: "2026-08-24T12:00:00+00:00",
            storage_destination_id: "local", storage_name: "dir-test",
            bundle_object_id: `/backup/rp-${generation}`, size_bytes: generation,
            chain_id: `chain-${generation}`, sequence: 0,
            parent_restore_point_id: null, replicas: [],
        }];
    }
    if (method === "receiver.key.list") return [];
    throw new Error(`unexpected method ${method}`);
};
const views = context.window.VmbackupViews;
views.configure({ refresh: async () => {} });
const model = {
    status: {}, discoveredVms: [], registeredVms: [],
    storage: [{ id: "local", name: "dir-test", storage_type: "LOCAL" }],
    jobs: [{ id: "job-1", vm_id: "vm-1", name: "job", enabled: true,
             storage_destination_id: "local", next_run_at: null }],
    runs: [], runPage: { total: 0, limit: 5, offset: 0 }, recovery: [], received: [],
    vmById: new Map([["vm-1", { id: "vm-1", name: "win10" }]]),
    jobById: new Map(), now: new Date(),
};
views.renderModel(model);
const show = buttons.find(button => button.textContent === "Show backups");
await show.listeners.click();
if (generation !== 1) throw new Error(`initial load count ${generation}`);
await views.refreshExpandedBackups();
if (generation !== 2) throw new Error(`live refresh count ${generation}`);
function allText(node) {
    if (!node) return "";
    return String(node.textContent || "") + " " + (node.children || []).map(allText).join(" ");
}
const rendered = allText(nodes.get("jobs"));
if (!rendered.includes("chain-2")) throw new Error(`new backup snapshot missing: ${rendered}`);
if (!buttons.some(button => button.textContent === "Hide backups"))
    throw new Error("expanded spoiler closed during live refresh");
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_seeded_full_replica_renders_network_savings():
    source = open("cockpit/vmbackupd/views.js", encoding="utf-8").read()
    assert 'replica.transport_mode === "SEEDED_FULL"' in source
    assert "Seeded FULL · transfer" in source
    assert "source_payload_bytes" in source

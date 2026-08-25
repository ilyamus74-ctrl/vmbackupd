import json
import io
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmbackupd.application import ApplicationError, VmbackupApplication
from vmbackupd.received_catalog_v2 import ReceivedCatalogV2
from vmbackupd.receiver_reclaim_delete import (
    ReceiverReclaimDeleteError,
    delete_published_replica,
)
from vmbackupd.receiver_resolver import helper_main
from vmbackupd.repository_v2 import RepositoryV2

NODE="11111111-1111-4111-8111-111111111111"; STORAGE="22222222-2222-4222-8222-222222222222"
POINT="33333333-3333-4333-8333-333333333333"; VM="44444444-4444-4444-8444-444444444444"
JOB="55555555-5555-4555-8555-555555555555"; RUN="66666666-6666-4666-8666-666666666666"
CHAIN="77777777-7777-4777-8777-777777777777"; TRANSFER="88888888-8888-4888-8888-888888888888"
INC1="99999999-9999-4999-8999-999999999999"
INC2="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

def setup_repo(tmp_path):
    repo=RepositoryV2.open(tmp_path/"state.db"); now=datetime.now(timezone.utc).isoformat(); root=tmp_path/"backup"; root.mkdir()
    repo.connection.execute("INSERT INTO nodes VALUES(?,?,?)",(NODE,"receiver",now))
    repo.connection.execute("INSERT INTO storage_destinations VALUES(?,?,?,?,?,?)",(STORAGE,NODE,"STOR_HDD","LOCAL",json.dumps({"backup_data_root":str(root),"backup_data_mode":"0750","minimum_free_bytes":0,"minimum_free_percent":0,"is_default":True}),now)); repo.connection.commit(); return repo,root

def fixture(root:Path):
    object_id=f"vms/{VM}/2026/08/bundle"; bundle=root/object_id; (bundle/"metadata").mkdir(parents=True); (bundle/"disks").mkdir()
    manifest={"format_version":1,"run_id":RUN,"job_id":JOB,"vm_id":VM,"storage_destination_id":"source-storage","backup_kind":"FULL","chain_id":CHAIN,"sequence":0,"parent_restore_point_id":None,"libvirt_checkpoint_name":"cp","disks":[]}
    restore={**manifest,"id":POINT,"job_run_id":RUN,"status":"AVAILABLE","created_at":"2026-08-23T16:00:40+00:00"}
    (bundle/"metadata"/"manifest.json").write_text(json.dumps(manifest)); (bundle/"metadata"/"restore-point.json").write_text(json.dumps(restore)); (bundle/"metadata"/"domain.xml").write_text("<domain><name>mail-from-a</name></domain>")
    markers=root/".vmbackupd-replica-state"/"published"; markers.mkdir(parents=True)
    marker={"version":1,"state":"PUBLISHED","transfer_id":TRANSFER,"storage_id":STORAGE,"restore_point_id":POINT,"vm_id":VM,"job_run_id":RUN,"chain_id":CHAIN,"kind":"FULL","sequence":0,"parent_restore_point_id":None,"bundle_object_id":object_id}
    path=markers/f"{POINT}.json"; path.write_text(json.dumps(marker)); return bundle,path

def chain_fixture(root: Path):
    specs = [
        (POINT, "FULL", 0, None, RUN),
        (INC1, "INCREMENTAL", 1, POINT, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        (INC2, "INCREMENTAL", 2, INC1, "cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
    ]
    result = {}
    markers = root / ".vmbackupd-replica-state" / "published"
    markers.mkdir(parents=True, exist_ok=True)
    for source_id, kind, sequence, parent, run_id in specs:
        object_id = f"vms/{VM}/2026/08/{source_id}"
        bundle = root / object_id
        (bundle / "metadata").mkdir(parents=True)
        (bundle / "disks").mkdir()
        manifest = {
            "format_version": 1, "run_id": run_id, "job_id": JOB,
            "vm_id": VM, "backup_kind": kind, "chain_id": CHAIN,
            "sequence": sequence, "parent_restore_point_id": parent,
            "disks": [],
        }
        restore = {
            **manifest, "id": source_id, "job_run_id": run_id,
            "status": "AVAILABLE", "created_at": "2026-08-23T16:00:40+00:00",
        }
        (bundle / "metadata" / "manifest.json").write_text(json.dumps(manifest))
        (bundle / "metadata" / "restore-point.json").write_text(json.dumps(restore))
        (bundle / "metadata" / "domain.xml").write_text("<domain><name>mail-from-a</name></domain>")
        marker = {
            "version": 1, "state": "PUBLISHED", "transfer_id": TRANSFER,
            "storage_id": STORAGE, "restore_point_id": source_id,
            "vm_id": VM, "job_run_id": run_id, "chain_id": CHAIN,
            "kind": kind, "sequence": sequence,
            "parent_restore_point_id": parent, "bundle_object_id": object_id,
        }
        marker_path = markers / f"{source_id}.json"
        marker_path.write_text(json.dumps(marker))
        result[source_id] = (bundle, marker_path, object_id)
    return result

def application(repo, catalog):
    app = object.__new__(VmbackupApplication)
    app.repository = repo
    app.node = SimpleNamespace(id=NODE)
    app.received_catalog = catalog
    return app

def local_delete_client(root, calls):
    class Client:
        def delete(self, storage_id, restore_point_id, bundle_object_id):
            calls.append((storage_id, restore_point_id, bundle_object_id))
            return delete_published_replica(
                {"storage_id": STORAGE, "backup_data_root": str(root)},
                restore_point_id,
                bundle_object_id,
            )
    return Client

def test_reconcile_imports_received_replica(tmp_path):
    repo,root=setup_repo(tmp_path); bundle,_=fixture(root); values=ReceivedCatalogV2(repo,NODE).reconcile(); assert len(values)==1
    v=values[0]; assert v["vm_name"]=="mail-from-a"; assert v["kind"]=="FULL"; assert v["status"]=="AVAILABLE"; assert v["source_restore_point_id"]==POINT; assert v["bundle_object_id"]==str(bundle); assert v["origin"]["received_via"]=="SSH_REPLICA"

def test_reconcile_is_idempotent(tmp_path):
    repo,root=setup_repo(tmp_path); fixture(root); c=ReceivedCatalogV2(repo,NODE); c.reconcile(); c.reconcile(); assert len(repo.list_received_restore_points(NODE))==1

def test_missing_published_marker_marks_catalog_missing(tmp_path):
    repo,root=setup_repo(tmp_path); _,marker=fixture(root); c=ReceivedCatalogV2(repo,NODE); c.reconcile(); marker.unlink(); c.reconcile(); assert repo.list_received_restore_points(NODE)[0]["status"]=="MISSING"

def test_received_delete_full_physically_removes_descendants_and_refreshes(monkeypatch, tmp_path):
    repo, root = setup_repo(tmp_path)
    published = chain_fixture(root)
    catalog = ReceivedCatalogV2(repo, NODE)
    values = catalog.reconcile()
    local_by_source = {value["source_restore_point_id"]: value["id"] for value in values}
    calls = []
    monkeypatch.setattr(
        "vmbackupd.receiver_reclaim_delete.ReceiverReclaimDeleteClient",
        local_delete_client(root, calls),
    )

    result = application(repo, catalog).dispatch(
        "received.delete", {"restore_point_id": local_by_source[POINT]}
    )

    assert [call[1] for call in calls] == [INC2, INC1, POINT]
    assert set(result["deleted_restore_point_ids"]) == set(local_by_source.values())
    for bundle, marker, _ in published.values():
        assert not bundle.exists()
        assert not marker.exists()
    assert application(repo, catalog).dispatch("received.list", {}) == []
    assert {value["status"] for value in repo.list_received_restore_points(NODE)} == {"MISSING"}

def test_received_delete_middle_incremental_keeps_full(monkeypatch, tmp_path):
    repo, root = setup_repo(tmp_path)
    published = chain_fixture(root)
    catalog = ReceivedCatalogV2(repo, NODE)
    values = catalog.reconcile()
    local_by_source = {value["source_restore_point_id"]: value["id"] for value in values}
    calls = []
    monkeypatch.setattr(
        "vmbackupd.receiver_reclaim_delete.ReceiverReclaimDeleteClient",
        local_delete_client(root, calls),
    )

    application(repo, catalog).received_delete(local_by_source[INC1])

    assert [call[1] for call in calls] == [INC2, INC1]
    assert published[POINT][0].is_dir() and published[POINT][1].is_file()
    for source_id in (INC1, INC2):
        assert not published[source_id][0].exists()
        assert not published[source_id][1].exists()
    remaining = application(repo, catalog).received_list()
    assert [value["source_restore_point_id"] for value in remaining] == [POINT]

def test_received_delete_full_without_children_physically_removes_bundle_and_marker(monkeypatch, tmp_path):
    repo, root = setup_repo(tmp_path)
    bundle, marker = fixture(root)
    catalog = ReceivedCatalogV2(repo, NODE)
    point = catalog.reconcile()[0]
    calls = []
    monkeypatch.setattr(
        "vmbackupd.receiver_reclaim_delete.ReceiverReclaimDeleteClient",
        local_delete_client(root, calls),
    )
    application(repo, catalog).received_delete(point["id"])
    assert [call[1] for call in calls] == [POINT]
    assert not bundle.exists() and not marker.exists()
    assert application(repo, catalog).received_list() == []

def test_received_delete_missing_bundle_removes_marker_safely(monkeypatch, tmp_path):
    repo, root = setup_repo(tmp_path)
    bundle, marker = fixture(root)
    catalog = ReceivedCatalogV2(repo, NODE)
    point = catalog.reconcile()[0]
    # Simulate a bundle lost after catalog import while retaining its trusted marker.
    import shutil
    shutil.rmtree(bundle)
    calls = []
    monkeypatch.setattr(
        "vmbackupd.receiver_reclaim_delete.ReceiverReclaimDeleteClient",
        local_delete_client(root, calls),
    )
    application(repo, catalog).received_delete(point["id"])
    assert not marker.exists()
    assert application(repo, catalog).received_list() == []
    repeated = delete_published_replica(
        {"storage_id": STORAGE, "backup_data_root": str(root)},
        POINT,
        str(bundle.relative_to(root)),
    )
    assert repeated["already_absent"] is True

def test_resolver_reclaim_delete_resolves_registered_root_and_physically_deletes(tmp_path):
    _, root = setup_repo(tmp_path)
    bundle, marker = fixture(root)
    object_id = str(bundle.relative_to(root))
    class Api:
        def request(self, method, params):
            assert method == "storage.list"
            return [{
                "id": STORAGE, "storage_type": "LOCAL",
                "backup_data_root": str(root),
            }]
    request = {
        "version": 1, "operation": "reclaim_delete", "storage_id": STORAGE,
        "restore_point_id": POINT, "bundle_object_id": object_id,
    }
    output = io.BytesIO()
    assert helper_main(
        api_client=Api(), stdin=io.BytesIO((json.dumps(request) + "\n").encode()),
        stdout=output,
    ) == 0
    response = json.loads(output.getvalue())
    assert response["ok"] is True
    assert not bundle.exists() and not marker.exists()

def test_receiver_delete_identity_mismatch_rejects_without_removal(tmp_path):
    _, root = setup_repo(tmp_path)
    published = chain_fixture(root)
    bundle, marker, _ = published[POINT]
    with pytest.raises(ReceiverReclaimDeleteError, match="identity does not match") as caught:
        delete_published_replica(
            {"storage_id": STORAGE, "backup_data_root": str(root)},
            POINT,
            published[INC1][2],
        )
    assert caught.value.code == "RECLAIM_DELETE_STATE_CONFLICT"
    assert bundle.is_dir() and marker.is_file()

def test_received_delete_backend_failure_is_explicit_and_keeps_catalog(monkeypatch, tmp_path):
    repo, root = setup_repo(tmp_path)
    bundle, marker = fixture(root)
    catalog = ReceivedCatalogV2(repo, NODE)
    point = catalog.reconcile()[0]
    class FailingClient:
        def delete(self, storage_id, restore_point_id, bundle_object_id):
            raise ReceiverReclaimDeleteError("RECLAIM_DELETE_FAILED", "filesystem denied delete")
    monkeypatch.setattr(
        "vmbackupd.receiver_reclaim_delete.ReceiverReclaimDeleteClient", FailingClient
    )
    with pytest.raises(ApplicationError, match="filesystem denied delete") as caught:
        application(repo, catalog).dispatch(
            "received.delete", {"restore_point_id": point["id"]}
        )
    assert caught.value.code == "RECLAIM_DELETE_FAILED"
    assert bundle.is_dir() and marker.is_file()
    assert application(repo, catalog).received_list()[0]["id"] == point["id"]

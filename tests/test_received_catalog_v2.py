import json
from datetime import datetime, timezone
from pathlib import Path
from vmbackupd.received_catalog_v2 import ReceivedCatalogV2
from vmbackupd.repository_v2 import RepositoryV2

NODE="11111111-1111-4111-8111-111111111111"; STORAGE="22222222-2222-4222-8222-222222222222"
POINT="33333333-3333-4333-8333-333333333333"; VM="44444444-4444-4444-8444-444444444444"
JOB="55555555-5555-4555-8555-555555555555"; RUN="66666666-6666-4666-8666-666666666666"
CHAIN="77777777-7777-4777-8777-777777777777"; TRANSFER="88888888-8888-4888-8888-888888888888"

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

def test_reconcile_imports_received_replica(tmp_path):
    repo,root=setup_repo(tmp_path); bundle,_=fixture(root); values=ReceivedCatalogV2(repo,NODE).reconcile(); assert len(values)==1
    v=values[0]; assert v["vm_name"]=="mail-from-a"; assert v["kind"]=="FULL"; assert v["status"]=="AVAILABLE"; assert v["source_restore_point_id"]==POINT; assert v["bundle_object_id"]==str(bundle); assert v["origin"]["received_via"]=="SSH_REPLICA"

def test_reconcile_is_idempotent(tmp_path):
    repo,root=setup_repo(tmp_path); fixture(root); c=ReceivedCatalogV2(repo,NODE); c.reconcile(); c.reconcile(); assert len(repo.list_received_restore_points(NODE))==1

def test_missing_published_marker_marks_catalog_missing(tmp_path):
    repo,root=setup_repo(tmp_path); _,marker=fixture(root); c=ReceivedCatalogV2(repo,NODE); c.reconcile(); marker.unlink(); c.reconcile(); assert repo.list_received_restore_points(NODE)[0]["status"]=="MISSING"

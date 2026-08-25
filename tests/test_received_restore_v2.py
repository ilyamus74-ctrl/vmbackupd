import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from vmbackupd.received_restore_v2 import ReceivedRestoreRuntimeV2
from vmbackupd.repository_v2 import RepositoryV2

NODE="11111111-1111-4111-8111-111111111111"
STORAGE="22222222-2222-4222-8222-222222222222"
FULL="33333333-3333-4333-8333-333333333333"
INC="44444444-4444-4444-8444-444444444444"
VM="55555555-5555-4555-8555-555555555555"
JOB="66666666-6666-4666-8666-666666666666"
RUN1="77777777-7777-4777-8777-777777777777"
RUN2="88888888-8888-4888-8888-888888888888"
CHAIN="99999999-9999-4999-8999-999999999999"

class Clock:
    def now(self): return datetime.now(timezone.utc)

class Runner:
    def run(self, argv, timeout=None):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

class ReadDriver:
    def __init__(self): self.names=[]; self.uuids={}
    def list_domain_names(self): return tuple(self.names)
    def domain_uuid(self,name): return self.uuids[name]

class MutationDriver:
    def __init__(self, read): self.read=read; self.started=[]
    def define(self, xml_path):
        import xml.etree.ElementTree as ET
        root=ET.parse(xml_path).getroot(); name=root.findtext("name"); uid=root.findtext("uuid")
        self.read.names.append(name); self.read.uuids[name]=uid
    def start(self,name): self.started.append(name)

class Runtime(ReceivedRestoreRuntimeV2):
    def _materialize_disk(self, chain, target_dev, staging):
        path=staging/"disks"/f"{target_dev}.qcow2"; path.write_bytes(b"restored"); return path


def setup(tmp_path):
    repo=RepositoryV2.open(tmp_path/"state.db"); now=datetime.now(timezone.utc).isoformat(); root=tmp_path/"backup"; root.mkdir()
    repo.connection.execute("INSERT INTO nodes VALUES(?,?,?)",(NODE,"receiver",now))
    repo.connection.execute("INSERT INTO storage_destinations VALUES(?,?,?,?,?,?)",(STORAGE,NODE,"STOR_HDD","LOCAL",json.dumps({"backup_data_root":str(root),"backup_data_mode":"0750","backup_data_gid":os.getgid(),"minimum_free_bytes":0,"minimum_free_percent":0,"is_default":True}),now)); repo.connection.commit()
    return repo,root


def bundle(root:Path, name, source_id, kind, seq, parent):
    path=root/name; (path/"metadata").mkdir(parents=True); (path/"disks").mkdir(); (path/"disks"/"vda.qcow2").write_bytes(b"x")
    manifest={"format_version":1,"run_id":RUN1 if kind=="FULL" else RUN2,"job_id":JOB,"vm_id":VM,"backup_kind":kind,"chain_id":CHAIN,"sequence":seq,"parent_restore_point_id":parent,"disks":[{"target":"vda"}]}
    restore={**manifest,"id":source_id}
    (path/"metadata"/"manifest.json").write_text(json.dumps(manifest)); (path/"metadata"/"restore-point.json").write_text(json.dumps(restore))
    (path/"metadata"/"domain.xml").write_text("<domain><name>source</name><uuid>aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa</uuid><devices><disk device='disk' type='file'><source file='/old.qcow2'/><target dev='vda'/></disk><interface type='network'><source network='default'/></interface></devices></domain>")
    return path


def import_point(repo, root, local_id, source_id, kind, seq, parent_local, run_id, path):
    repo.upsert_received_restore_point(receiver_node_id=NODE,storage_destination_id=STORAGE,
        local_restore_point_id=local_id,source_restore_point_id=source_id,local_vm_id="aaaaaaaa-1111-4111-8111-111111111111",source_vm_id=VM,
        local_job_id="bbbbbbbb-2222-4222-8222-222222222222",source_job_id=JOB,local_run_id=run_id,source_run_id=run_id,
        source_node_id="cccccccc-3333-4333-8333-333333333333",vm_name="source",kind=kind,chain_id=CHAIN,sequence=seq,
        parent_restore_point_id=parent_local,bundle_object_id=str(path),source_bundle_object_id=path.name,
        libvirt_checkpoint_name=None,created_at=datetime.now(timezone.utc).isoformat(),origin={"received_via":"SSH_REPLICA"})


def test_received_incremental_restore_materializes_chain_defines_registers_and_starts(tmp_path):
    repo,root=setup(tmp_path)
    full_path=bundle(root,"full",FULL,"FULL",0,None); inc_path=bundle(root,"inc",INC,"INCREMENTAL",1,FULL)
    import_point(repo,root,"local-full",FULL,"FULL",0,None,"local-run1",full_path)
    import_point(repo,root,"local-inc",INC,"INCREMENTAL",1,"local-full","local-run2",inc_path)
    target=root/"restored-vms"/"restored"
    op=repo.create_received_restore_operation_v2("local-inc",NODE,"restored",str(target),Clock().now(),start_after_restore=True)
    read=ReadDriver(); mutation=MutationDriver(read); runtime=Runtime(repo,NODE,Runner(),read,mutation,Clock(),True)
    result=runtime.advance(op.id)
    assert result.state.value=="SUCCESS"; assert target.joinpath("disks/vda.qcow2").is_file(); assert mutation.started==["restored"]
    assert target.joinpath("disks/vda.qcow2").stat().st_gid == os.getgid()
    assert target.parent.stat().st_gid == os.getgid()
    assert target.parent.stat().st_mode & 0o050 == 0o050
    registered=[v for v in repo.list_vms(NODE) if v.name=="restored"]; assert len(registered)==1
    xml=target.joinpath("metadata/restored-domain.xml").read_text(); assert "/old.qcow2" not in xml; assert str(target/"disks/vda.qcow2") in xml; assert 'state="down"' in xml


def test_received_restore_requires_full_ancestor(tmp_path):
    repo,root=setup(tmp_path); inc_path=bundle(root,"inc",INC,"INCREMENTAL",1,FULL)
    import_point(repo,root,"local-inc",INC,"INCREMENTAL",1,"missing-full","local-run2",inc_path)
    target=root/"restored-vms"/"restored"
    op=repo.create_received_restore_operation_v2("local-inc",NODE,"restored",str(target),Clock().now())
    runtime=Runtime(repo,NODE,Runner(),ReadDriver(),MutationDriver(ReadDriver()),Clock(),True)
    result=runtime.advance(op.id)
    assert result.state.value=="FAILED"; assert "PARENT_MISSING" in (result.error or "") or "parent" in (result.error or "").lower()


def test_received_restore_creates_new_nested_folder_inside_registered_storage(tmp_path):
    repo,root=setup(tmp_path)
    full_path=bundle(root,"full-new-folder",FULL,"FULL",0,None)
    import_point(repo,root,"local-full-new",FULL,"FULL",0,None,"local-run-new",full_path)
    target=root/"new"/"nested"/"vm-restored"
    op=repo.create_received_restore_operation_v2("local-full-new",NODE,"restored-new",str(target),Clock().now())
    read=ReadDriver(); mutation=MutationDriver(read)
    runtime=Runtime(repo,NODE,Runner(),read,mutation,Clock(),True)
    result=runtime.advance(op.id)
    assert result.state.value=="SUCCESS"
    assert target.joinpath("disks/vda.qcow2").is_file()


def test_received_restore_rejects_target_outside_registered_local_storage(tmp_path):
    repo,root=setup(tmp_path)
    full_path=bundle(root,"full-outside",FULL,"FULL",0,None)
    import_point(repo,root,"local-full-outside",FULL,"FULL",0,None,"local-run-outside",full_path)
    target=tmp_path/"outside"/"restored"
    op=repo.create_received_restore_operation_v2("local-full-outside",NODE,"restored-outside",str(target),Clock().now())
    runtime=Runtime(repo,NODE,Runner(),ReadDriver(),MutationDriver(ReadDriver()),Clock(),True)
    result=runtime.advance(op.id)
    assert result.state.value=="FAILED"
    assert "OUTSIDE_LOCAL_STORAGE" in (result.error or "")


def test_received_restore_ignores_missing_ssh_system_staging_destination(tmp_path):
    repo,root=setup(tmp_path)
    missing_ssh_root=tmp_path/"ssh"/"system-staging"
    now=datetime.now(timezone.utc).isoformat()
    repo.connection.execute(
        "INSERT INTO storage_destinations VALUES(?,?,?,?,?,?)",
        (
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", NODE, "__system_ssh_identity__", "SSH",
            json.dumps({
                "backup_data_root":str(missing_ssh_root),
                "backup_data_mode":"0750",
                "backup_data_gid":os.getgid(),
                "minimum_free_bytes":0,
                "minimum_free_percent":0,
                "ssh_host":"localhost",
                "ssh_port":22022,
                "ssh_user":"vmbackupd-transfer",
                "ssh_remote_root":"/srv/vmbackupd",
                "is_default":False,
            }),
            now,
        ),
    )
    repo.connection.commit()
    assert not missing_ssh_root.exists()

    full_path=bundle(root,"full-no-system-staging",FULL,"FULL",0,None)
    import_point(repo,root,"local-full-no-system-staging",FULL,"FULL",0,None,"local-run-no-system-staging",full_path)
    target=root/"restored-vms"/"restored-no-system-staging"
    op=repo.create_received_restore_operation_v2(
        "local-full-no-system-staging",NODE,"restored-no-system-staging",str(target),Clock().now()
    )
    read=ReadDriver(); mutation=MutationDriver(read)
    runtime=Runtime(repo,NODE,Runner(),read,mutation,Clock(),True)
    result=runtime.advance(op.id)

    assert result.state.value=="SUCCESS"
    assert target.joinpath("disks/vda.qcow2").is_file()
    assert not missing_ssh_root.exists()

import io, json, uuid
from pathlib import Path

from vmbackupd.receiver_seed import run_receiver_seed, SEED_BLOCK_BYTES


def _u(): return str(uuid.uuid4())


class Resolver:
    def __init__(self, root, storage_id): self.root=root; self.storage_id=storage_id
    def resolve(self, storage_id):
        assert storage_id == self.storage_id
        return {"storage_id": storage_id, "backup_data_root": str(self.root)}


def test_receiver_seed_finds_latest_compatible_full_and_compares_blocks(tmp_path):
    storage_id, vm_id, rp_id = _u(), _u(), _u()
    root = tmp_path / "storage"; root.mkdir()
    bundle = root / "vms" / vm_id / "2026" / "08" / "bundle"
    (bundle / "disks").mkdir(parents=True)
    disk = bundle / "disks" / "vda.qcow2"
    disk.write_bytes(b"A" * 4096 + b"B" * 4096)
    state = root / ".vmbackupd-replica-state" / "published"; state.mkdir(parents=True)
    (state / f"{rp_id}.json").write_text(json.dumps({
        "state":"PUBLISHED", "storage_id":storage_id, "vm_id":vm_id,
        "kind":"FULL", "restore_point_id":rp_id,
        "bundle_object_id":bundle.relative_to(root).as_posix(),
    }))
    import hashlib
    same = hashlib.sha256(disk.read_bytes()).hexdigest()
    source = io.BytesIO(
        (json.dumps({"protocol_version":1,"operation":"BEGIN","storage_id":storage_id,
                     "vm_id":vm_id,"files":[{"path":"disks/vda.qcow2","logical_size":8192}]})+"\n").encode()
        +(json.dumps({"protocol_version":1,"operation":"COMPARE","path":"disks/vda.qcow2",
                      "blocks":[{"offset":0,"length":8192,"signature":same},
                                {"offset":0,"length":4096,"signature":"0"*64}]})+"\n").encode()
        +(json.dumps({"protocol_version":1,"operation":"FINISH"})+"\n").encode()
    )
    output=io.BytesIO()
    assert run_receiver_seed(source=source, output=output, resolver_client=Resolver(root, storage_id)) == 0
    rows=[json.loads(x) for x in output.getvalue().splitlines()]
    assert rows[0]["status"] == "SEED_READY"
    assert rows[0]["restore_point_id"] == rp_id
    assert rows[0]["block_bytes"] == SEED_BLOCK_BYTES
    assert rows[1] == {"protocol_version":1,"status":"COMPARE_RESULT","same":[True, False]}
    assert rows[2]["status"] == "DONE"


def test_receiver_seed_returns_no_seed_for_incompatible_disk_size(tmp_path):
    storage_id, vm_id, rp_id = _u(), _u(), _u()
    root=tmp_path/"storage"; root.mkdir()
    bundle=root/"vms"/vm_id/"bundle"; (bundle/"disks").mkdir(parents=True)
    (bundle/"disks"/"vda.qcow2").write_bytes(b"x"*10)
    state=root/".vmbackupd-replica-state"/"published"; state.mkdir(parents=True)
    (state/f"{rp_id}.json").write_text(json.dumps({"state":"PUBLISHED","storage_id":storage_id,
        "vm_id":vm_id,"kind":"FULL","restore_point_id":rp_id,
        "bundle_object_id":bundle.relative_to(root).as_posix()}))
    source=io.BytesIO((json.dumps({"protocol_version":1,"operation":"BEGIN","storage_id":storage_id,
        "vm_id":vm_id,"files":[{"path":"disks/vda.qcow2","logical_size":11}]})+"\n").encode())
    output=io.BytesIO()
    assert run_receiver_seed(source=source, output=output, resolver_client=Resolver(root, storage_id)) == 0
    assert json.loads(output.getvalue())["status"] == "NO_SEED"

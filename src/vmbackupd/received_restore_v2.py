# Architecture: NEW
"""Materialize and define received SSH replica restore points on this node."""
from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import RestoreOperationState


class ReceivedRestoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ReceivedRestoreRuntimeV2:
    """Cooperative restore runtime for already-local received replicas."""

    _TERMINAL = {"SUCCESS", "FAILED", "RECOVERY_REQUIRED"}
    _UNSAFE = {"MATERIALIZING", "DEFINING", "STARTING"}

    def __init__(self, repository, node_id, runner, read_driver, mutation_driver,
                 clock, allow_mutation: bool) -> None:
        self.repository = repository
        self.node_id = node_id
        self.runner = runner
        self.read_driver = read_driver
        self.mutation_driver = mutation_driver
        self.clock = clock
        self.allow_mutation = allow_mutation

    @staticmethod
    def _json(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ReceivedRestoreError(
                "RECEIVED_RESTORE_METADATA_INVALID", f"cannot read {path.name}"
            ) from exc
        if not isinstance(value, dict):
            raise ReceivedRestoreError(
                "RECEIVED_RESTORE_METADATA_INVALID", f"{path.name} must be an object"
            )
        return value

    def _run(self, argv, *, timeout=3600):
        result = self.runner.run(tuple(str(v) for v in argv), timeout=timeout)
        if result.returncode != 0:
            detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "")
                      or f"command exited {result.returncode}").strip()
            raise ReceivedRestoreError("RECEIVED_RESTORE_COMMAND_FAILED", detail)
        return result

    def recover_startup(self):
        changed = []
        for operation in self.repository.list_received_restore_operations_v2(self.node_id):
            if operation.state.value not in self._UNSAFE:
                continue
            changed.append(self.repository.transition_received_restore_v2(
                operation.id, operation.state.value, "FAILED", self.clock.now(),
                error="RECEIVED_RESTORE_INTERRUPTED: restore interrupted by daemon restart; staging preserved",
            ))
        return changed

    def tick(self):
        if not self.allow_mutation:
            return []
        progressed = []
        for operation in self.repository.list_received_restore_operations_v2(self.node_id):
            if operation.state.value in self._TERMINAL:
                continue
            progressed.append(self.advance(operation.id))
        return progressed

    def _validate_chain(self, operation):
        chain = self.repository.received_restore_chain_v2(
            operation.restore_point_id, self.node_id
        )
        if not chain:
            raise ReceivedRestoreError("RECEIVED_RESTORE_POINT_NOT_FOUND", "restore point is not received on this node")
        if chain[0]["kind"] != "FULL":
            raise ReceivedRestoreError("RECEIVED_RESTORE_BASE_MISSING", "received incremental chain has no FULL base")
        expected_parent = None
        expected_sequence = 0
        disk_targets = None
        for item in chain:
            if item["status"] != "AVAILABLE":
                raise ReceivedRestoreError("RECEIVED_RESTORE_SOURCE_UNAVAILABLE", "one restore point in the chain is not AVAILABLE")
            if int(item["sequence"]) != expected_sequence:
                raise ReceivedRestoreError("RECEIVED_RESTORE_CHAIN_INVALID", "restore point sequence is not contiguous")
            if item["parent_restore_point_id"] != expected_parent:
                raise ReceivedRestoreError("RECEIVED_RESTORE_CHAIN_INVALID", "restore point parent linkage is invalid")
            bundle = Path(item["bundle_object_id"])
            if not bundle.is_absolute() or not bundle.is_dir():
                raise ReceivedRestoreError("RECEIVED_RESTORE_SOURCE_MISSING", f"bundle is missing: {bundle}")
            manifest = self._json(bundle / "metadata" / "manifest.json")
            restore = self._json(bundle / "metadata" / "restore-point.json")
            if str(restore.get("id")) != str(item["source_restore_point_id"]):
                raise ReceivedRestoreError("RECEIVED_RESTORE_METADATA_MISMATCH", "restore-point ID differs from received catalog")
            targets = tuple(sorted(
                str(d.get("target")) for d in manifest.get("disks", [])
                if isinstance(d, dict) and d.get("target")
            ))
            if not targets:
                targets = tuple(sorted(p.stem for p in (bundle / "disks").glob("*.qcow2")))
            if not targets:
                raise ReceivedRestoreError("RECEIVED_RESTORE_METADATA_INVALID", "backup has no disks")
            if disk_targets is None:
                disk_targets = targets
            elif disk_targets != targets:
                raise ReceivedRestoreError("RECEIVED_RESTORE_CHAIN_INVALID", "disk set changes inside restore chain")
            expected_parent = item["id"]
            expected_sequence += 1
        return chain, disk_targets

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _safe_target(self, operation):
        target = Path(operation.target_root)
        if not target.is_absolute() or target == Path("/") or ".." in target.parts:
            raise ReceivedRestoreError("RESTORE_TARGET_INVALID", "target folder must be an absolute safe path")
        if target.exists():
            raise ReceivedRestoreError("RESTORE_TARGET_EXISTS", "target folder already exists")

        local_destinations = []
        for destination in self.repository.list_storage_destinations(self.node_id):
            storage_type = getattr(destination.storage_type, "value", destination.storage_type)
            if str(storage_type).upper() != "LOCAL":
                continue
            root = Path(destination.backup_data_root)
            if not root.is_absolute() or not root.is_dir():
                continue
            try:
                resolved_root = root.resolve(strict=True)
            except OSError:
                continue
            local_destinations.append((resolved_root, destination))
        if not local_destinations:
            raise ReceivedRestoreError("RESTORE_TARGET_STORAGE_UNAVAILABLE", "no writable LOCAL storage root is available")

        # Walk through already-existing ancestors so a symlink cannot escape the
        # selected/registered storage root.  Missing subdirectories are allowed
        # and are created below that root.
        matching_root = None
        lexical = target.absolute()
        for root, _destination in local_destinations:
            try:
                lexical.relative_to(root)
            except ValueError:
                continue
            matching_root = root
            break
        if matching_root is None:
            raise ReceivedRestoreError("RESTORE_TARGET_OUTSIDE_LOCAL_STORAGE", "target folder must be inside a registered LOCAL storage")

        probe = target.parent
        while not probe.exists() and probe != matching_root:
            probe = probe.parent
        try:
            resolved_probe = probe.resolve(strict=True)
        except OSError as exc:
            raise ReceivedRestoreError("RESTORE_TARGET_PARENT_MISSING", "restore target parent cannot be resolved") from exc
        if not self._within(resolved_probe, matching_root):
            raise ReceivedRestoreError("RESTORE_TARGET_SYMLINK_ESCAPE", "restore target parent escapes the selected storage root")
        if not os.access(resolved_probe, os.W_OK | os.X_OK):
            raise ReceivedRestoreError("RESTORE_TARGET_NOT_WRITABLE", "vmbackupd cannot write to target storage")

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReceivedRestoreError("RESTORE_TARGET_CREATE_FAILED", f"cannot create target parent: {exc}") from exc
        try:
            resolved_parent = target.parent.resolve(strict=True)
        except OSError as exc:
            raise ReceivedRestoreError("RESTORE_TARGET_PARENT_MISSING", "restore target parent cannot be resolved") from exc
        if not self._within(resolved_parent, matching_root):
            raise ReceivedRestoreError("RESTORE_TARGET_SYMLINK_ESCAPE", "restore target parent escapes the selected storage root")
        if not os.access(resolved_parent, os.W_OK | os.X_OK):
            raise ReceivedRestoreError("RESTORE_TARGET_NOT_WRITABLE", "vmbackupd cannot write to target parent folder")

        destination = next(
            item for root, item in local_destinations
            if root == matching_root
        )
        return target, matching_root, destination

    @staticmethod
    def _apply_libvirt_access(target: Path, storage_root: Path, destination) -> None:
        """Make restored images traversable/readable by the storage's QEMU group.

        LOCAL storage preparation already records the intended uid/gid.  Restore
        subdirectories must inherit that access contract as well; otherwise a
        perfectly materialized image can be unreadable by the qemu process.
        """
        gid = getattr(destination, "backup_data_gid", None)
        if gid is None:
            raise ReceivedRestoreError(
                "RESTORE_TARGET_GROUP_UNAVAILABLE",
                "selected LOCAL storage has no backup_data_gid for libvirt access",
            )

        # Repair every directory below the registered storage root that qemu
        # must traverse.  Do not alter the storage root itself; it is managed
        # by the storage preparation contract.
        current = target.parent
        ancestors = []
        while current != storage_root:
            ancestors.append(current)
            parent = current.parent
            if parent == current:
                raise ReceivedRestoreError(
                    "RESTORE_TARGET_OUTSIDE_LOCAL_STORAGE",
                    "restore target escaped the selected storage root",
                )
            current = parent
        for directory in reversed(ancestors):
            try:
                os.chown(directory, -1, int(gid))
                mode = stat.S_IMODE(directory.stat().st_mode) | stat.S_IRGRP | stat.S_IXGRP
                os.chmod(directory, mode)
            except OSError as exc:
                raise ReceivedRestoreError(
                    "RESTORE_TARGET_PERMISSION_FAILED",
                    f"cannot grant libvirt access to {directory}: {exc}",
                ) from exc

        for path in [target, *target.rglob("*")]:
            try:
                os.chown(path, -1, int(gid))
                if path.is_dir():
                    mode = stat.S_IMODE(path.stat().st_mode) | stat.S_IRGRP | stat.S_IXGRP
                else:
                    mode = stat.S_IMODE(path.stat().st_mode) | stat.S_IRGRP
                os.chmod(path, mode)
            except OSError as exc:
                raise ReceivedRestoreError(
                    "RESTORE_TARGET_PERMISSION_FAILED",
                    f"cannot grant libvirt access to {path}: {exc}",
                ) from exc

    def _materialize_disk(self, chain, target_dev: str, staging: Path):
        source = Path(chain[0]["bundle_object_id"]) / "disks" / f"{target_dev}.qcow2"
        if not source.is_file():
            raise ReceivedRestoreError("RECEIVED_RESTORE_SOURCE_MISSING", f"missing FULL disk {target_dev}")
        current = staging / "disks" / f".{target_dev}.flat0.qcow2"
        self._run(["qemu-img", "convert", "-f", "qcow2", "-O", "qcow2", source, current])
        for index, point in enumerate(chain[1:], start=1):
            delta_source = Path(point["bundle_object_id"]) / "disks" / f"{target_dev}.qcow2"
            if not delta_source.is_file():
                raise ReceivedRestoreError("RECEIVED_RESTORE_SOURCE_MISSING", f"missing incremental disk {target_dev}")
            delta = staging / "disks" / f".{target_dev}.delta{index}.qcow2"
            self._run(["cp", "--reflink=auto", "--sparse=always", "--", delta_source, delta])
            self._run(["qemu-img", "rebase", "-u", "-f", "qcow2", "-F", "qcow2", "-b", current, delta])
            next_flat = staging / "disks" / f".{target_dev}.flat{index}.qcow2"
            self._run(["qemu-img", "convert", "-f", "qcow2", "-O", "qcow2", delta, next_flat])
            delta.unlink(missing_ok=True)
            current.unlink(missing_ok=True)
            current = next_flat
        final = staging / "disks" / f"{target_dev}.qcow2"
        os.replace(current, final)
        self._run(["qemu-img", "check", "-f", "qcow2", final], timeout=600)
        return final

    @staticmethod
    def _rewrite_domain(source_xml: Path, operation, disk_paths: dict[str, Path], output: Path):
        try:
            root = ET.parse(source_xml).getroot()
        except Exception as exc:
            raise ReceivedRestoreError("RECEIVED_RESTORE_DOMAIN_XML_INVALID", "cannot parse source domain XML") from exc
        name = root.find("name")
        uid = root.find("uuid")
        if name is None or uid is None:
            raise ReceivedRestoreError("RECEIVED_RESTORE_DOMAIN_XML_INVALID", "domain XML has no name/uuid")
        name.text = operation.target_vm_name
        uid.text = operation.target_domain_uuid
        seen = set()
        for disk in root.findall("./devices/disk"):
            if disk.get("device") != "disk":
                continue
            target = disk.find("target")
            source = disk.find("source")
            dev = target.get("dev") if target is not None else None
            if not dev or dev not in disk_paths or source is None:
                raise ReceivedRestoreError("RECEIVED_RESTORE_DOMAIN_DISK_MISMATCH", "domain disk mapping does not match backup")
            source.attrib.clear()
            source.set("file", str(disk_paths[dev]))
            seen.add(dev)
        if seen != set(disk_paths):
            raise ReceivedRestoreError("RECEIVED_RESTORE_DOMAIN_DISK_MISMATCH", "backup disk set differs from domain XML")
        for interface in root.findall("./devices/interface"):
            link = interface.find("link")
            if link is None:
                link = ET.SubElement(interface, "link")
            link.set("state", "down")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n")

    def advance(self, operation_id: str):
        operation = self.repository.get_restore_operation(operation_id)
        if operation.state.value in self._TERMINAL:
            return operation
        try:
            if operation.state is RestoreOperationState.PLANNED:
                operation = self.repository.transition_received_restore_v2(
                    operation.id, "PLANNED", "VERIFYING", self.clock.now()
                )
            if operation.state is RestoreOperationState.VERIFYING:
                chain, disk_targets = self._validate_chain(operation)
                target, storage_root, destination = self._safe_target(operation)
                if operation.target_vm_name in tuple(self.read_driver.list_domain_names()):
                    raise ReceivedRestoreError("RESTORE_DOMAIN_NAME_EXISTS", "target libvirt domain name already exists")
                operation = self.repository.transition_received_restore_v2(
                    operation.id, "VERIFYING", "MATERIALIZING", self.clock.now()
                )
                staging = target.parent / f".{target.name}.vmbackupd-{operation.id}"
                if staging.exists():
                    raise ReceivedRestoreError("RESTORE_STAGING_EXISTS", f"restore staging already exists: {staging}")
                (staging / "disks").mkdir(parents=True)
                staging_disk_paths = {dev: self._materialize_disk(chain, dev, staging) for dev in disk_targets}
                final_disk_paths = {dev: target / "disks" / path.name for dev, path in staging_disk_paths.items()}
                latest_bundle = Path(chain[-1]["bundle_object_id"])
                self._rewrite_domain(
                    latest_bundle / "metadata" / "domain.xml", operation, final_disk_paths,
                    staging / "metadata" / "restored-domain.xml",
                )
                shutil.copy2(latest_bundle / "metadata" / "domain.xml", staging / "metadata" / "source-domain.xml")
                (staging / "metadata" / "restore-source.json").write_text(json.dumps({
                    "restore_point_id": operation.restore_point_id,
                    "chain": [p["id"] for p in chain],
                }, sort_keys=True) + "\n", encoding="utf-8")
                os.replace(staging, target)
                self._apply_libvirt_access(target, storage_root, destination)
                operation = self.repository.transition_received_restore_v2(
                    operation.id, "MATERIALIZING", "DEFINING", self.clock.now()
                )
            if operation.state is RestoreOperationState.DEFINING:
                target = Path(operation.target_root)
                self.mutation_driver.define(str(target / "metadata" / "restored-domain.xml"))
                if operation.target_vm_name not in tuple(self.read_driver.list_domain_names()):
                    raise ReceivedRestoreError("RESTORE_LIBVIRT_DEFINE_UNVERIFIED", "defined VM is not visible in libvirt")
                observed_uuid = self.read_driver.domain_uuid(operation.target_vm_name)
                if observed_uuid != operation.target_domain_uuid:
                    raise ReceivedRestoreError("RESTORE_LIBVIRT_DEFINE_UNVERIFIED", "defined VM UUID differs from restore plan")
                self.repository.register_vm(
                    self.node_id, operation.target_vm_name, operation.target_vm_name,
                    operation.target_domain_uuid,
                )
                operation = self.repository.transition_received_restore_v2(
                    operation.id, "DEFINING", "READY", self.clock.now()
                )
            if operation.state is RestoreOperationState.READY:
                if operation.start_after_restore:
                    operation = self.repository.transition_received_restore_v2(
                        operation.id, "READY", "STARTING", self.clock.now()
                    )
                    self.mutation_driver.start(operation.target_vm_name)
                    operation = self.repository.transition_received_restore_v2(
                        operation.id, "STARTING", "SUCCESS", self.clock.now()
                    )
                else:
                    operation = self.repository.transition_received_restore_v2(
                        operation.id, "READY", "SUCCESS", self.clock.now()
                    )
            return operation
        except Exception as exc:
            current = self.repository.get_restore_operation(operation_id)
            return self.repository.transition_received_restore_v2(
                current.id, current.state.value, "FAILED", self.clock.now(),
                error=f"{type(exc).__name__}: {exc}",
            )

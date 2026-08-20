"""Safe libvirt definition boundary for LOCAL restore.

A3.5.3 owns:

    materialized target
        -> validated/re-written domain XML
        -> libvirt persistent define
        -> read-back verification
        -> READY

It deliberately does NOT start the restored VM.
"""

from __future__ import annotations

import json
import os
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .bundle import BundlePathPlanner
from .models import (
    RestoreNetworkMode,
    RestoreOperation,
    RestoreOperationState,
)


_MAX_MARKER_BYTES = 1024 * 1024
_MAX_DOMAIN_XML_BYTES = 16 * 1024 * 1024


class RestoreDomainDefinitionError(RuntimeError):
    """Fail-closed restore domain-definition error."""

    def __init__(
        self,
        code: str,
        message: str,
    ) -> None:
        self.code = code
        self.message = message
        super().__init__(
            f"{code}: {message}"
        )


@dataclass(frozen=True, slots=True)
class PreparedRestoreDomain:
    xml_path: str
    target_name: str
    target_uuid: str
    disk_paths: tuple[str, ...]


class LocalRestoreDomainBuilder:
    """Prepare one immutable materialized restore for libvirt definition."""

    @staticmethod
    def _read_regular(
        path: Path,
        *,
        label: str,
        maximum: int,
    ) -> bytes:
        descriptor = None

        try:
            before = path.lstat()

            if (
                not stat.S_ISREG(
                    before.st_mode
                )
                or before.st_nlink != 1
                or before.st_size <= 0
                or before.st_size > maximum
            ):
                raise OSError(
                    f"unsafe {label}"
                )

            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(
                    os,
                    "O_NOFOLLOW",
                    0,
                ),
            )

            opened = os.fstat(
                descriptor
            )

            if (
                not stat.S_ISREG(
                    opened.st_mode
                )
                or opened.st_nlink != 1
                or (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                )
                != (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                )
            ):
                raise OSError(
                    f"{label} identity changed"
                )

            chunks = []
            remaining = opened.st_size

            while remaining:
                payload = os.read(
                    descriptor,
                    min(
                        remaining,
                        1024 * 1024,
                    ),
                )

                if not payload:
                    raise OSError(
                        f"{label} ended early"
                    )

                chunks.append(
                    payload
                )
                remaining -= len(
                    payload
                )

            after = os.fstat(
                descriptor
            )

            if (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ):
                raise OSError(
                    f"{label} changed while reading"
                )

            return b"".join(
                chunks
            )

        except OSError as exc:
            raise RestoreDomainDefinitionError(
                "RESTORE_TARGET_EVIDENCE_INVALID",
                f"cannot safely read {label}",
            ) from exc

        finally:
            if descriptor is not None:
                os.close(
                    descriptor
                )

    @classmethod
    def _marker(
        cls,
        operation: RestoreOperation,
        target: Path,
    ) -> None:
        raw = cls._read_regular(
            target
            / ".vmbackupd-restore.json",
            label="restore materialization marker",
            maximum=_MAX_MARKER_BYTES,
        )

        try:
            value = json.loads(
                raw.decode("utf-8")
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise RestoreDomainDefinitionError(
                "RESTORE_TARGET_EVIDENCE_INVALID",
                "restore materialization marker is invalid",
            ) from exc

        expected = {
            "version": 1,
            "state": "MATERIALIZED",
            "operation_id":
                operation.id,
            "restore_point_id":
                operation.restore_point_id,
            "source_bundle_object_id":
                operation.source_bundle_object_id,
            "target_vm_name":
                operation.target_vm_name,
            "target_domain_uuid":
                operation.target_domain_uuid,
        }

        if (
            not isinstance(value, dict)
            or any(
                value.get(key)
                != expected_value
                for key, expected_value
                in expected.items()
            )
        ):
            raise RestoreDomainDefinitionError(
                "RESTORE_TARGET_EVIDENCE_INVALID",
                (
                    "restore materialization marker "
                    "does not match the operation"
                ),
            )

    @staticmethod
    def _materialized_disks(
        target: Path,
    ) -> dict[str, Path]:
        directory = (
            target
            / "disks"
        )

        try:
            directory_info = (
                directory.lstat()
            )
        except OSError as exc:
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_DISK_MISMATCH",
                "materialized disk directory is unavailable",
            ) from exc

        if (
            not stat.S_ISDIR(
                directory_info.st_mode
            )
            or stat.S_ISLNK(
                directory_info.st_mode
            )
        ):
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_DISK_MISMATCH",
                "materialized disk path is not a safe directory",
            )

        result = {}

        try:
            entries = list(
                directory.iterdir()
            )
        except OSError as exc:
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_DISK_MISMATCH",
                "cannot enumerate materialized disks",
            ) from exc

        if not entries:
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_DISK_MISMATCH",
                "restore has no materialized disks",
            )

        for path in entries:
            name = path.name

            if not name.endswith(
                ".qcow2"
            ):
                raise RestoreDomainDefinitionError(
                    "RESTORE_DOMAIN_DISK_MISMATCH",
                    "materialized disk set contains an unexpected entry",
                )

            target_dev = name[:-6]

            try:
                BundlePathPlanner._component(
                    target_dev,
                    "disk target",
                )
            except ValueError as exc:
                raise RestoreDomainDefinitionError(
                    "RESTORE_DOMAIN_DISK_MISMATCH",
                    "materialized disk target is unsafe",
                ) from exc

            try:
                info = path.lstat()
            except OSError as exc:
                raise RestoreDomainDefinitionError(
                    "RESTORE_DOMAIN_DISK_MISMATCH",
                    "materialized disk cannot be inspected",
                ) from exc

            if (
                not stat.S_ISREG(
                    info.st_mode
                )
                or stat.S_ISLNK(
                    info.st_mode
                )
                or info.st_nlink != 1
                or info.st_size <= 0
            ):
                raise RestoreDomainDefinitionError(
                    "RESTORE_DOMAIN_DISK_MISMATCH",
                    "materialized disk is unsafe",
                )

            if target_dev in result:
                raise RestoreDomainDefinitionError(
                    "RESTORE_DOMAIN_DISK_MISMATCH",
                    "materialized disk target is duplicated",
                )

            result[target_dev] = path

        return result

    @staticmethod
    def _identity_node(
        root: ET.Element,
        name: str,
    ) -> ET.Element:
        values = root.findall(
            name
        )

        if len(values) != 1:
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_XML_INVALID",
                (
                    f"domain XML must contain exactly "
                    f"one {name}"
                ),
            )

        return values[0]

    @staticmethod
    def _disconnect_interfaces(
        devices: ET.Element,
    ) -> None:
        for interface in devices.findall(
            "interface"
        ):
            for existing in list(
                interface.findall(
                    "link"
                )
            ):
                interface.remove(
                    existing
                )

            ET.SubElement(
                interface,
                "link",
                {
                    "state": "down",
                },
            )

    @staticmethod
    def _reject_external_devices(
        devices: ET.Element,
    ) -> None:
        # Host passthrough is not safe to reproduce automatically on a
        # restored domain. A future explicit restore policy may add
        # supported device mapping.
        if devices.findall(
            "hostdev"
        ):
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_EXTERNAL_DEVICE_UNSUPPORTED",
                (
                    "restored domain contains host passthrough "
                    "devices which cannot be reproduced safely"
                ),
            )

    @classmethod
    def _rewrite_disks(
        cls,
        devices: ET.Element,
        materialized: dict[str, Path],
    ) -> tuple[str, ...]:
        used = {}

        for disk in list(
            devices.findall(
                "disk"
            )
        ):
            device = disk.get(
                "device"
            )

            # Removable media and other non-VM disk devices are deliberately
            # not reproduced. This prevents an old ISO/floppy source from
            # becoming a dependency of the restored domain.
            if device != "disk":
                devices.remove(
                    disk
                )
                continue

            if disk.get("type") != "file":
                raise RestoreDomainDefinitionError(
                    "RESTORE_DOMAIN_DISK_UNSUPPORTED",
                    (
                        "restore supports only file-backed "
                        "VM disks"
                    ),
                )

            target_node = disk.find(
                "target"
            )

            source_node = disk.find(
                "source"
            )

            driver = disk.find(
                "driver"
            )

            if (
                target_node is None
                or source_node is None
                or driver is None
                or driver.get("type")
                != "qcow2"
            ):
                raise RestoreDomainDefinitionError(
                    "RESTORE_DOMAIN_DISK_UNSUPPORTED",
                    (
                        "domain disk definition is incomplete "
                        "or not qcow2"
                    ),
                )

            target_dev = target_node.get(
                "dev"
            )

            if not isinstance(
                target_dev,
                str,
            ):
                raise RestoreDomainDefinitionError(
                    "RESTORE_DOMAIN_DISK_UNSUPPORTED",
                    "domain disk target is missing",
                )

            try:
                BundlePathPlanner._component(
                    target_dev,
                    "disk target",
                )
            except ValueError as exc:
                raise RestoreDomainDefinitionError(
                    "RESTORE_DOMAIN_DISK_UNSUPPORTED",
                    "domain disk target is unsafe",
                ) from exc

            if target_dev in used:
                raise RestoreDomainDefinitionError(
                    "RESTORE_DOMAIN_DISK_UNSUPPORTED",
                    "domain disk target is duplicated",
                )

            target_path = materialized.get(
                target_dev
            )

            if target_path is None:
                raise RestoreDomainDefinitionError(
                    "RESTORE_DOMAIN_DISK_MISMATCH",
                    (
                        "domain disk does not have a matching "
                        "materialized target"
                    ),
                )

            # Remove every original source attribute. No backup/source
            # path may survive into the restored writable disk definition.
            source_node.attrib.clear()
            source_node.set(
                "file",
                str(target_path),
            )

            used[target_dev] = str(
                target_path
            )

        if set(used) != set(
            materialized
        ):
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_DISK_MISMATCH",
                (
                    "materialized disk set differs from "
                    "the domain disk set"
                ),
            )

        if not used:
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_DISK_MISMATCH",
                "restored domain has no VM disks",
            )

        return tuple(
            used[target]
            for target in sorted(
                used
            )
        )

    @staticmethod
    def _atomic_write(
        path: Path,
        payload: bytes,
    ) -> None:
        descriptor = None

        try:
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(
                    os,
                    "O_NOFOLLOW",
                    0,
                ),
                0o600,
            )

            offset = 0

            while offset < len(
                payload
            ):
                written = os.write(
                    descriptor,
                    payload[offset:],
                )

                if written <= 0:
                    raise OSError(
                        "domain XML write failed"
                    )

                offset += written

            os.fsync(
                descriptor
            )

        except FileExistsError as exc:
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_XML_EXISTS",
                (
                    "prepared restore domain XML "
                    "already exists"
                ),
            ) from exc

        except RestoreDomainDefinitionError:
            raise

        except OSError as exc:
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_XML_WRITE_FAILED",
                "cannot write restored domain XML",
            ) from exc

        finally:
            if descriptor is not None:
                os.close(
                    descriptor
                )

        directory_fd = None

        try:
            directory_fd = os.open(
                path.parent,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(
                    os,
                    "O_NOFOLLOW",
                    0,
                ),
            )
            os.fsync(
                directory_fd
            )
        except OSError as exc:
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_XML_WRITE_FAILED",
                "cannot persist restored domain XML directory",
            ) from exc
        finally:
            if directory_fd is not None:
                os.close(
                    directory_fd
                )

    def prepare(
        self,
        operation: RestoreOperation,
    ) -> PreparedRestoreDomain:
        if (
            operation.state
            is not RestoreOperationState.DEFINING
        ):
            raise RestoreDomainDefinitionError(
                "RESTORE_EXECUTION_STATE_INVALID",
                "domain preparation requires DEFINING state",
            )

        if (
            operation.network_mode
            is not RestoreNetworkMode.DISCONNECTED
        ):
            raise RestoreDomainDefinitionError(
                "RESTORE_NETWORK_MODE_UNSUPPORTED",
                "A3.5.3 supports DISCONNECTED restore only",
            )

        target = Path(
            operation.target_root
        )

        if (
            not target.is_absolute()
            or target == Path("/")
            or ".." in target.parts
        ):
            raise RestoreDomainDefinitionError(
                "RESTORE_TARGET_EVIDENCE_INVALID",
                "materialized target_root is invalid",
            )

        self._marker(
            operation,
            target,
        )

        materialized = (
            self._materialized_disks(
                target
            )
        )

        source_xml = self._read_regular(
            target
            / "metadata"
            / "domain.xml",
            label="materialized domain XML",
            maximum=_MAX_DOMAIN_XML_BYTES,
        )

        try:
            root = ET.fromstring(
                source_xml
            )
        except ET.ParseError as exc:
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_XML_INVALID",
                "materialized domain XML is malformed",
            ) from exc

        if root.tag != "domain":
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_XML_INVALID",
                "materialized XML root is not domain",
            )

        name_node = self._identity_node(
            root,
            "name",
        )

        uuid_node = self._identity_node(
            root,
            "uuid",
        )

        devices_values = root.findall(
            "devices"
        )

        if len(devices_values) != 1:
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_XML_INVALID",
                (
                    "domain XML must contain exactly "
                    "one devices element"
                ),
            )

        devices = devices_values[0]

        self._reject_external_devices(
            devices
        )

        name_node.text = (
            operation.target_vm_name
        )

        uuid_node.text = (
            operation.target_domain_uuid
        )

        disk_paths = self._rewrite_disks(
            devices,
            materialized,
        )

        self._disconnect_interfaces(
            devices
        )

        payload = ET.tostring(
            root,
            encoding="utf-8",
            xml_declaration=True,
        ) + b"\n"

        output = (
            target
            / "metadata"
            / "restored-domain.xml"
        )

        self._atomic_write(
            output,
            payload,
        )

        return PreparedRestoreDomain(
            xml_path=str(output),
            target_name=operation.target_vm_name,
            target_uuid=operation.target_domain_uuid,
            disk_paths=disk_paths,
        )


class VirshRestoreDriver:
    """Minimal mutating libvirt boundary for persistent restore definition."""

    def __init__(
        self,
        runner,
        connection_uri: str = "qemu:///system",
        timeout: float = 15,
    ) -> None:
        self.runner = runner
        self.connection_uri = connection_uri
        self.timeout = timeout

    def define(
        self,
        xml_path: str,
    ):
        result = self.runner.run(
            (
                "virsh",
                "--connect",
                self.connection_uri,
                "define",
                xml_path,
                "--validate",
            ),
            timeout=self.timeout,
        )

        if result.returncode != 0:
            detail = (
                getattr(
                    result,
                    "stderr",
                    "",
                )
                or getattr(
                    result,
                    "stdout",
                    "",
                )
                or (
                    "virsh define exited "
                    f"{result.returncode}"
                )
            ).strip()

            raise RestoreDomainDefinitionError(
                "RESTORE_LIBVIRT_DEFINE_FAILED",
                detail,
            )

        return result


class LocalRestoreDefinitionExecutor:
    """Advance one materialized LOCAL restore from DEFINING to READY."""

    def __init__(
        self,
        *,
        repository,
        builder: LocalRestoreDomainBuilder,
        read_driver,
        mutation_driver: VirshRestoreDriver,
        clock,
    ) -> None:
        self.repository = repository
        self.builder = builder
        self.read_driver = read_driver
        self.mutation_driver = mutation_driver
        self.clock = clock

    @staticmethod
    def _reason(
        exc: Exception,
    ) -> str:
        message = (
            "LOCAL restore definition failed: "
            f"{type(exc).__name__}: {exc}"
        ).strip()

        if len(message) > 2000:
            message = message[-2000:]

        return message

    def _preflight_catalog_collisions(
        self,
        operation: RestoreOperation,
    ) -> None:
        """Recheck the VM catalog immediately before libvirt mutation."""

        for vm in self.repository.list_vms(
            operation.target_node_id
        ):
            if (
                vm.name
                == operation.target_vm_name
                or vm.external_id
                == operation.target_vm_name
                or (
                    vm.libvirt_domain_uuid
                    is not None
                    and vm.libvirt_domain_uuid
                    == operation.target_domain_uuid
                )
            ):
                raise RestoreDomainDefinitionError(
                    "RESTORE_TARGET_VM_EXISTS",
                    (
                        "VM catalog identity appeared "
                        "after restore planning"
                    ),
                )

    def _preflight_collisions(
        self,
        operation: RestoreOperation,
    ) -> None:
        names = tuple(
            self.read_driver
            .list_domain_names()
        )

        if (
            operation.target_vm_name
            in names
        ):
            raise RestoreDomainDefinitionError(
                "RESTORE_DOMAIN_NAME_EXISTS",
                (
                    "a libvirt domain already exists "
                    "with the target VM name"
                ),
            )

        for name in names:
            current_uuid = (
                self.read_driver
                .domain_uuid(
                    name
                )
            )

            if (
                current_uuid
                == operation.target_domain_uuid
            ):
                raise RestoreDomainDefinitionError(
                    "RESTORE_DOMAIN_UUID_EXISTS",
                    (
                        "a libvirt domain already exists "
                        "with the target UUID"
                    ),
                )

    @staticmethod
    def _verify_defined_xml(
        operation: RestoreOperation,
        prepared: PreparedRestoreDomain,
        xml_text: str,
    ) -> None:
        try:
            root = ET.fromstring(
                xml_text
            )
        except ET.ParseError as exc:
            raise RestoreDomainDefinitionError(
                "RESTORE_LIBVIRT_DEFINE_UNVERIFIED",
                "defined domain XML is malformed",
            ) from exc

        if (
            root.findtext("name")
            != operation.target_vm_name
            or root.findtext("uuid")
            != operation.target_domain_uuid
        ):
            raise RestoreDomainDefinitionError(
                "RESTORE_LIBVIRT_DEFINE_UNVERIFIED",
                "defined domain identity differs from restore plan",
            )

        actual_disks = {}

        for disk in root.findall(
            "./devices/disk"
        ):
            if disk.get(
                "device"
            ) != "disk":
                continue

            target = disk.find(
                "target"
            )
            source = disk.find(
                "source"
            )

            if (
                target is None
                or source is None
                or not target.get("dev")
                or not source.get("file")
            ):
                raise RestoreDomainDefinitionError(
                    "RESTORE_LIBVIRT_DEFINE_UNVERIFIED",
                    "defined domain disk mapping is incomplete",
                )

            actual_disks[
                target.get("dev")
            ] = source.get(
                "file"
            )

        if tuple(
            actual_disks[target]
            for target in sorted(
                actual_disks
            )
        ) != prepared.disk_paths:
            raise RestoreDomainDefinitionError(
                "RESTORE_LIBVIRT_DEFINE_UNVERIFIED",
                (
                    "defined domain disks differ "
                    "from materialized target disks"
                ),
            )

        for interface in root.findall(
            "./devices/interface"
        ):
            link = interface.find(
                "link"
            )

            if (
                link is None
                or link.get("state")
                != "down"
            ):
                raise RestoreDomainDefinitionError(
                    "RESTORE_LIBVIRT_DEFINE_UNVERIFIED",
                    (
                        "defined restored domain has "
                        "an enabled network interface"
                    ),
                )

    def _verify_defined(
        self,
        operation: RestoreOperation,
        prepared: PreparedRestoreDomain,
    ) -> None:
        names = tuple(
            self.read_driver
            .list_domain_names()
        )

        if (
            operation.target_vm_name
            not in names
        ):
            raise RestoreDomainDefinitionError(
                "RESTORE_LIBVIRT_DEFINE_UNVERIFIED",
                "defined domain is not visible in libvirt",
            )

        current_uuid = (
            self.read_driver
            .domain_uuid(
                operation.target_vm_name
            )
        )

        if (
            current_uuid
            != operation.target_domain_uuid
        ):
            raise RestoreDomainDefinitionError(
                "RESTORE_LIBVIRT_DEFINE_UNVERIFIED",
                "defined domain UUID differs from restore plan",
            )

        xml_text = (
            self.read_driver
            .domain_xml(
                operation.target_vm_name
            )
        )

        self._verify_defined_xml(
            operation,
            prepared,
            xml_text,
        )

    def advance(
        self,
        operation_id: str,
    ) -> RestoreOperation:
        operation = (
            self.repository
            .get_restore_operation(
                operation_id
            )
        )

        if (
            operation.state
            is not RestoreOperationState.DEFINING
        ):
            raise RestoreDomainDefinitionError(
                "RESTORE_EXECUTION_STATE_INVALID",
                (
                    "A3.5.3 may advance only "
                    "a DEFINING restore operation"
                ),
            )

        try:
            # create_restore_operation() already checks catalog
            # collisions, but planning and actual definition are
            # separated in time. Recheck both catalog and libvirt
            # immediately before any external mutation.
            self._preflight_catalog_collisions(
                operation
            )

            self._preflight_collisions(
                operation
            )

            prepared = (
                self.builder.prepare(
                    operation
                )
            )

            self.mutation_driver.define(
                prepared.xml_path
            )

            # Never claim READY merely because virsh returned zero.
            # The persistent definition must be visible and must still
            # carry the exact frozen restore identity and disk mapping.
            self._verify_defined(
                operation,
                prepared,
            )

            return (
                self.repository
                .mark_restore_ready(
                    operation.id,
                    self.clock.now(),
                )
            )

        except Exception as exc:
            current = (
                self.repository
                .get_restore_operation(
                    operation.id
                )
            )

            if (
                current.state
                is RestoreOperationState.RECOVERY_REQUIRED
            ):
                return current

            if (
                current.state
                is RestoreOperationState.DEFINING
            ):
                return (
                    self.repository
                    .require_restore_recovery(
                        operation.id,
                        self._reason(
                            exc
                        ),
                        self.clock.now(),
                    )
                )

            raise

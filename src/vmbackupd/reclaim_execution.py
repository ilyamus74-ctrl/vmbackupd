from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from .bundle import (
    BundlePathPlanner,
    BundlePurger,
    BundleQuarantiner,
)
from .models import (
    ReclaimBundle,
    ReclaimBundleState,
    ReclaimOperation,
    ReclaimOperationState,
)
from .repository import SQLiteRepository


class ReclaimExecutionError(RuntimeError):
    pass


class ReclaimRecoveryRequiredError(ReclaimExecutionError):
    pass


class ReclaimInsufficientSpaceError(ReclaimExecutionError):
    def __init__(
        self,
        *,
        free_bytes_after: int,
        required_backup_bytes: int,
        reserve_bytes: int,
    ) -> None:
        self.free_bytes_after = free_bytes_after
        self.required_backup_bytes = required_backup_bytes
        self.reserve_bytes = reserve_bytes
        required_total = required_backup_bytes + reserve_bytes

        super().__init__(
            "measured free space after reclaim is insufficient: "
            f"free={free_bytes_after}, "
            f"required_backup={required_backup_bytes}, "
            f"reserve={reserve_bytes}, "
            f"required_total={required_total}"
        )


from .repository import DomainInvariantError

class ReclaimExecutor:
    """Drive one durable capacity-reclaim transaction to completion."""

    _DESTRUCTIVE_STATES = {
        ReclaimOperationState.RETIRING,
        ReclaimOperationState.QUARANTINED,
        ReclaimOperationState.CATALOG_REMOVED,
        ReclaimOperationState.PURGING,
        ReclaimOperationState.PURGED,
    }

    def __init__(
        self,
        repository: SQLiteRepository,
        planner: BundlePathPlanner,
        *,
        storage_destination_id: str,
        quarantiner: BundleQuarantiner | None = None,
        purger: BundlePurger | None = None,
        free_space_reader: Callable[[Path], int] | None = None,
    ) -> None:
        if not storage_destination_id:
            raise ValueError(
                "storage_destination_id must not be empty"
            )

        self.repository = repository
        self.planner = planner
        self.storage_destination_id = storage_destination_id
        self.quarantiner = (
            quarantiner or BundleQuarantiner(planner)
        )
        self.purger = purger or BundlePurger(planner)
        self.free_space_reader = (
            free_space_reader
            or self._default_free_space_reader
        )

    @staticmethod
    def _default_free_space_reader(root: Path) -> int:
        return shutil.disk_usage(root).free

    def execute(
        self,
        operation_id: str,
    ) -> ReclaimOperation:
        operation = self.repository.get_reclaim_operation(
            operation_id
        )

        if (
            operation.state
            is ReclaimOperationState.RECOVERY_REQUIRED
        ):
            raise ReclaimRecoveryRequiredError(
                "reclaim operation requires explicit recovery"
            )

        return self._drive_with_recovery(operation_id)

    def recover(
        self,
        operation_id: str,
    ) -> ReclaimOperation:
        operation = self.repository.get_reclaim_operation(
            operation_id
        )

        if (
            operation.state
            is not ReclaimOperationState.RECOVERY_REQUIRED
        ):
            raise ReclaimExecutionError(
                "reclaim recovery requires RECOVERY_REQUIRED"
            )

        try:
            self.repository.resume_reclaim_recovery(
                operation_id
            )
        except Exception as exc:
            raise ReclaimRecoveryRequiredError(
                "durable reclaim recovery cannot be resumed"
            ) from exc

        return self._drive_with_recovery(operation_id)

    def _drive_with_recovery(
        self,
        operation_id: str,
    ) -> ReclaimOperation:
        try:
            return self._drive(operation_id)
        except (
            ReclaimInsufficientSpaceError,
            ReclaimRecoveryRequiredError,
        ):
            raise
        except Exception as exc:
            current = self.repository.get_reclaim_operation(
                operation_id
            )

            if (
                current.state
                is ReclaimOperationState.RECOVERY_REQUIRED
            ):
                raise ReclaimRecoveryRequiredError(
                    "reclaim operation entered recovery-required state"
                ) from exc

            if current.state in self._DESTRUCTIVE_STATES:
                message = (
                    "reclaim executor stopped in destructive state "
                    f"{current.state}: {type(exc).__name__}: {exc}"
                )

                try:
                    self.repository.require_reclaim_recovery(
                        operation_id,
                        message,
                    )
                except Exception as recovery_exc:
                    raise ReclaimExecutionError(
                        "reclaim failed and recovery state "
                        "could not be persisted"
                    ) from recovery_exc

                raise ReclaimRecoveryRequiredError(
                    message
                ) from exc

            raise

    def _drive(
        self,
        operation_id: str,
    ) -> ReclaimOperation:
        # One pass can process all bundles in a stage; only a handful of
        # operation-level state changes are possible. Guard against an
        # accidental future non-progressing state-machine loop.
        for _ in range(32):
            operation = self.repository.get_reclaim_operation(
                operation_id
            )

            if (
                operation.storage_destination_id
                != self.storage_destination_id
            ):
                raise ReclaimExecutionError(
                    "reclaim operation belongs to another "
                    "storage destination"
                )

            state = operation.state

            if state is ReclaimOperationState.PLANNED:
                try:
                    self.repository.begin_reclaim_retirement(
                        operation_id
                    )
                except DomainInvariantError:
                    # Repository safety checks intentionally leave the
                    # operation PLANNED when retirement is rejected before
                    # any destructive transition. Persist ABORTED here so a
                    # deterministic pre-destructive refusal (for example a
                    # replica dependency or changed policy/snapshot) cannot
                    # leave a stale operation that blocks future reclaim.
                    current = self.repository.get_reclaim_operation(
                        operation_id
                    )
                    if (
                        current.state
                        is ReclaimOperationState.PLANNED
                    ):
                        self.repository.abort_reclaim(
                            operation_id
                        )
                    raise
                continue

            if state is ReclaimOperationState.RETIRING:
                self._retire_bundles(operation_id)
                continue

            if state is ReclaimOperationState.QUARANTINED:
                self._verify_quarantined_bundles(
                    operation_id
                )
                self.repository.retire_reclaim_catalog(
                    operation_id
                )
                continue

            if (
                state
                is ReclaimOperationState.CATALOG_REMOVED
            ):
                self._verify_quarantined_bundles(
                    operation_id
                )
                self.repository.begin_reclaim_purge(
                    operation_id
                )
                continue

            if state is ReclaimOperationState.PURGING:
                self._purge_bundles(operation_id)
                continue

            if state is ReclaimOperationState.PURGED:
                self._verify_purged_bundles(operation_id)

                free_bytes_after = int(
                    self.free_space_reader(
                        self.planner.root
                    )
                )

                if free_bytes_after < 0:
                    raise ReclaimExecutionError(
                        "free-space reader returned a negative value"
                    )

                completed = self.repository.complete_reclaim(
                    operation_id,
                    free_bytes_after=free_bytes_after,
                )

                self._require_capacity(completed)
                return completed

            if state is ReclaimOperationState.COMPLETED:
                self._require_capacity(operation)
                return operation

            if (
                state
                is ReclaimOperationState.RECOVERY_REQUIRED
            ):
                raise ReclaimRecoveryRequiredError(
                    "reclaim operation requires explicit recovery"
                )

            if state is ReclaimOperationState.ABORTED:
                raise ReclaimExecutionError(
                    "aborted reclaim operation cannot be executed"
                )

            raise ReclaimExecutionError(
                f"unsupported reclaim operation state: {state}"
            )

        raise ReclaimExecutionError(
            "reclaim state machine did not converge"
        )

    def _require_capacity(
        self,
        operation: ReclaimOperation,
    ) -> None:
        if operation.free_bytes_after is None:
            raise ReclaimExecutionError(
                "completed reclaim has no measured free_bytes_after"
            )

        required_total = (
            operation.required_backup_bytes
            + operation.reserve_bytes
        )

        if operation.free_bytes_after < required_total:
            raise ReclaimInsufficientSpaceError(
                free_bytes_after=operation.free_bytes_after,
                required_backup_bytes=(
                    operation.required_backup_bytes
                ),
                reserve_bytes=operation.reserve_bytes,
            )

    def _retire_bundles(
        self,
        operation_id: str,
    ) -> None:
        bundles = self.repository.list_reclaim_bundles(
            operation_id
        )

        if not bundles:
            raise ReclaimExecutionError(
                "reclaim operation has no bundles"
            )

        for bundle in bundles:
            if (
                bundle.state
                is ReclaimBundleState.QUARANTINED
            ):
                self._verify_one_quarantined(bundle)
                continue

            if (
                bundle.state
                is not ReclaimBundleState.PLANNED
            ):
                raise ReclaimExecutionError(
                    "RETIRING contains invalid bundle state "
                    f"{bundle.state}"
                )

            presence = (
                self.purger.inspect_reclaim_presence(
                    operation_id=bundle.operation_id,
                    restore_point_id=(
                        bundle.restore_point_id
                    ),
                    destination_id=bundle.destination_id,
                    source_bundle_object_id=(
                        bundle.source_bundle_object_id
                    ),
                )
            )

            if presence.purging_exists:
                raise ReclaimExecutionError(
                    "purge staging exists while operation "
                    "is RETIRING"
                )

            source_present = (
                self.quarantiner.source_present(
                    bundle.source_bundle_object_id
                )
            )

            if (
                source_present
                and presence.quarantine_exists
            ):
                raise ReclaimExecutionError(
                    "both source and deterministic quarantine "
                    "bundle exist"
                )

            if source_present:
                result = self.quarantiner.quarantine(
                    source_bundle_object_id=(
                        bundle.source_bundle_object_id
                    ),
                    operation_id=bundle.operation_id,
                    restore_point_id=(
                        bundle.restore_point_id
                    ),
                    destination_id=bundle.destination_id,
                )
            else:
                if not presence.quarantine_exists:
                    raise ReclaimExecutionError(
                        "both source and deterministic quarantine "
                        "bundle are absent"
                    )

                result = (
                    self.quarantiner.inspect_quarantine(
                        source_bundle_object_id=(
                            bundle.source_bundle_object_id
                        ),
                        operation_id=bundle.operation_id,
                        restore_point_id=(
                            bundle.restore_point_id
                        ),
                        destination_id=bundle.destination_id,
                    )
                )

                # Recheck after inspection so recovery never knowingly seals
                # duplicate source/quarantine names.
                if self.quarantiner.source_present(
                    bundle.source_bundle_object_id
                ):
                    raise ReclaimExecutionError(
                        "source bundle reappeared during "
                        "quarantine recovery"
                    )

            self.repository.mark_reclaim_bundle_quarantined(
                bundle.operation_id,
                bundle.destination_id,
                bundle.source_bundle_object_id,
                quarantine_object_id=(
                    result.quarantine_object_id
                ),
                expected_physical_bytes=(
                    result.expected_physical_bytes
                ),
                source_device=result.source_device,
                source_inode=result.source_inode,
            )

        self.repository.mark_reclaim_quarantined(
            operation_id
        )

    def _verify_quarantined_bundles(
        self,
        operation_id: str,
    ) -> None:
        bundles = self.repository.list_reclaim_bundles(
            operation_id
        )

        if not bundles:
            raise ReclaimExecutionError(
                "reclaim operation has no bundles"
            )

        for bundle in bundles:
            if (
                bundle.state
                is not ReclaimBundleState.QUARANTINED
            ):
                raise ReclaimExecutionError(
                    "quarantined operation contains invalid "
                    f"bundle state {bundle.state}"
                )

            self._verify_one_quarantined(bundle)

    def _verify_one_quarantined(
        self,
        bundle: ReclaimBundle,
    ) -> None:
        if self.quarantiner.source_present(
            bundle.source_bundle_object_id
        ):
            raise ReclaimExecutionError(
                "source bundle still exists after quarantine"
            )

        presence = self.purger.inspect_reclaim_presence(
            operation_id=bundle.operation_id,
            restore_point_id=bundle.restore_point_id,
            destination_id=bundle.destination_id,
            source_bundle_object_id=(
                bundle.source_bundle_object_id
            ),
        )

        if (
            not presence.quarantine_exists
            or presence.purging_exists
        ):
            raise ReclaimExecutionError(
                "quarantined bundle filesystem state "
                "does not match durable state"
            )

        inspected = self.quarantiner.inspect_quarantine(
            source_bundle_object_id=(
                bundle.source_bundle_object_id
            ),
            operation_id=bundle.operation_id,
            restore_point_id=bundle.restore_point_id,
            destination_id=bundle.destination_id,
        )

        expected = (
            bundle.quarantine_object_id,
            bundle.expected_physical_bytes,
            bundle.source_device,
            bundle.source_inode,
        )

        actual = (
            inspected.quarantine_object_id,
            inspected.expected_physical_bytes,
            inspected.source_device,
            inspected.source_inode,
        )

        if None in expected or expected != actual:
            raise ReclaimExecutionError(
                "quarantined filesystem evidence differs "
                "from durable reclaim bundle"
            )

    def _purge_bundles(
        self,
        operation_id: str,
    ) -> None:
        bundles = self.repository.list_reclaim_bundles(
            operation_id
        )

        if not bundles:
            raise ReclaimExecutionError(
                "reclaim operation has no bundles"
            )

        for bundle in bundles:
            if (
                bundle.state
                is ReclaimBundleState.QUARANTINED
            ):
                self._verify_one_quarantined(bundle)
                bundle = (
                    self.repository.begin_reclaim_bundle_purge(
                        operation_id,
                        bundle.destination_id,
                        bundle.source_bundle_object_id,
                    )
                )

            if bundle.state is ReclaimBundleState.PURGING:
                if self.quarantiner.source_present(
                    bundle.source_bundle_object_id
                ):
                    raise ReclaimExecutionError(
                        "source bundle exists during physical purge"
                    )

                presence = (
                    self.purger.inspect_reclaim_presence(
                        operation_id=bundle.operation_id,
                        restore_point_id=(
                            bundle.restore_point_id
                        ),
                        destination_id=bundle.destination_id,
                        source_bundle_object_id=(
                            bundle.source_bundle_object_id
                        ),
                    )
                )

                if (
                    not presence.quarantine_exists
                    and not presence.purging_exists
                ):
                    # Durable per-bundle PURGING was committed before
                    # physical deletion was authorized. Therefore complete
                    # absence after that intent is safe evidence that the
                    # destructive step reached filesystem completion.
                    self.repository.mark_reclaim_bundle_purged(
                        operation_id,
                        bundle.destination_id,
                        bundle.source_bundle_object_id,
                    )
                    continue

                self.purger.purge(
                    quarantine_object_id=(
                        bundle.quarantine_object_id
                    ),
                    operation_id=bundle.operation_id,
                    restore_point_id=(
                        bundle.restore_point_id
                    ),
                    expected_physical_bytes=(
                        bundle.expected_physical_bytes
                    ),
                    source_device=bundle.source_device,
                    source_inode=bundle.source_inode,
                    destination_id=bundle.destination_id,
                    source_bundle_object_id=(
                        bundle.source_bundle_object_id
                    ),
                )

                after = self.purger.inspect_reclaim_presence(
                    operation_id=bundle.operation_id,
                    restore_point_id=bundle.restore_point_id,
                    destination_id=bundle.destination_id,
                    source_bundle_object_id=(
                        bundle.source_bundle_object_id
                    ),
                )

                if (
                    after.quarantine_exists
                    or after.purging_exists
                ):
                    raise ReclaimExecutionError(
                        "bundle remains after physical purge"
                    )

                self.repository.mark_reclaim_bundle_purged(
                    operation_id,
                    bundle.destination_id,
                    bundle.source_bundle_object_id,
                )
                continue

            if bundle.state is ReclaimBundleState.PURGED:
                self._verify_one_purged(bundle)
                continue

            raise ReclaimExecutionError(
                "PURGING operation contains invalid bundle "
                f"state {bundle.state}"
            )

        self.repository.mark_reclaim_purged(
            operation_id
        )

    def _verify_purged_bundles(
        self,
        operation_id: str,
    ) -> None:
        bundles = self.repository.list_reclaim_bundles(
            operation_id
        )

        if not bundles:
            raise ReclaimExecutionError(
                "reclaim operation has no bundles"
            )

        for bundle in bundles:
            if bundle.state is not ReclaimBundleState.PURGED:
                raise ReclaimExecutionError(
                    "PURGED operation contains non-PURGED bundle"
                )

            self._verify_one_purged(bundle)

    def _verify_one_purged(
        self,
        bundle: ReclaimBundle,
    ) -> None:
        if self.quarantiner.source_present(
            bundle.source_bundle_object_id
        ):
            raise ReclaimExecutionError(
                "source bundle exists after physical purge"
            )

        presence = self.purger.inspect_reclaim_presence(
            operation_id=bundle.operation_id,
            restore_point_id=bundle.restore_point_id,
            destination_id=bundle.destination_id,
            source_bundle_object_id=(
                bundle.source_bundle_object_id
            ),
        )

        if (
            presence.quarantine_exists
            or presence.purging_exists
        ):
            raise ReclaimExecutionError(
                "physical reclaim object remains after PURGED"
            )

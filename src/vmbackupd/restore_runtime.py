"""LOCAL restore runtime orchestration.

This module connects the already-separated restore boundaries:

    PLANNED / VERIFYING
        -> source verification + materialization
        -> DEFINING
        -> persistent libvirt definition
        -> READY
        -> optional STARTING
        -> SUCCESS

Unsafe restore states are never guessed or automatically resumed after
controller restart.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .models import (
    RestoreOperation,
    RestoreOperationState,
    RestorePointLocationRole,
)


class RestoreExecutionError(RuntimeError):
    """Fail-closed restore orchestration error."""

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


class LocalRestoreStartExecutor:
    """Advance READY -> STARTING -> SUCCESS with durable start evidence."""

    def __init__(
        self,
        *,
        repository,
        read_driver,
        mutation_driver,
        clock,
    ) -> None:
        self.repository = repository
        self.read_driver = read_driver
        self.mutation_driver = mutation_driver
        self.clock = clock

    @staticmethod
    def _reason(
        exc: Exception,
    ) -> str:
        value = (
            "LOCAL restore start failed: "
            f"{type(exc).__name__}: {exc}"
        ).strip()

        if len(value) > 2000:
            value = value[-2000:]

        return value

    def _verify_frozen_definition(
        self,
        operation: RestoreOperation,
    ) -> None:
        names = tuple(
            self.read_driver
            .list_domain_names()
        )

        if (
            operation.target_vm_name
            not in names
        ):
            raise RestoreExecutionError(
                "RESTORE_START_IDENTITY_MISMATCH",
                "restored libvirt domain is not present",
            )

        observed_uuid = (
            self.read_driver
            .domain_uuid(
                operation.target_vm_name
            )
        )

        if (
            observed_uuid
            != operation.target_domain_uuid
        ):
            raise RestoreExecutionError(
                "RESTORE_START_IDENTITY_MISMATCH",
                (
                    "restored libvirt UUID differs "
                    "from the frozen restore identity"
                ),
            )

        xml_text = (
            self.read_driver
            .domain_xml(
                operation.target_vm_name
            )
        )

        try:
            root = ET.fromstring(
                xml_text
            )
        except ET.ParseError as exc:
            raise RestoreExecutionError(
                "RESTORE_START_DEFINITION_INVALID",
                "restored libvirt XML is malformed",
            ) from exc

        if (
            root.findtext("name")
            != operation.target_vm_name
            or root.findtext("uuid")
            != operation.target_domain_uuid
        ):
            raise RestoreExecutionError(
                "RESTORE_START_IDENTITY_MISMATCH",
                (
                    "restored domain definition differs "
                    "from the frozen restore identity"
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
                raise RestoreExecutionError(
                    "RESTORE_START_NETWORK_NOT_DISCONNECTED",
                    (
                        "restored VM network is not "
                        "durably disconnected"
                    ),
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
            is not RestoreOperationState.READY
        ):
            raise RestoreExecutionError(
                "RESTORE_EXECUTION_STATE_INVALID",
                "restore start requires READY state",
            )

        if not operation.start_after_restore:
            raise RestoreExecutionError(
                "RESTORE_START_NOT_REQUESTED",
                "restore operation did not request VM start",
            )

        # STARTING is persisted before any read/write interaction which
        # participates in the unsafe start boundary. A daemon restart from
        # this point can no longer silently retry a possibly-issued start.
        operation = (
            self.repository
            .mark_restore_starting(
                operation.id,
                self.clock.now(),
            )
        )

        try:
            self._verify_frozen_definition(
                operation
            )

            self.mutation_driver.start(
                operation.target_vm_name
            )

            observed_state = (
                self.read_driver
                .domain_state(
                    operation.target_vm_name
                )
            )

            if (
                str(observed_state)
                .strip()
                .lower()
                != "running"
            ):
                raise RestoreExecutionError(
                    "RESTORE_LIBVIRT_START_UNVERIFIED",
                    (
                        "restored domain did not reach "
                        "the running state"
                    ),
                )

            # Starting must not have changed the frozen domain identity or
            # reconnect the deliberately disconnected interfaces.
            self._verify_frozen_definition(
                operation
            )

            return (
                self.repository
                .finalize_restore_success(
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
                is RestoreOperationState.STARTING
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


class LocalRestorePipeline:
    """Advance exactly one cooperative LOCAL restore stage per call."""

    def __init__(
        self,
        *,
        repository,
        source_executor,
        definition_executor,
        start_executor,
        clock,
    ) -> None:
        self.repository = repository
        self.source_executor = source_executor
        self.definition_executor = definition_executor
        self.start_executor = start_executor
        self.clock = clock

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

        if operation.state in {
            RestoreOperationState.PLANNED,
            RestoreOperationState.VERIFYING,
        }:
            return (
                self.source_executor
                .advance(
                    operation.id
                )
            )

        if (
            operation.state
            is RestoreOperationState.DEFINING
        ):
            return (
                self.definition_executor
                .advance(
                    operation.id
                )
            )

        if (
            operation.state
            is RestoreOperationState.READY
        ):
            if operation.start_after_restore:
                return (
                    self.start_executor
                    .advance(
                        operation.id
                    )
                )

            return (
                self.repository
                .finalize_restore_success(
                    operation.id,
                    self.clock.now(),
                )
            )

        # These states may already have produced filesystem or external
        # libvirt side effects. Restart/takeover must reconcile them rather
        # than calling the executor again.
        if operation.state in {
            RestoreOperationState.ACQUIRING,
            RestoreOperationState.MATERIALIZING,
            RestoreOperationState.STARTING,
        }:
            raise RestoreExecutionError(
                "RESTORE_EXECUTION_STATE_INVALID",
                (
                    "unsafe restore state cannot be "
                    "automatically resumed"
                ),
            )

        if operation.state in {
            RestoreOperationState.SUCCESS,
            RestoreOperationState.FAILED,
            RestoreOperationState.RECOVERY_REQUIRED,
        }:
            return operation

        raise RestoreExecutionError(
            "RESTORE_EXECUTION_STATE_INVALID",
            (
                "restore operation is not actionable "
                f"from {operation.state.value}"
            ),
        )


class RestoreRuntimeController:
    """Cooperative LOCAL restore polling owned by the daemon controller."""

    _UNSAFE = frozenset({
        RestoreOperationState.ACQUIRING,
        RestoreOperationState.MATERIALIZING,
        RestoreOperationState.DEFINING,
        RestoreOperationState.STARTING,
    })

    _TERMINAL = frozenset({
        RestoreOperationState.SUCCESS,
        RestoreOperationState.FAILED,
        RestoreOperationState.RECOVERY_REQUIRED,
    })

    def __init__(
        self,
        *,
        repository,
        node_id: str,
        pipeline: LocalRestorePipeline,
        clock,
        allow_mutation: bool,
    ) -> None:
        self.repository = repository
        self.node_id = node_id
        self.pipeline = pipeline
        self.clock = clock
        self.allow_mutation = allow_mutation

    @staticmethod
    def _is_remote(
        operation: RestoreOperation,
    ) -> bool:
        return (
            operation.source_role
            is RestorePointLocationRole.REPLICA
            or operation.source_remote_node_id
            is not None
            or operation.source_remote_storage_id
            is not None
        )

    @staticmethod
    def _reason(
        prefix: str,
        exc: Exception,
    ) -> str:
        value = (
            f"{prefix}: "
            f"{type(exc).__name__}: {exc}"
        ).strip()

        if len(value) > 2000:
            value = value[-2000:]

        return value

    def recover_startup(
        self,
    ) -> list[RestoreOperation]:
        """Fence unsafe restore state after daemon/controller restart."""

        changed = []

        for operation in (
            self.repository
            .list_restore_operations_for_node(
                self.node_id
            )
        ):
            if (
                operation.state
                not in self._UNSAFE
            ):
                continue

            changed.append(
                self.repository
                .require_restore_recovery(
                    operation.id,
                    (
                        "restore reconciliation required "
                        "after daemon/controller startup"
                    ),
                    self.clock.now(),
                )
            )

        return changed

    def tick(
        self,
    ) -> list[RestoreOperation]:
        if not self.allow_mutation:
            return []

        progressed = []

        for operation in (
            self.repository
            .list_restore_operations_for_node(
                self.node_id
            )
        ):
            operation = (
                self.repository
                .get_restore_operation(
                    operation.id
                )
            )

            if (
                operation.state
                in self._TERMINAL
            ):
                continue

            if self._is_remote(
                operation
            ):
                # Remote planning/manifest identity exists, but acquisition
                # of restore bytes intentionally does not. A runtime attempt
                # must fail explicitly rather than remain silently pending.
                if operation.state in {
                    RestoreOperationState.PLANNED,
                    RestoreOperationState.VERIFYING,
                }:
                    progressed.append(
                        self.repository
                        .fail_restore(
                            operation.id,
                            (
                                "RESTORE_REMOTE_ACQUISITION_NOT_IMPLEMENTED: "
                                "remote restore bytes are not implemented"
                            ),
                            self.clock.now(),
                        )
                    )

                continue

            # Unsafe states seen during normal runtime should not normally
            # occur here except as the result of an interrupted call. Never
            # auto-resume them.
            if (
                operation.state
                in self._UNSAFE
                and operation.state
                is not RestoreOperationState.DEFINING
            ):
                progressed.append(
                    self.repository
                    .require_restore_recovery(
                        operation.id,
                        (
                            "unsafe restore state observed "
                            "without active executor ownership"
                        ),
                        self.clock.now(),
                    )
                )
                continue

            try:
                progressed.append(
                    self.pipeline.advance(
                        operation.id
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
                    in self._UNSAFE
                ):
                    progressed.append(
                        self.repository
                        .require_restore_recovery(
                            current.id,
                            self._reason(
                                (
                                    "restore executor "
                                    "raised unexpectedly"
                                ),
                                exc,
                            ),
                            self.clock.now(),
                        )
                    )

                elif current.state in {
                    RestoreOperationState.PLANNED,
                    RestoreOperationState.VERIFYING,
                }:
                    progressed.append(
                        self.repository
                        .fail_restore(
                            current.id,
                            self._reason(
                                (
                                    "restore executor "
                                    "raised before mutation"
                                ),
                                exc,
                            ),
                            self.clock.now(),
                        )
                    )

                # READY without start mutation is safe and retryable. If
                # finalization itself failed, leave it READY for a later tick.
                elif (
                    current.state
                    is RestoreOperationState.READY
                ):
                    continue

                else:
                    raise

        return progressed

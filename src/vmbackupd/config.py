"""Typed Phase 3C TOML configuration."""

from __future__ import annotations

import grp
import pwd
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


DEFAULT_CONFIG_PATH = Path("/etc/vmbackupd/vmbackupd.toml")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    node_name: str
    database_path: Path
    socket_path: Path
    socket_mode: int = 0o660
    tick_interval_seconds: float = 1
    controller_lease_seconds: int = 30
    execution_lease_seconds: int = 300


@dataclass(frozen=True, slots=True)
class LibvirtConfig:
    uri: str = "qemu:///system"
    allow_mutation: bool = False


@dataclass(frozen=True, slots=True)
class StorageConfig:
    name: str
    control_root: Path
    backup_data_root: Path
    backup_data_mode: int = 0o750
    backup_data_uid: int | None = None
    backup_data_gid: int | None = None
    minimum_free_bytes: int = 0
    minimum_free_percent: float = 5


@dataclass(frozen=True, slots=True)
class StorageCatalogConfig:
    default_destination: str
    destinations: tuple[StorageConfig, ...]

    @property
    def default(self) -> StorageConfig:
        return next(item for item in self.destinations
                    if item.name == self.default_destination)


@dataclass(frozen=True, slots=True)
class AppConfig:
    daemon: DaemonConfig
    libvirt: LibvirtConfig
    storage: StorageCatalogConfig


def _absolute(value: object, label: str) -> Path:
    path = Path(str(value))
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"{label} must be an absolute traversal-free path")
    return path


def _mode(value: object, label: str, *, world_writable_forbidden: bool = True) -> int:
    try:
        mode = int(str(value), 8)
    except ValueError as exc:
        raise ConfigError(f"{label} must be an octal string") from exc
    if not 0 <= mode <= 0o777 or (world_writable_forbidden and mode & 0o002):
        raise ConfigError(f"{label} is unsafe")
    return mode


def load_config(
    path: str | Path = DEFAULT_CONFIG_PATH, *,
    user_lookup: Callable[[str], object] = pwd.getpwnam,
    group_lookup: Callable[[str], object] = grp.getgrnam,
) -> AppConfig:
    try:
        with Path(path).open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load configuration: {exc}") from exc
    try:
        daemon_raw, libvirt_raw, storage_raw = raw["daemon"], raw["libvirt"], raw["storage"]
        node_name = str(daemon_raw["node_name"]).strip()
        if not node_name:
            raise ConfigError("daemon.node_name cannot be empty")
        tick = float(daemon_raw.get("tick_interval_seconds", 1))
        controller = int(daemon_raw.get("controller_lease_seconds", 30))
        execution = int(daemon_raw.get("execution_lease_seconds", 300))
        if tick <= 0 or controller < 1 or execution < 1:
            raise ConfigError("daemon intervals and leases must be positive")
        daemon = DaemonConfig(
            node_name=node_name,
            database_path=_absolute(daemon_raw["database_path"], "daemon.database_path"),
            socket_path=_absolute(daemon_raw["socket_path"], "daemon.socket_path"),
            socket_mode=_mode(daemon_raw.get("socket_mode", "0660"), "daemon.socket_mode"),
            tick_interval_seconds=tick, controller_lease_seconds=controller,
            execution_lease_seconds=execution,
        )
        allow_mutation = libvirt_raw.get("allow_mutation", False)
        if not isinstance(allow_mutation, bool):
            raise ConfigError("libvirt.allow_mutation must be boolean")
        libvirt = LibvirtConfig(str(libvirt_raw.get("uri", "qemu:///system")),
                                allow_mutation)
        raw_destinations = storage_raw.get("destinations")
        if not isinstance(raw_destinations, list) or not raw_destinations:
            raise ConfigError("storage.destinations must contain at least one destination")
        destinations = []
        for index, item in enumerate(raw_destinations):
            if not isinstance(item, dict):
                raise ConfigError(f"storage destination {index} must be a table")
            user, group = item.get("backup_data_user"), item.get("backup_data_group")
            try:
                uid = int(user_lookup(str(user)).pw_uid) if user is not None else None
            except KeyError as exc:
                raise ConfigError(f"unknown backup data user: {user}") from exc
            try:
                gid = int(group_lookup(str(group)).gr_gid) if group is not None else None
            except KeyError as exc:
                raise ConfigError(f"unknown backup data group: {group}") from exc
            destination = StorageConfig(
                name=str(item.get("name", "")).strip(),
                control_root=_absolute(item["control_root"],
                                       f"storage.destinations[{index}].control_root"),
                backup_data_root=_absolute(item["backup_data_root"],
                                           f"storage.destinations[{index}].backup_data_root"),
                backup_data_mode=_mode(item.get("backup_data_mode", "0750"),
                                       f"storage.destinations[{index}].backup_data_mode"),
                backup_data_uid=uid, backup_data_gid=gid,
                minimum_free_bytes=int(item.get("minimum_free_bytes", 0)),
                minimum_free_percent=float(item.get("minimum_free_percent", 5)),
            )
            if not destination.name:
                raise ConfigError("storage destination name cannot be empty")
            if destination.control_root == destination.backup_data_root:
                raise ConfigError("control and backup data roots must differ")
            if (destination.minimum_free_bytes < 0
                    or not 0 <= destination.minimum_free_percent <= 100):
                raise ConfigError("invalid storage reserve")
            destinations.append(destination)
        names = [item.name for item in destinations]
        if len(names) != len(set(names)):
            raise ConfigError("storage destination names must be unique")
        default = str(storage_raw.get("default_destination", "")).strip()
        if default not in names:
            raise ConfigError("storage.default_destination must name a configured destination")
        return AppConfig(daemon, libvirt, StorageCatalogConfig(default, tuple(destinations)))
    except KeyError as exc:
        raise ConfigError(f"missing configuration key: {exc.args[0]}") from exc
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(str(exc)) from exc

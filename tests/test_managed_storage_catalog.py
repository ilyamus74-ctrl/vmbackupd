from __future__ import annotations

import pytest

from vmbackupd.models import Node, StorageDestination
from vmbackupd.repository import (
    DomainInvariantError,
    SQLiteRepository,
)
from vmbackupd.storage_prepare import probe_managed_storage_root


def test_missing_managed_storage_probe_is_non_mutating(tmp_path):
    filesystem = tmp_path / "filesystem"
    filesystem.mkdir()

    target = filesystem / "future-storage"

    result = probe_managed_storage_root(
        target,
        minimum_free_bytes=0,
        minimum_free_percent=0,
    )

    assert result["ok"] is True
    assert result["ready_to_prepare"] is True
    assert result["will_create"] is True
    assert result["backup_data_root_exists"] is False

    assert result["total_bytes"] > 0
    assert result["free_bytes"] >= 0
    assert result["usable_after_reserve_bytes"] >= 0

    # Test button must not create the target.
    assert not target.exists()


def test_storage_catalog_delete_preserves_filesystem(tmp_path):
    repository = SQLiteRepository()

    node = Node(name="delete-test")
    repository.add_node(node)

    default_root = tmp_path / "default"
    default_root.mkdir()

    default = StorageDestination(
        node_id=node.id,
        name="default",
        backup_data_root=str(default_root),
        is_default=True,
    )

    repository.create_storage_destination(
        default,
        make_default=True,
    )

    root = tmp_path / "remove-from-catalog"
    root.mkdir()

    marker = root / "must-survive.txt"
    marker.write_text("preserve")

    destination = StorageDestination(
        node_id=node.id,
        name="secondary",
        backup_data_root=str(root),
    )

    repository.create_storage_destination(destination)

    removed = repository.delete_storage_destination(
        node.id,
        destination.id,
    )

    assert removed.id == destination.id

    with pytest.raises(KeyError):
        repository.get_storage_destination(
            node.id,
            destination.id,
        )

    assert root.is_dir()
    assert marker.read_text() == "preserve"

    # Default destination must not be removable.
    with pytest.raises(DomainInvariantError) as caught:
        repository.delete_storage_destination(
            node.id,
            default.id,
        )

    assert str(caught.value) == "STORAGE_DELETE_DEFAULT"

    repository.close()

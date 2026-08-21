
from pathlib import Path

from vmbackupd.physical_delete_adapter_v2 import (
    PhysicalDeleteAdapter,
)



def test_physical_delete_removes_file(
    tmp_path,
):

    target = tmp_path / "bundle"

    target.write_text(
        "data"
    )


    adapter = PhysicalDeleteAdapter()


    result = adapter.delete_path(
        target
    )


    assert result["deleted"] is True

    assert not target.exists()



def test_physical_delete_removes_directory(
    tmp_path,
):

    target = tmp_path / "bundle"

    target.mkdir()

    (target / "disk.qcow2").write_text(
        "data"
    )


    adapter = PhysicalDeleteAdapter()


    result = adapter.delete_path(
        target
    )


    assert result["deleted"] is True

    assert not target.exists()

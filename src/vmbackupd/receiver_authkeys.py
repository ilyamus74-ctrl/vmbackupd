"""OpenSSH AuthorizedKeysCommand projection for vmbackupd receiver."""

from __future__ import annotations

import sys
from pathlib import Path

from .clock import SystemClock
from .ssh_receiver import SSHReceiverError, SSHReceiverRegistry


TRANSFER_USER = "vmbackupd-transfer"
RECEIVER_ROOT = Path("/var/lib/vmbackupd/receiver")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # Fail closed. This helper authorizes one dedicated account only.
    if args != [TRANSFER_USER]:
        return 0

    registry = SSHReceiverRegistry(
        RECEIVER_ROOT,
        SystemClock(),
    )

    try:
        sources = registry.list()
    except SSHReceiverError as exc:
        print(
            f"vmbackupd-authorized-keys: {exc.code}: {exc}",
            file=sys.stderr,
        )
        return 1

    for source in sources:
        # Defense in depth in addition to sshd DisableForwarding/PermitTTY.
        print(f"restrict {source['public_key']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

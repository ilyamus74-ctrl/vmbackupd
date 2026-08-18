"""Restricted SSH receiver session entry point.

SSH.3c.2 establishes the authenticated/restricted receiver boundary only.
Remote capacity preflight belongs to SSH.4 and data transfer to SSH.5.
"""

from __future__ import annotations

import json
import sys


PROTOCOL_VERSION = 1


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args:
        print(
            "vmbackupd-receiver-session: arguments are not accepted",
            file=sys.stderr,
        )
        return 64

    # Read-only protocol handshake. Do not claim transport readiness.
    result = {
        "service": "vmbackupd-receiver",
        "protocol_version": PROTOCOL_VERSION,
        "transport_ready": False,
        "preflight_ready": False,
    }

    print(
        json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

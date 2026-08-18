"""Fail-closed login shell for the vmbackupd-transfer system account."""

from __future__ import annotations

import os
import sys


SESSION = "/usr/libexec/vmbackupd-receiver-session"


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # sshd ForceCommand is invoked through the user's login shell:
    #
    #   shell -c /usr/libexec/vmbackupd-receiver-session
    #
    # Reject interactive use and every other command.
    if args != ["-c", SESSION]:
        print(
            "vmbackupd-transfer: restricted receiver account",
            file=sys.stderr,
        )
        return 126

    os.execv(
        SESSION,
        [SESSION],
    )

    return 126


if __name__ == "__main__":
    raise SystemExit(main())

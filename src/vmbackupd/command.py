"""Argv-only command execution boundary."""

from __future__ import annotations

import subprocess
import os
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult) -> None:
        super().__init__(
            f"command failed with exit {result.returncode}: {list(result.argv)!r}: "
            f"{result.stderr.strip()}"
        )
        self.result = result


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        args = tuple(str(arg) for arg in argv)
        try:
            completed = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, check=False,
                env={**os.environ, "LC_ALL": "C", "LANG": "C"},
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"command timed out: {list(args)!r}") from exc
        except OSError as exc:
            raise RuntimeError(f"cannot execute command {list(args)!r}: {exc}") from exc
        return CommandResult(args, completed.stdout, completed.stderr, completed.returncode)


class FakeCommandRunner:
    def __init__(
        self,
        responses: Mapping[tuple[str, ...], CommandResult | tuple[int, str, str]] | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.calls: list[tuple[tuple[str, ...], float | None]] = []

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        args = tuple(str(arg) for arg in argv)
        self.calls.append((args, timeout))
        response = self.responses.get(args)
        if response is None:
            return CommandResult(args, "", "fake command not configured", 1)
        if isinstance(response, CommandResult):
            return response
        returncode, stdout, stderr = response
        return CommandResult(args, stdout, stderr, returncode)

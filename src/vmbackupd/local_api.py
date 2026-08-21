"""Versioned bounded JSON-lines protocol over a local UNIX stream socket."""

from __future__ import annotations

import asyncio
import errno
import json
import socket
import stat
from pathlib import Path
from uuid import uuid4

from .application import ApplicationError


API_VERSION = 1
DEFAULT_MAX_REQUEST_BYTES = 64 * 1024


class ApiServer:
    def __init__(self, application, socket_path: Path, socket_mode: int,
                 max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
                 socket_probe=None) -> None:
        self.application, self.socket_path, self.socket_mode = application, socket_path, socket_mode
        self.max_request_bytes = max_request_bytes
        self.socket_probe = socket_probe or self._probe_socket
        self.server: asyncio.AbstractServer | None = None
        self._owned = False

    @staticmethod
    def _reject_symlinks(path: Path) -> None:
        for candidate in (*reversed(path.parents), path.parent):
            if candidate.is_symlink():
                raise RuntimeError(f"unsafe symlink in socket path: {candidate}")

    async def start(self) -> None:
        if not self.socket_path.is_absolute():
            raise RuntimeError("socket path must be absolute")
        self._reject_symlinks(self.socket_path)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlinks(self.socket_path)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            mode = self.socket_path.lstat().st_mode
            if not stat.S_ISSOCK(mode):
                raise RuntimeError("socket path exists and is not a UNIX socket")
            result = self.socket_probe(str(self.socket_path))
            if result == 0:
                raise RuntimeError("a live listener already owns the socket")
            if result not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise RuntimeError(f"socket ownership probe was ambiguous: errno {result}")
            self.socket_path.unlink()
        self.server = await asyncio.start_unix_server(
            self._client, path=str(self.socket_path), limit=self.max_request_bytes + 1
        )
        self._owned = True
        self.socket_path.chmod(self.socket_mode)

    @staticmethod
    def _probe_socket(path: str) -> int:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.1)
            return probe.connect_ex(path)
        finally:
            probe.close()

    async def stop(self) -> None:
        await self.stop_accepting()
        self.remove_socket()

    async def stop_accepting(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    def remove_socket(self) -> None:
        if self._owned and self.socket_path.exists() and stat.S_ISSOCK(self.socket_path.lstat().st_mode):
            self.socket_path.unlink()
        self._owned = False

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                data = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                await self._write(writer, self._error(None, "REQUEST_TOO_LARGE", "request exceeds limit"))
                return
            if len(data) > self.max_request_bytes:
                await self._write(writer, self._error(None, "REQUEST_TOO_LARGE", "request exceeds limit"))
                return
            try:
                request = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError):
                await self._write(writer, self._error(None, "MALFORMED_JSON", "invalid JSON request"))
                return
            request_id = request.get("id") if isinstance(request, dict) else None
            if not isinstance(request, dict) or request.get("version") != API_VERSION:
                await self._write(writer, self._error(request_id, "UNSUPPORTED_VERSION", "protocol version 1 required"))
                return
            if not isinstance(request.get("method"), str) or not isinstance(request.get("params", {}), dict):
                await self._write(writer, self._error(request_id, "INVALID_REQUEST", "method and params are required"))
                return
            try:
                result = self.application.dispatch(request["method"], request.get("params", {}))
                response = {"version": API_VERSION, "id": request_id, "ok": True, "result": result}
            except ApplicationError as exc:
                response = self._error(request_id, exc.code, str(exc))
            except Exception as exc:
                import traceback
                traceback.print_exc()
                response = self._error(
                    request_id,
                    "INTERNAL_ERROR",
                    f"{type(exc).__name__}: {exc}",
                )
            await self._write(writer, response)
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    def _error(request_id, code, message):
        return {"version": API_VERSION, "id": request_id, "ok": False,
                "error": {"code": code, "message": message}}

    @staticmethod
    async def _write(writer, value):
        writer.write((json.dumps(value, sort_keys=True) + "\n").encode())
        await writer.drain()


class ApiClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ApiUnavailable(RuntimeError):
    pass


class ApiClient:
    def __init__(self, socket_path: str | Path, timeout: float = 5) -> None:
        self.socket_path, self.timeout = Path(socket_path), timeout

    def request(self, method: str, params: dict | None = None):
        request_id = str(uuid4())
        payload = json.dumps({"version": API_VERSION, "id": request_id,
                              "method": method, "params": params or {}}).encode() + b"\n"
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(str(self.socket_path))
            connection.sendall(payload)
            chunks = bytearray()
            while not chunks.endswith(b"\n"):
                part = connection.recv(65536)
                if not part:
                    break
                chunks.extend(part)
        except OSError as exc:
            raise ApiUnavailable(str(exc)) from exc
        finally:
            connection.close()
        try:
            response = json.loads(chunks)
        except json.JSONDecodeError as exc:
            raise ApiUnavailable("daemon returned malformed response") from exc
        if not response.get("ok"):
            error = response.get("error", {})
            raise ApiClientError(error.get("code", "INTERNAL_ERROR"), error.get("message", "request failed"))
        return response["result"]

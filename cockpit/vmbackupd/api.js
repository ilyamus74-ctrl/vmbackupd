(function (global) {
    "use strict";

    const SOCKET_PATH = "/run/vmbackupd/vmbackupd.sock";
    const PROTOCOL_VERSION = 1;
    const MAX_RESPONSE_BYTES = 1024 * 1024;
    const API_REQUEST_TIMEOUT_MS = 45000;
    const ALLOWED_METHODS = Object.freeze([
        "daemon.status",
        "vm.discover",
        "vm.inventory",
        "vm.registered.list",
        "vm.list",
        "vm.inventory",
        "vm.registered.list",
        "storage.list",
        "storage.create",
        "storage.update",
        "storage.delete",
        "storage.set_default",
        "storage.test",
        "ssh.identity.show",
        "ssh.identity.generate",
        "ssh.identity.rotate",
        "ssh.hostkey.show",
        "ssh.hostkey.add",
        "ssh.hostkey.revoke",
        "ssh.hostkey.endpoint.show",
        "ssh.hostkey.endpoint.add",
        "ssh.hostkey.endpoint.revoke",
        "ssh.storage.discover",
        "receiver.info",
        "receiver.key.list",
        "receiver.key.add",
        "receiver.key.revoke",
        "job.list",
        "run.list",
        "restore_point.list",
        "restore_point.delete",
        "replica.retry",
        "received.list",
        "received.delete",
        "received.restore.create",
        "restore.list",
        "recovery.list",
        "vm.register",
        "job.create",
        "job.update",
        "backup.run",
    ]);
    let requestSequence = 0;
    let forcePrivilegedTransport = false;
    const adminPermission = global.cockpit && typeof global.cockpit.permission === "function" ?
        global.cockpit.permission({ admin: true }) : null;
    const adminChangedListeners = new Set();

    if (adminPermission && typeof adminPermission.addEventListener === "function") {
        adminPermission.addEventListener("changed", () => {
            if (!adminPermission.allowed)
                forcePrivilegedTransport = false;
            for (const listener of adminChangedListeners)
                listener(Boolean(adminPermission.allowed));
        });
    }

    class TransportError extends Error {}
    class ProtocolError extends Error {}
    class ApiError extends Error {
        constructor(code, message) {
            super(message);
            this.code = code;
        }
    }

    function requestId() {
        if (global.crypto && typeof global.crypto.randomUUID === "function")
            return global.crypto.randomUUID();
        requestSequence += 1;
        return `cockpit-${Date.now()}-${requestSequence}`;
    }

    function validatedEnvelope(response, id) {
        if (response === null || typeof response !== "object" || Array.isArray(response))
            throw new ProtocolError("API response must be an object");
        if (response.version !== PROTOCOL_VERSION)
            throw new ProtocolError("API response version mismatch");
        if (response.id !== id)
            throw new ProtocolError("API response ID mismatch");
        if (typeof response.ok !== "boolean")
            throw new ProtocolError("API response ok must be boolean");
        if (response.ok) {
            if (!Object.prototype.hasOwnProperty.call(response, "result"))
                throw new ProtocolError("API success response has no result member");
            return { result: response.result, error: null };
        }
        const error = response.error;
        if (error === null || typeof error !== "object" || Array.isArray(error))
            throw new ProtocolError("API error response has an invalid error object");
        if (typeof error.code !== "string" || error.code.trim() === "")
            throw new ProtocolError("API error response has an invalid code");
        if (typeof error.message !== "string")
            throw new ProtocolError("API error response has an invalid message");
        return { result: undefined, error: new ApiError(error.code, error.message) };
    }

    function directRequest(method, params = {}) {
        if (!ALLOWED_METHODS.includes(method))
            return Promise.reject(new ApiError("METHOD_NOT_ALLOWED", "Frontend method is not allowed"));

        const id = requestId();
        const requestLine = JSON.stringify({
            version: PROTOCOL_VERSION,
            id: id,
            method: method,
            params: params,
        }) + "\n";

        return new Promise((resolve, reject) => {
            const channel = global.cockpit.channel({
                payload: "stream",
                unix: SOCKET_PATH,
            });
            let buffer = "";
            let trailing = "";
            let receivedBytes = 0;
            let recordComplete = false;
            let storedResult;
            let storedError = null;
            let settled = false;
            let timer = null;

            function clearTimer() {
                if (timer !== null) {
                    global.clearTimeout(timer);
                    timer = null;
                }
            }

            function abort(error) {
                if (settled)
                    return;
                settled = true;
                clearTimer();
                channel.close();
                reject(error);
            }

            channel.addEventListener("message", (_event, chunk) => {
                if (settled)
                    return;
                receivedBytes += new TextEncoder().encode(chunk).byteLength;
                if (receivedBytes > MAX_RESPONSE_BYTES) {
                    abort(new ProtocolError("API response exceeds buffer limit"));
                    return;
                }
                if (recordComplete) {
                    trailing += chunk;
                    if (trailing.trim() !== "")
                        abort(new ProtocolError("API returned data after its response record"));
                    return;
                }
                buffer += chunk;
                const newline = buffer.indexOf("\n");
                if (newline === -1)
                    return;
                const line = buffer.slice(0, newline);
                trailing = buffer.slice(newline + 1);
                let response;
                try {
                    response = JSON.parse(line);
                } catch (_error) {
                    abort(new ProtocolError("API returned malformed JSON"));
                    return;
                }
                try {
                    const validated = validatedEnvelope(response, id);
                    storedResult = validated.result;
                    storedError = validated.error;
                    recordComplete = true;
                } catch (error) {
                    abort(error);
                    return;
                }
                if (trailing.trim() !== "")
                    abort(new ProtocolError("API returned data after its response record"));
            });

            channel.addEventListener("close", (_event, options) => {
                if (settled)
                    return;
                settled = true;
                clearTimer();
                if (options && options.problem) {
                    reject(new TransportError(`API channel failed: ${options.problem}`));
                    return;
                }
                if (!recordComplete) {
                    reject(new TransportError("API channel closed before a complete response"));
                    return;
                }
                if (trailing.trim() !== "") {
                    reject(new ProtocolError("API returned data after its response record"));
                    return;
                }
                if (storedError !== null)
                    reject(storedError);
                else
                    resolve(storedResult);
            });

            timer = global.setTimeout(() => {
                abort(new TransportError("API request timed out"));
            }, API_REQUEST_TIMEOUT_MS);
            channel.send(requestLine);
        });
    }

    function adminAllowed() {
        return Boolean(adminPermission && adminPermission.allowed);
    }

    function privilegedSpawn(args, input = null, superuser = "require", requireAdmin = true) {
        if (requireAdmin && !adminAllowed())
            return Promise.reject(new ApiError(
                "ADMIN_REQUIRED",
                "Cockpit Administrative access is required",
            ));
        const process = global.cockpit.spawn(args, {
            superuser: superuser,
            err: "message",
        });
        if (input !== null)
            process.input(input);
        return process;
    }

    async function privilegedRequest(method, params = {}) {
        if (!ALLOWED_METHODS.includes(method))
            throw new ApiError("METHOD_NOT_ALLOWED", "Frontend method is not allowed");
        const id = requestId();
        const requestLine = JSON.stringify({
            version: PROTOCOL_VERSION,
            id: id,
            method: method,
            params: params,
        }) + "\n";
        let output;
        try {
            output = await privilegedSpawn(
                ["/usr/libexec/vmbackupd-cockpit-helper", "relay"],
                requestLine,
                "try",
                false,
            );
        } catch (error) {
            const detail = error && error.message ? `: ${error.message}` : "";
            throw new TransportError(`privileged API relay failed${detail}`);
        }
        const lines = String(output).split("\n");
        if (lines.length < 2 || lines.slice(1).some(line => line.trim() !== ""))
            throw new ProtocolError("privileged API relay returned an invalid record");
        let response;
        try {
            response = JSON.parse(lines[0]);
        } catch (_error) {
            throw new ProtocolError("privileged API relay returned malformed JSON");
        }
        const validated = validatedEnvelope(response, id);
        if (validated.error)
            throw validated.error;
        return validated.result;
    }

    async function request(method, params = {}) {
        if (forcePrivilegedTransport && adminAllowed())
            return privilegedRequest(method, params);
        try {
            return await directRequest(method, params);
        } catch (error) {
            if (!(error instanceof TransportError))
                throw error;
            forcePrivilegedTransport = true;
            return privilegedRequest(method, params);
        }
    }

    async function setMutation(enabled) {
        if (typeof enabled !== "boolean")
            throw new ApiError("INVALID_PARAMS", "Mutation state must be boolean");
        let output;
        try {
            output = await privilegedSpawn([
                "/usr/libexec/vmbackupd-cockpit-helper",
                "mutation-set",
                enabled ? "true" : "false",
            ]);
        } catch (error) {
            if (error instanceof ApiError)
                throw error;
            const detail = error && error.message ? `: ${error.message}` : "";
            throw new TransportError(`mutation update failed${detail}`);
        }
        try {
            const result = JSON.parse(String(output).trim());
            if (!result || result.libvirt_mutation_enabled !== enabled)
                throw new Error("state mismatch");
            return result;
        } catch (_error) {
            throw new ProtocolError("mutation helper returned malformed state");
        }
    }

    function onAdminChanged(listener) {
        if (typeof listener !== "function")
            return () => {};
        adminChangedListeners.add(listener);
        return () => adminChangedListeners.delete(listener);
    }

    global.VmbackupApi = Object.freeze({
        request: request,
        setMutation: setMutation,
        adminAllowed: adminAllowed,
        onAdminChanged: onAdminChanged,
        methods: ALLOWED_METHODS,
        ApiError: ApiError,
        ProtocolError: ProtocolError,
        TransportError: TransportError,
    });
}(window));

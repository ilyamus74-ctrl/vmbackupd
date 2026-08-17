(function (global) {
    "use strict";

    const SOCKET_PATH = "/run/vmbackupd/vmbackupd.sock";
    const PROTOCOL_VERSION = 1;
    const MAX_RESPONSE_BYTES = 1024 * 1024;
    const API_REQUEST_TIMEOUT_MS = 45000;
    const READ_ONLY_METHODS = Object.freeze([
        "daemon.status",
        "vm.discover",
        "vm.list",
        "storage.list",
        "job.list",
        "run.list",
        "restore_point.list",
        "recovery.list",
    ]);
    let requestSequence = 0;

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

    function request(method) {
        if (!READ_ONLY_METHODS.includes(method))
            return Promise.reject(new ApiError("METHOD_NOT_ALLOWED", "Frontend method is not allowed"));

        const id = requestId();
        const requestLine = JSON.stringify({
            version: PROTOCOL_VERSION,
            id: id,
            method: method,
            params: {},
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

    global.VmbackupApi = Object.freeze({
        request: request,
        methods: READ_ONLY_METHODS,
        ApiError: ApiError,
        ProtocolError: ProtocolError,
        TransportError: TransportError,
    });
}(window));

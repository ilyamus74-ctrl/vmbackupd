from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
API = ROOT / "cockpit" / "vmbackupd" / "api.js"


def test_admin_fallback_relays_api_and_mutation_through_privileged_helper():
    script = r'''
const fs = require("fs");
const vm = require("vm");
const crypto = require("crypto");
const calls = [];
const permission = {
  allowed: true,
  listeners: {},
  addEventListener(name, callback) { this.listeners[name] = callback; },
};
function channel() {
  const listeners = {};
  return {
    addEventListener(name, cb) { listeners[name] = cb; },
    close() {},
    send(_line) { queueMicrotask(() => listeners.close({}, { problem: "terminated" })); },
  };
}
function spawn(args, options) {
  let input = "";
  const promise = new Promise((resolve) => {
    queueMicrotask(() => {
      calls.push([args, options, input]);
      if (args[1] === "relay") {
        const request = JSON.parse(input);
        resolve(JSON.stringify({version: 1, id: request.id, ok: true, result: {runtime_state:"RUNNING"}}) + "\n");
      } else {
        resolve(JSON.stringify({libvirt_mutation_enabled: args[2] === "true"}));
      }
    });
  });
  promise.input = value => { input += value; return promise; };
  return promise;
}
const context = {
  console, TextEncoder, setTimeout, clearTimeout,
  crypto: crypto.webcrypto,
  cockpit: { channel, spawn, permission: () => permission },
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
(async () => {
  const status = await context.VmbackupApi.request("daemon.status");
  if (status.runtime_state !== "RUNNING") throw new Error("privileged relay result lost");
  const relay = calls.find(call => call[0][1] === "relay");
  if (!relay || relay[0][0] !== "/usr/libexec/vmbackupd-cockpit-helper") throw new Error("relay helper missing");
  if (relay[1].superuser !== "try") throw new Error("relay did not request Cockpit superuser transport");
  const toggled = await context.VmbackupApi.setMutation(false);
  if (toggled.libvirt_mutation_enabled !== false) throw new Error("mutation helper result lost");
  const mutation = calls.find(call => call[0][1] === "mutation-set");
  if (!mutation || mutation[0][2] !== "false" || mutation[1].superuser !== "require")
    throw new Error("mutation helper was not privileged");
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    completed = subprocess.run(
        ["node", "-e", script, str(API)],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_non_admin_cannot_use_mutation_helper():
    script = r'''
const fs = require("fs");
const vm = require("vm");
const crypto = require("crypto");
const permission = { allowed: false, addEventListener() {} };
const context = {
  console, TextEncoder, setTimeout, clearTimeout,
  crypto: crypto.webcrypto,
  cockpit: { permission: () => permission, channel() { throw new Error("unused"); }, spawn() { throw new Error("must not spawn"); } },
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
(async () => {
  try {
    await context.VmbackupApi.setMutation(true);
    throw new Error("non-admin mutation unexpectedly succeeded");
  } catch (error) {
    if (error.code !== "ADMIN_REQUIRED") throw error;
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    completed = subprocess.run(
        ["node", "-e", script, str(API)], cwd=ROOT, text=True, capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr

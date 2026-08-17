"""vmbackupctl: UNIX API client only."""

from __future__ import annotations

import argparse
import json
import sys

from .local_api import ApiClient, ApiClientError, ApiUnavailable


DEFAULT_SOCKET = "/run/vmbackupd/vmbackupd.sock"


def _parser():
    parser = argparse.ArgumentParser(prog="vmbackupctl")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--json", action="store_true")
    top = parser.add_subparsers(dest="group", required=True)
    simple = {"daemon": ["status"], "node": ["list"], "storage": ["list", "show"],
              "vm": ["discover", "list", "show", "register"],
              "job": ["list", "show", "create"], "backup": ["run"],
              "run": ["list", "show"], "restore-point": ["list", "show"],
              "recovery": ["list", "show"], "event": ["list"]}
    for group, commands in simple.items():
        group_parser = top.add_parser(group)
        subs = group_parser.add_subparsers(dest="command", required=True)
        for command in commands:
            item = subs.add_parser(command)
            if command == "show": item.add_argument("id")
            if group == "vm" and command == "register":
                item.add_argument("domain"); item.add_argument("--name")
            if group == "job" and command == "create":
                item.add_argument("--vm", required=True); item.add_argument("--name", required=True)
                item.add_argument("--storage"); item.add_argument("--storage-name")
                item.add_argument("--max-incrementals", type=int, default=0)
                item.add_argument("--retain", type=int, default=7)
                item.add_argument("--minimum-full-chains", type=int, default=1)
                item.add_argument("--interval", type=int, default=3600)
                item.add_argument("--misfire-grace", type=int, default=0)
            if group == "backup" and command == "run": item.add_argument("job_id")
            if group == "event" and command == "list": item.add_argument("--run")
    return parser


def _request(args):
    method = f"{args.group.replace('-', '_')}.{args.command}"
    params = {}
    if args.command == "show":
        params["run_id" if args.group == "recovery" else "id"] = args.id
    elif args.group == "vm" and args.command == "register":
        params = {"external_id": args.domain, "name": args.name}
    elif args.group == "job" and args.command == "create":
        params = {"vm_id": args.vm, "name": args.name,
                  "storage_destination_id": args.storage,
                  "storage_destination": args.storage_name,
                  "max_incrementals_per_chain": args.max_incrementals,
                  "restore_points_to_retain": args.retain,
                  "minimum_full_chains": args.minimum_full_chains,
                  "interval_seconds": args.interval,
                  "misfire_grace_seconds": args.misfire_grace}
    elif args.group == "backup": params = {"job_id": args.job_id}
    elif args.group == "event" and args.run: params = {"run_id": args.run}
    return method, params


def main(argv=None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        method, params = _request(args)
        result = ApiClient(args.socket).request(method, params)
        if args.json:
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        else:
            print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ApiUnavailable as exc:
        print(f"vmbackupctl: daemon unavailable: {exc}", file=sys.stderr); return 3
    except ApiClientError as exc:
        print(f"vmbackupctl: {exc.code}: {exc}", file=sys.stderr)
        return 5 if exc.code == "INTERNAL_ERROR" else 4
    except (ValueError, KeyError) as exc:
        print(f"vmbackupctl: {exc}", file=sys.stderr); return 2

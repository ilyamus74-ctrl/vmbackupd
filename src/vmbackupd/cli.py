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
    simple = {"daemon": ["status"], "node": ["list"],
              "storage": ["list", "show", "create", "update", "set-default", "test"],
              "vm": ["discover", "list", "show", "register"],
              "job": ["list", "show", "create", "update"], "backup": ["run"],
              "run": ["list", "show"], "restore-point": ["list", "show"],
              "recovery": ["list", "show"], "event": ["list"]}
    for group, commands in simple.items():
        group_parser = top.add_parser(group)
        subs = group_parser.add_subparsers(dest="command", required=True)
        for command in commands:
            item = subs.add_parser(command)
            if command == "show": item.add_argument("id")
            if group == "storage" and command == "create":
                item.add_argument("--name", required=True)
                item.add_argument("--backup-data-root", required=True)
                item.add_argument(
                    "--storage-type",
                    choices=("LOCAL", "SSH"),
                    default="LOCAL",
                )
                item.add_argument("--ssh-host")
                item.add_argument("--ssh-port", type=int)
                item.add_argument("--ssh-user")
                item.add_argument("--ssh-remote-root")
                item.add_argument("--minimum-free-bytes", type=int, default=0)
                item.add_argument("--minimum-free-percent", type=float, default=5)
                item.add_argument("--default", action="store_true")
            if group == "storage" and command == "update":
                item.add_argument("id"); item.add_argument("--name")
                item.add_argument("--backup-data-root")
                item.add_argument(
                    "--storage-type",
                    choices=("LOCAL", "SSH"),
                )
                item.add_argument("--ssh-host")
                item.add_argument("--ssh-port", type=int)
                item.add_argument("--ssh-user")
                item.add_argument("--ssh-remote-root")
                item.add_argument("--minimum-free-bytes", type=int)
                item.add_argument("--minimum-free-percent", type=float)
                item.add_argument("--default", action="store_true")
            if group == "storage" and command in {"set-default", "test"}:
                item.add_argument("id")
            if group == "vm" and command == "register":
                item.add_argument("domain"); item.add_argument("--name")
            if group == "job" and command == "create":
                item.add_argument("--vm", required=True); item.add_argument("--name", required=True)
                destination = item.add_mutually_exclusive_group()
                destination.add_argument("--storage"); destination.add_argument("--storage-name")
                item.add_argument("--max-incrementals", type=int, default=0)
                item.add_argument("--retain", type=int, default=7)
                item.add_argument("--full-chains-to-retain", type=int, default=2)
                item.add_argument("--minimum-full-chains", type=int, default=1)
                item.add_argument(
                    "--space-reclaim-mode",
                    choices=("SAFE", "SPACE_OPTIMIZED"),
                    default="SAFE",
                )
                item.add_argument(
                    "--backup-size-margin-percent", type=float, default=20.0
                )
                item.add_argument("--interval", type=int, default=3600)
                item.add_argument("--misfire-grace", type=int, default=0)
                item.add_argument(
                    "--schedule-type",
                    choices=("INTERVAL", "DAILY"),
                    default="INTERVAL",
                )
                item.add_argument("--daily-time")
                item.add_argument("--schedule-timezone")
                item.add_argument("--schedule", action="store_true")
                item.add_argument("--disabled", action="store_true")
            if group == "job" and command == "update":
                item.add_argument("id"); item.add_argument("--name")
                destination = item.add_mutually_exclusive_group()
                destination.add_argument("--storage"); destination.add_argument("--storage-name")
                item.add_argument("--retain", type=int)
                item.add_argument("--full-chains-to-retain", type=int)
                item.add_argument("--minimum-full-chains", type=int)
                item.add_argument(
                    "--space-reclaim-mode",
                    choices=("SAFE", "SPACE_OPTIMIZED"),
                )
                item.add_argument("--backup-size-margin-percent", type=float)
                item.add_argument("--interval", type=int)
                item.add_argument("--misfire-grace", type=int)
                item.add_argument(
                    "--schedule-type",
                    choices=("INTERVAL", "DAILY"),
                )
                item.add_argument("--daily-time")
                item.add_argument("--schedule-timezone")
                enabled = item.add_mutually_exclusive_group()
                enabled.add_argument("--enable", action="store_true")
                enabled.add_argument("--disable", action="store_true")
                schedule = item.add_mutually_exclusive_group()
                schedule.add_argument("--schedule", action="store_true")
                schedule.add_argument("--manual", action="store_true")
            if group == "backup" and command == "run": item.add_argument("job_id")
            if group == "event" and command == "list": item.add_argument("--run")
    return parser


def _request(args):
    method = f"{args.group.replace('-', '_')}.{args.command.replace('-', '_')}"
    params = {}
    if args.command == "show":
        params["run_id" if args.group == "recovery" else "id"] = args.id
    elif args.group == "storage" and args.command == "create":
        params = {"name": args.name, "backup_data_root": args.backup_data_root,
                  "minimum_free_bytes": args.minimum_free_bytes,
                  "minimum_free_percent": args.minimum_free_percent,
                  "make_default": args.default}
        if args.storage_type == "SSH":
            params.update({
                "storage_type": args.storage_type,
                "ssh_host": args.ssh_host,
                "ssh_port": args.ssh_port,
                "ssh_user": args.ssh_user,
                "ssh_remote_root": args.ssh_remote_root,
            })
    elif args.group == "storage" and args.command == "update":
        params = {"id": args.id, "name": args.name,
                  "backup_data_root": args.backup_data_root,
                  "minimum_free_bytes": args.minimum_free_bytes,
                  "minimum_free_percent": args.minimum_free_percent,
                  "make_default": args.default}
        for key, value in (
            ("storage_type", args.storage_type),
            ("ssh_host", args.ssh_host),
            ("ssh_port", args.ssh_port),
            ("ssh_user", args.ssh_user),
            ("ssh_remote_root", args.ssh_remote_root),
        ):
            if value is not None:
                params[key] = value
    elif args.group == "storage" and args.command in {"set-default", "test"}:
        params = {"id": args.id}
    elif args.group == "vm" and args.command == "register":
        params = {"external_id": args.domain, "name": args.name}
    elif args.group == "job" and args.command == "create":
        params = {"vm_id": args.vm, "name": args.name,
                  "storage_destination_id": args.storage,
                  "storage_destination": args.storage_name,
                  "max_incrementals_per_chain": args.max_incrementals,
                  "restore_points_to_retain": args.retain,
                  "full_chains_to_retain": args.full_chains_to_retain,
                  "minimum_full_chains": args.minimum_full_chains,
                  "space_reclaim_mode": args.space_reclaim_mode,
                  "backup_size_margin_percent": args.backup_size_margin_percent,
                  "interval_seconds": args.interval,
                  "misfire_grace_seconds": args.misfire_grace,
                  "schedule_type": args.schedule_type,
                  "daily_time": args.daily_time,
                  "schedule_timezone": args.schedule_timezone,
                  "schedule_enabled": args.schedule,
                  "enabled": not args.disabled}
    elif args.group == "job" and args.command == "update":
        params = {"id": args.id, "name": args.name,
                  "storage_destination_id": args.storage,
                  "storage_destination": args.storage_name,
                  "restore_points_to_retain": args.retain,
                  "full_chains_to_retain": args.full_chains_to_retain,
                  "minimum_full_chains": args.minimum_full_chains,
                  "space_reclaim_mode": args.space_reclaim_mode,
                  "backup_size_margin_percent": args.backup_size_margin_percent,
                  "interval_seconds": args.interval,
                  "misfire_grace_seconds": args.misfire_grace,
                  "schedule_type": args.schedule_type,
                  "daily_time": args.daily_time,
                  "schedule_timezone": args.schedule_timezone,
                  "enabled": True if args.enable else False if args.disable else None,
                  "schedule_enabled": True if args.schedule else False if args.manual else None}
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

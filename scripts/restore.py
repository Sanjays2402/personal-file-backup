#!/usr/bin/env python3
"""Restore a backed-up file from the versioned backup bucket.

Two subcommands:

    list     Show every version of a key (newest first), with size, timestamp
             and which version is current.
    restore  Copy one chosen version back — into the inbox bucket, or any
             target bucket/key you name — so a mistaken overwrite can be
             undone in seconds.

Credentials: uses your normal AWS CLI configuration (aws configure /
environment / SSO). Needs ``s3:ListBucketVersions``, ``s3:GetObjectVersion``
on the backup bucket and ``s3:PutObject`` on the destination.

Examples:
    python scripts/restore.py list my-backup-bucket docs/report.pdf
    python scripts/restore.py restore my-backup-bucket docs/report.pdf v3XYZ \\
        --to-bucket my-inbox --to-key docs/report-restored.pdf
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys

import boto3


def _fmt_time(value) -> str:
    if isinstance(value, _dt.datetime):
        return value.astimezone(_dt.timezone.utc).isoformat(timespec="seconds")
    return str(value)


def list_versions(s3, bucket: str, key: str) -> list[dict]:
    """Return every version of ``key`` in ``bucket``, newest first.

    Each dict has: VersionId, LastModified (ISO-8601 UTC string), Size,
    IsLatest, IsDeleteMarker. Delete markers are excluded — there is no
    data to restore from them.
    """
    paginator = s3.get_paginator("list_object_versions")
    versions: list[dict] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=key):
        for entry in page.get("Versions", []):
            if entry.get("Key") != key:
                continue  # same prefix, different key
            versions.append({
                "VersionId": entry.get("VersionId"),
                "LastModified": _fmt_time(entry.get("LastModified")),
                "Size": entry.get("Size", 0),
                "IsLatest": bool(entry.get("IsLatest", False)),
                "IsDeleteMarker": False,
            })
    versions.sort(key=lambda v: v["LastModified"], reverse=True)
    return versions


def restore_version(s3, backup_bucket: str, key: str, version_id: str,
                    to_bucket: str, to_key: str | None = None) -> dict:
    """Copy one version from the backup bucket to ``to_bucket``.

    Returns the ``copy_object`` response dict. Raises if the version does
    not exist (the copy fails) — the caller reports the error.
    """
    target_key = to_key or key
    response = s3.copy_object(
        Bucket=to_bucket,
        Key=target_key,
        CopySource={"Bucket": backup_bucket, "Key": key, "VersionId": version_id},
    )
    return response


def _cmd_list(s3, args) -> int:
    versions = list_versions(s3, args.bucket, args.key)
    if not versions:
        print(f"No versions found for '{args.key}' in bucket '{args.bucket}'.")
        return 1
    print(f"{len(versions)} version(s) of '{args.key}':")
    for v in versions:
        latest = "  <-- current" if v["IsLatest"] else ""
        size = f"{v['Size']:,}"
        print(f"  {v['VersionId']:<40} {v['LastModified']}  {size:>12} bytes{latest}")
    return 0


def _cmd_restore(s3, args) -> int:
    resp = restore_version(s3, args.bucket, args.key, args.version_id,
                           args.to_bucket, args.to_key)
    target = f"s3://{args.to_bucket}/{args.to_key or args.key}"
    new_version = resp.get("VersionId", "(unversioned)")
    print(f"Restored version {args.version_id} of '{args.key}' -> {target}")
    print(f"New version in target: {new_version}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List versions of a backed-up file and restore one.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List all versions of a key.")
    p_list.add_argument("bucket", help="Backup bucket name.")
    p_list.add_argument("key", help="Object key to inspect.")
    p_list.set_defaults(func=_cmd_list)

    p_restore = sub.add_parser("restore", help="Restore one version to a bucket.")
    p_restore.add_argument("bucket", help="Backup bucket name.")
    p_restore.add_argument("key", help="Object key to restore.")
    p_restore.add_argument("version_id", help="Version ID to restore.")
    p_restore.add_argument("--to-bucket", required=True,
                           help="Destination bucket (e.g. the inbox bucket).")
    p_restore.add_argument("--to-key", default=None,
                           help="Destination key (defaults to the original key).")
    p_restore.set_defaults(func=_cmd_restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    s3 = boto3.client("s3")
    try:
        return args.func(s3, args)
    except Exception as exc:  # noqa: BLE001 - CLI: print the error, exit 2
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

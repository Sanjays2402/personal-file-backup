"""Personal File Backup — Lambda handler.

Event-driven backup pipeline: every object uploaded to the *inbox* (source)
S3 bucket is copied to the *backup* bucket, then the owner receives an SNS
email receipt ("File Backed Up").

Design notes
------------
* Failed copies raise: S3 event delivery retries the record, so nothing is
  silently skipped.
* Copies are idempotent — reprocessing the same record (duplicate events,
  retries) simply overwrites the same backup key, so retries are safe.
* All logs are single-line structured JSON for CloudWatch Logs Insights.
* Object metadata is preserved (MetadataDirective=COPY); we add S3 tags
  ``copied-from`` / ``copied-at`` so a backup object is always traceable
  back to its source.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import urllib.parse

import boto3

SERVICE = "personal-file-backup"

BACKUP_BUCKET_NAME = os.environ.get("BACKUP_BUCKET_NAME", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
# Empty string => SSE-S3 (AES256); otherwise SSE-KMS with this key ID/ARN.
SSEKMS_KEY_ID = os.environ.get("SSEKMS_KEY_ID", "")


# Lazily-created clients: boto3.client() must not run at import time (its
# credential/region lookup performs network I/O). Importing this module
# therefore needs no AWS credentials; tests patch the _s3/_sns caches below.
_s3 = None
_sns = None


def s3():
    """Return the S3 client, creating it on first use."""
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def sns():
    """Return the SNS client, creating it on first use."""
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _log(event_name: str, **fields) -> None:
    """Emit one structured JSON log line (CloudWatch-friendly)."""
    payload = {"service": SERVICE, "ts": _now_iso(), "event": event_name}
    payload.update(fields)
    print(json.dumps(payload, default=str))


def _parse_record(record: dict) -> tuple[str, str]:
    """Extract (bucket, url-decoded key) from one S3 event record."""
    s3info = record.get("s3", {}) or {}
    bucket = (s3info.get("bucket", {}) or {}).get("name")
    raw_key = (s3info.get("object", {}) or {}).get("key")
    if not bucket or not raw_key:
        raise ValueError(f"S3 record missing bucket/key: {record!r}")
    return bucket, urllib.parse.unquote_plus(raw_key)


def _object_size(source_bucket: str, key: str) -> int | None:
    """Best-effort size lookup for the receipt email; backup must not depend on it."""
    try:
        head = s3().head_object(Bucket=source_bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - logged, non-fatal
        _log("head_failed", source_bucket=source_bucket, key=key,
             error=str(exc), error_type=type(exc).__name__)
        return None
    return head.get("ContentLength")


def _encryption_kwargs() -> dict:
    """SSE params for copy_object: SSE-KMS when a key is configured, else SSE-S3."""
    if SSEKMS_KEY_ID:
        return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": SSEKMS_KEY_ID}
    return {"ServerSideEncryption": "AES256"}


def _copy_object(source_bucket: str, key: str) -> tuple[str, str | None]:
    """Copy the object into the backup bucket; raises on any failure."""
    copied_at = _now_iso()
    tags = (
        "copied-from=" + urllib.parse.quote(source_bucket, safe="")
        + "&copied-at=" + urllib.parse.quote(copied_at, safe="")
    )
    try:
        response = s3().copy_object(
            Bucket=BACKUP_BUCKET_NAME,
            Key=key,
            CopySource={"Bucket": source_bucket, "Key": key},
            TaggingDirective="REPLACE",
            Tagging=tags,
            MetadataDirective="COPY",
            **_encryption_kwargs(),
        )
    except Exception as exc:  # noqa: BLE001 - must surface for retry
        _log("copy_failed", source_bucket=source_bucket, key=key,
             error=str(exc), error_type=type(exc).__name__)
        raise
    version_id = response.get("VersionId")
    _log("copied", source_bucket=source_bucket, backup_bucket=BACKUP_BUCKET_NAME,
         key=key, version_id=version_id, copied_at=copied_at)
    return copied_at, version_id


def _notify(source_bucket: str, key: str, size: int | None,
            copied_at: str, version_id: str | None) -> None:
    """Publish the SNS email receipt; raises on any failure."""
    size_line = f"{size:,} bytes" if size is not None else "unknown"
    version_line = version_id if version_id else "unknown"
    message = (
        "Your file was backed up successfully.\n"
        "\n"
        f"Source bucket : {source_bucket}\n"
        f"Backup bucket : {BACKUP_BUCKET_NAME}\n"
        f"Key           : {key}\n"
        f"Size          : {size_line}\n"
        f"Backup version: {version_line}\n"
        f"Backed up at  : {copied_at} (UTC)\n"
    )
    try:
        sns().publish(TopicArn=SNS_TOPIC_ARN, Subject="File Backed Up", Message=message)
    except Exception as exc:  # noqa: BLE001 - must surface for retry
        _log("notify_failed", key=key,
             error=str(exc), error_type=type(exc).__name__)
        raise
    _log("notified", key=key, topic_arn=SNS_TOPIC_ARN)


def _process_record(record: dict) -> dict:
    source_bucket, key = _parse_record(record)
    size = _object_size(source_bucket, key)
    copied_at, version_id = _copy_object(source_bucket, key)
    _notify(source_bucket, key, size, copied_at, version_id)
    return {
        "key": key,
        "source_bucket": source_bucket,
        "backup_bucket": BACKUP_BUCKET_NAME,
        "copied_at": copied_at,
        "version_id": version_id,
        "size": size,
    }


def handler(event: dict | None, context=None) -> dict:
    """Lambda entry point. Raises on any record failure so the event retries."""
    records = (event or {}).get("Records", []) or []
    copied: list[dict] = []
    failed: list[dict] = []

    for record in records:
        try:
            copied.append(_process_record(record))
        except Exception as exc:  # noqa: BLE001 - collected, then raised
            failed.append({
                "record": record,
                "error": str(exc),
                "error_type": type(exc).__name__,
            })

    if failed:
        _log("batch_completed_with_failures", copied=len(copied), failed=len(failed))
        first = failed[0]
        raise RuntimeError(
            f"{len(failed)}/{len(records)} record(s) failed; "
            f"first error [{first['error_type']}]: {first['error']}"
        )

    _log("batch_completed", copied=len(copied))
    return {"copied": copied}

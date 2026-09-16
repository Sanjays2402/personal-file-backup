"""pytest suite for the Personal File Backup Lambda handler.

All AWS clients are mocked — no network calls, no credentials needed.
Covers: happy path, duplicate events, missing key, permission errors,
SNS publish failures, tag/metadata correctness, and a SAM template
smoke check.
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest
import yaml
from botocore.exceptions import ClientError

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
)

import app  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

def _s3_record(bucket="my-inbox", encoded_key="docs/my%20report.pdf",
               event_name="ObjectCreated:Put"):
    return {
        "eventVersion": "2.1",
        "eventSource": "aws:s3",
        "awsRegion": "us-west-2",
        "eventTime": "2026-09-13T19:00:00.000Z",
        "eventName": event_name,
        "s3": {
            "s3SchemaVersion": "1.0",
            "bucket": {"name": bucket, "arn": f"arn:aws:s3:::{bucket}"},
            "object": {"key": encoded_key, "size": 12345},
        },
    }


def _event(*records):
    return {"Records": list(records)}


def _client_error(code, message="boom"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "Op")


@pytest.fixture()
def mocks(monkeypatch):
    """Patch boto3 clients and env-driven module constants."""
    mock_s3 = MagicMock(name="s3")
    mock_sns = MagicMock(name="sns")
    mock_s3.head_object.return_value = {"ContentLength": 12345}
    mock_s3.copy_object.return_value = {"VersionId": "v1"}
    mock_sns.publish.return_value = {"MessageId": "mid-1"}
    monkeypatch.setattr(app, "_s3", mock_s3)
    monkeypatch.setattr(app, "_sns", mock_sns)
    monkeypatch.setattr(app, "BACKUP_BUCKET_NAME", "my-backup")
    monkeypatch.setattr(app, "SNS_TOPIC_ARN", "arn:aws:sns:us-west-2:123:backup")
    return mock_s3, mock_sns


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------

def test_happy_path_copy_and_notify(mocks):
    mock_s3, mock_sns = mocks

    result = app.handler(_event(_s3_record()), None)

    assert len(result["copied"]) == 1
    entry = result["copied"][0]
    assert entry["key"] == "docs/my report.pdf"  # URL-decoded
    assert entry["source_bucket"] == "my-inbox"
    assert entry["backup_bucket"] == "my-backup"
    assert entry["size"] == 12345

    # Copy: same key preserved, source preserved, backup bucket targeted.
    kwargs = mock_s3.copy_object.call_args.kwargs
    assert kwargs["Bucket"] == "my-backup"
    assert kwargs["Key"] == "docs/my report.pdf"
    assert kwargs["CopySource"] == {"Bucket": "my-inbox", "Key": "docs/my report.pdf"}
    assert kwargs["MetadataDirective"] == "COPY"
    assert kwargs["ServerSideEncryption"] == "AES256"

    # SNS receipt email.
    pub_kwargs = mock_sns.publish.call_args.kwargs
    assert pub_kwargs["TopicArn"] == "arn:aws:sns:us-west-2:123:backup"
    assert pub_kwargs["Subject"] == "File Backed Up"
    assert "docs/my report.pdf" in pub_kwargs["Message"]
    assert "my-inbox" in pub_kwargs["Message"]
    assert "my-backup" in pub_kwargs["Message"]
    assert "12,345 bytes" in pub_kwargs["Message"]


def test_receipt_includes_version_id_and_timestamp(mocks):
    """The email receipt carries the backup version ID and backup time."""
    _, mock_sns = mocks
    app.handler(_event(_s3_record()), None)

    message = mock_sns.publish.call_args.kwargs["Message"]
    assert "Backup version: v1" in message
    assert "Backed up at  : " in message and "(UTC)" in message


def test_receipt_shows_unknown_version_when_missing(mocks):
    """copy_object without a VersionId (e.g. versioning suspended) -> 'unknown'."""
    mock_s3, mock_sns = mocks
    mock_s3.copy_object.return_value = {}

    app.handler(_event(_s3_record()), None)

    message = mock_sns.publish.call_args.kwargs["Message"]
    assert "Backup version: unknown" in message


def test_default_encryption_is_sse_s3(mocks, monkeypatch):
    mock_s3, _ = mocks
    monkeypatch.setattr(app, "SSEKMS_KEY_ID", "")

    app.handler(_event(_s3_record()), None)

    kwargs = mock_s3.copy_object.call_args.kwargs
    assert kwargs["ServerSideEncryption"] == "AES256"
    assert "SSEKMSKeyId" not in kwargs


def test_kms_encryption_used_when_key_configured(mocks, monkeypatch):
    """SSEKMS_KEY_ID env set -> copies use SSE-KMS with that key."""
    mock_s3, _ = mocks
    monkeypatch.setattr(app, "SSEKMS_KEY_ID",
                        "arn:aws:kms:us-west-2:123:key/abcd-1234")

    app.handler(_event(_s3_record()), None)

    kwargs = mock_s3.copy_object.call_args.kwargs
    assert kwargs["ServerSideEncryption"] == "aws:kms"
    assert kwargs["SSEKMSKeyId"] == "arn:aws:kms:us-west-2:123:key/abcd-1234"


def test_backup_tags_identify_source_and_time(mocks):
    mock_s3, _ = mocks
    app.handler(_event(_s3_record()), None)

    kwargs = mock_s3.copy_object.call_args.kwargs
    assert kwargs["TaggingDirective"] == "REPLACE"
    tags = dict(part.split("=", 1) for part in kwargs["Tagging"].split("&"))
    assert tags["copied-from"] == "my-inbox"
    # copied-at must be a parseable UTC timestamp.
    assert "T" in tags["copied-at"]


def test_duplicate_event_is_idempotent(mocks):
    """Same record twice (S3 retries can deliver duplicates) -> same result, no error."""
    mock_s3, mock_sns = mocks
    record = _s3_record()

    result = app.handler(_event(record, record), None)

    assert len(result["copied"]) == 2
    assert mock_s3.copy_object.call_count == 2
    assert mock_sns.publish.call_count == 2
    for call in mock_s3.copy_object.call_args_list:
        assert call.kwargs["Bucket"] == "my-backup"
        assert call.kwargs["Key"] == "docs/my report.pdf"


def test_multiple_records_in_one_batch(mocks):
    mock_s3, mock_sns = mocks
    app.handler(
        _event(_s3_record(encoded_key="a.txt"), _s3_record(encoded_key="b.txt")), None
    )
    assert mock_s3.copy_object.call_count == 2
    assert mock_sns.publish.call_count == 2


def test_empty_records_returns_empty(mocks):
    assert app.handler(_event(), None) == {"copied": []}
    assert app.handler(None, None) == {"copied": []}


# --------------------------------------------------------------------------
# Error paths
# --------------------------------------------------------------------------

def test_missing_source_key_raises_for_retry(mocks, capsys):
    """Copy of a deleted/missing key fails -> RuntimeError so the event retries."""
    mock_s3, mock_sns = mocks
    mock_s3.copy_object.side_effect = _client_error("NoSuchKey", "The specified key does not exist.")

    with pytest.raises(RuntimeError, match="1/1 record\\(s\\) failed"):
        app.handler(_event(_s3_record()), None)

    # Nothing should be emailed for a failed copy.
    mock_sns.publish.assert_not_called()
    # Structured JSON log line for the failure.
    assert "copy_failed" in capsys.readouterr().out


def test_permission_error_raises_for_retry(mocks, capsys):
    mock_s3, _ = mocks
    mock_s3.copy_object.side_effect = _client_error("AccessDenied", "Access Denied")

    with pytest.raises(RuntimeError, match="AccessDenied"):
        app.handler(_event(_s3_record()), None)
    assert "copy_failed" in capsys.readouterr().out


def test_sns_publish_failure_raises_for_retry(mocks, capsys):
    """Copy succeeded but the receipt email failed -> raise so the record
    retries; the retry re-copies (idempotent) and re-attempts the email."""
    mock_s3, mock_sns = mocks
    mock_sns.publish.side_effect = _client_error("EndpointDisabled")

    with pytest.raises(RuntimeError, match="1/1 record\\(s\\) failed"):
        app.handler(_event(_s3_record()), None)

    mock_s3.copy_object.assert_called_once()  # the copy itself happened
    assert "notify_failed" in capsys.readouterr().out


def test_one_bad_record_fails_batch_but_good_one_is_processed(mocks):
    mock_s3, mock_sns = mocks
    good = _s3_record(encoded_key="good.txt")
    bad = {"s3": {"bucket": {"name": "my-inbox"}, "object": {}}}  # missing key

    with pytest.raises(RuntimeError, match="1/2 record\\(s\\) failed"):
        app.handler(_event(good, bad), None)

    # The good record was still copied and emailed before the failure.
    assert mock_s3.copy_object.call_count == 1
    assert mock_sns.publish.call_count == 1


def test_head_failure_does_not_block_backup(mocks):
    """Size lookup is best-effort: if it fails we still back up, email says unknown."""
    mock_s3, mock_sns = mocks
    mock_s3.head_object.side_effect = _client_error("403", "Forbidden")

    result = app.handler(_event(_s3_record()), None)

    assert result["copied"][0]["size"] is None
    mock_s3.copy_object.assert_called_once()
    assert "unknown" in mock_sns.publish.call_args.kwargs["Message"]


def test_integrity_verified_when_etags_match(mocks, capsys):
    """copy_object's CopyObjectResult.ETag equals the source ETag -> verified, no error."""
    mock_s3, _ = mocks
    etag = '"d41d8cd98f00b204e9800998ecf8427e"'
    mock_s3.head_object.return_value = {"ContentLength": 12345, "ETag": etag}
    mock_s3.copy_object.return_value = {
        "VersionId": "v1", "CopyObjectResult": {"ETag": etag}}

    app.handler(_event(_s3_record()), None)

    assert "integrity_verified" in capsys.readouterr().out


def test_integrity_mismatch_raises_for_retry(mocks, capsys):
    """Backup ETag differs from the source -> raise (event retries), no email."""
    mock_s3, mock_sns = mocks
    mock_s3.head_object.return_value = {
        "ContentLength": 12345, "ETag": '"src-etag"'}
    mock_s3.copy_object.return_value = {
        "VersionId": "v1", "CopyObjectResult": {"ETag": '"different-etag"'}}

    with pytest.raises(RuntimeError, match="1/1 record\\(s\\) failed"):
        app.handler(_event(_s3_record()), None)

    mock_sns.publish.assert_not_called()
    out = capsys.readouterr().out
    assert "integrity_mismatch" in out


def test_integrity_check_skipped_when_etag_unavailable(mocks):
    """No CopyObjectResult ETag (or head failed) -> check skipped, backup proceeds."""
    mock_s3, _ = mocks
    mock_s3.head_object.return_value = {"ContentLength": 12345}
    mock_s3.copy_object.return_value = {"VersionId": "v1"}

    result = app.handler(_event(_s3_record()), None)

    assert len(result["copied"]) == 1  # no integrity error raised


def test_malformed_record_raises(mocks):
    with pytest.raises(RuntimeError, match="record\\(s\\) failed"):
        app.handler(_event({"nope": "not-an-s3-record"}), None)


# --------------------------------------------------------------------------
# SAM template smoke check
# --------------------------------------------------------------------------

def _unknown_tag(loader, tag_suffix, node):
    """CloudFormation intrinsic tags (!Ref, !Sub, !GetAtt, ...) have no YAML
    constructor — load them as a plain {'Fn::Tag': ...} style mapping."""
    tag = "!" + tag_suffix
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {"Fn::Intrinsic": {"tag": tag, "value": value}}


class _CfnLoader(yaml.SafeLoader):
    pass


_CfnLoader.add_multi_constructor("!", _unknown_tag)


def _load_template():
    path = os.path.join(PROJECT_ROOT, "template.yaml")
    with open(path) as fh:
        return yaml.load(fh, Loader=_CfnLoader)


def test_template_has_required_resources():
    tpl = _load_template()
    resources = tpl["Resources"]
    for name in ("SourceBucket", "BackupBucket", "BackupTopic",
                 "BackupEmailSubscription", "FileBackupFunction"):
        assert name in resources, f"missing resource {name}"


def test_template_buckets_hardened():
    tpl = _load_template()
    for bucket_name in ("SourceBucket", "BackupBucket"):
        props = tpl["Resources"][bucket_name]["Properties"]
        assert props["VersioningConfiguration"]["Status"] == "Enabled"
        sse = props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
        algo = sse["ServerSideEncryptionByDefault"]["SSEAlgorithm"]
        if bucket_name == "SourceBucket":
            assert algo == "AES256"
        else:
            # Backup bucket: SSE-S3 by default, SSE-KMS when KmsKeyArn is set.
            assert algo == {"Fn::Intrinsic": {
                "tag": "!If",
                "value": ["UseKmsEncryption", "aws:kms", "AES256"]}}
        pab = props["PublicAccessBlockConfiguration"]
        assert all(pab[k] for k in
                   ("BlockPublicAcls", "BlockPublicPolicy",
                    "IgnorePublicAcls", "RestrictPublicBuckets"))


def test_template_function_wiring():
    tpl = _load_template()
    fn = tpl["Resources"]["FileBackupFunction"]["Properties"]
    assert fn["Runtime"] == "python3.12"
    assert fn["Handler"] == "app.handler"
    events = fn["Events"]["InboxUpload"]["Properties"]
    assert events["Events"] == "s3:ObjectCreated:*"
    env = fn["Environment"]["Variables"]
    assert "BACKUP_BUCKET_NAME" in env and "SNS_TOPIC_ARN" in env
    # Least privilege: no wildcards in any policy statement. Some policy
    # entries are wrapped in Fn::If (the KMS policy is conditional) — unwrap
    # the true-branch before checking.
    for policy in fn["Policies"]:
        if "Fn::Intrinsic" in policy and policy["Fn::Intrinsic"]["tag"] == "!If":
            policy = policy["Fn::Intrinsic"]["value"][1]  # true-branch policy
        for stmt in policy["Statement"]:
            for key in ("Action", "Resource"):
                values = stmt[key] if isinstance(stmt[key], list) else [stmt[key]]
                assert "*" not in values, f"wildcard found in {key}: {values}"


def test_template_lifecycle_expires_noncurrent_versions():
    tpl = _load_template()
    rules = tpl["Resources"]["BackupBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
    expire = next(r for r in rules if r["Id"] == "ExpireNoncurrentVersions")
    assert expire["Status"] == "Enabled"
    # Default parameter value is 90 days.
    param = tpl["Parameters"]["NoncurrentVersionExpirationDays"]
    assert param["Default"] == 90
    # The rule references the parameter (via Ref), not a hard-coded number.
    rule_days = expire["NoncurrentVersionExpirationInDays"]
    assert rule_days == {"Fn::Intrinsic": {"tag": "!Ref",
                                           "value": "NoncurrentVersionExpirationDays"}}


def test_template_kms_is_optional():
    """Default (empty KmsKeyArn) keeps SSE-S3; setting it switches to SSE-KMS."""
    tpl = _load_template()
    assert tpl["Parameters"]["KmsKeyArn"]["Default"] == ""
    assert "UseKmsEncryption" in tpl["Conditions"]

    sse = tpl["Resources"]["BackupBucket"]["Properties"]["BucketEncryption"][
        "ServerSideEncryptionConfiguration"][0]["ServerSideEncryptionByDefault"]
    algo = sse["SSEAlgorithm"]
    assert algo["Fn::Intrinsic"]["tag"] == "!If"
    _cond, true_val, false_val = algo["Fn::Intrinsic"]["value"]
    assert true_val == "aws:kms" and false_val == "AES256"

    # The handler gets the key via env; the conditional IAM policy grants
    # kms:Encrypt only when a key is configured.
    env = tpl["Resources"]["FileBackupFunction"]["Properties"]["Environment"]["Variables"]
    assert "SSEKMS_KEY_ID" in env
    policies = tpl["Resources"]["FileBackupFunction"]["Properties"]["Policies"]
    conditional = [p for p in policies
                   if p.get("Fn::Intrinsic", {}).get("tag") == "!If"]
    assert len(conditional) == 1
    statements = conditional[0]["Fn::Intrinsic"]["value"][1]["Statement"]
    actions = [a for s in statements for a in s["Action"]]
    assert "kms:Encrypt" in actions

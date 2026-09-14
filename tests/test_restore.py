"""pytest suite for scripts/restore.py.

All boto3 calls are mocked — no network, no credentials needed. Covers:
version listing (sorting, key filtering), restore copy arguments, and the
CLI wiring of both subcommands.
"""

import datetime as _dt
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
)

import restore  # noqa: E402


def _version(key, vid, minutes_ago, size=100, latest=False):
    return {
        "Key": key,
        "VersionId": vid,
        "LastModified": _dt.datetime(2026, 9, 14, 12, 0, 0, tzinfo=_dt.timezone.utc)
        - _dt.timedelta(minutes=minutes_ago),
        "Size": size,
        "IsLatest": latest,
    }


def _s3_with_versions(pages):
    mock_s3 = MagicMock(name="s3")
    paginator = MagicMock(name="paginator")
    paginator.paginate.return_value = pages
    mock_s3.get_paginator.return_value = paginator
    return mock_s3


# --------------------------------------------------------------------------
# list_versions
# --------------------------------------------------------------------------

def test_list_versions_newest_first():
    mock_s3 = _s3_with_versions([
        {"Versions": [
            _version("docs/a.pdf", "v-old", 120),
            _version("docs/a.pdf", "v-new", 5, latest=True),
            _version("docs/a.pdf", "v-mid", 60),
        ]}
    ])

    versions = restore.list_versions(mock_s3, "my-backup", "docs/a.pdf")

    assert [v["VersionId"] for v in versions] == ["v-new", "v-mid", "v-old"]
    assert versions[0]["IsLatest"] is True
    assert versions[0]["LastModified"].endswith("+00:00")  # ISO UTC string


def test_list_versions_ignores_other_keys_with_same_prefix():
    mock_s3 = _s3_with_versions([
        {"Versions": [
            _version("docs/a.pdf", "v-1", 10),
            _version("docs/a.pdf.bak", "v-other", 10),
        ]}
    ])

    versions = restore.list_versions(mock_s3, "my-backup", "docs/a.pdf")

    assert [v["VersionId"] for v in versions] == ["v-1"]


def test_list_versions_paginates():
    mock_s3 = _s3_with_versions([
        {"Versions": [_version("k", "v-1", 30)]},
        {"Versions": [_version("k", "v-2", 10, latest=True)]},
    ])

    versions = restore.list_versions(mock_s3, "k-bucket", "k")

    assert [v["VersionId"] for v in versions] == ["v-2", "v-1"]


def test_list_versions_empty():
    mock_s3 = _s3_with_versions([{"Versions": []}])
    assert restore.list_versions(mock_s3, "b", "missing.txt") == []


# --------------------------------------------------------------------------
# restore_version
# --------------------------------------------------------------------------

def test_restore_version_copies_chosen_version():
    mock_s3 = MagicMock(name="s3")
    mock_s3.copy_object.return_value = {"VersionId": "v-new-copy"}

    resp = restore.restore_version(mock_s3, "my-backup", "docs/a.pdf", "v-old",
                                   "my-inbox")

    kwargs = mock_s3.copy_object.call_args.kwargs
    assert kwargs["Bucket"] == "my-inbox"
    assert kwargs["Key"] == "docs/a.pdf"  # same key when --to-key omitted
    assert kwargs["CopySource"] == {
        "Bucket": "my-backup", "Key": "docs/a.pdf", "VersionId": "v-old",
    }
    assert resp == {"VersionId": "v-new-copy"}


def test_restore_version_supports_custom_target_key():
    mock_s3 = MagicMock(name="s3")
    mock_s3.copy_object.return_value = {}

    restore.restore_version(mock_s3, "my-backup", "docs/a.pdf", "v-old",
                            "my-inbox", to_key="docs/a-restored.pdf")

    kwargs = mock_s3.copy_object.call_args.kwargs
    assert kwargs["Bucket"] == "my-inbox"
    assert kwargs["Key"] == "docs/a-restored.pdf"


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------

def test_cli_list_wires_to_list_command(capsys):
    mock_s3 = _s3_with_versions([
        {"Versions": [_version("docs/a.pdf", "v-1", 10, latest=True)]}
    ])
    parser = restore.build_parser()
    args = parser.parse_args(["list", "my-backup", "docs/a.pdf"])

    rc = args.func(mock_s3, args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "v-1" in out
    assert "current" in out


def test_cli_list_no_versions_returns_1(capsys):
    mock_s3 = _s3_with_versions([{"Versions": []}])
    parser = restore.build_parser()
    args = parser.parse_args(["list", "my-backup", "missing.txt"])

    assert args.func(mock_s3, args) == 1


def test_cli_restore_wires_arguments():
    mock_s3 = MagicMock(name="s3")
    mock_s3.copy_object.return_value = {}
    parser = restore.build_parser()
    args = parser.parse_args(["restore", "my-backup", "docs/a.pdf", "v-old",
                              "--to-bucket", "my-inbox",
                              "--to-key", "docs/restored.pdf"])

    assert args.func(mock_s3, args) == 0

    kwargs = mock_s3.copy_object.call_args.kwargs
    assert kwargs["CopySource"]["VersionId"] == "v-old"
    assert kwargs["Key"] == "docs/restored.pdf"

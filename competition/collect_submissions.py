#!/usr/bin/env python3
"""Export the last accepted LoomQ submission per team for private evaluation."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import stat
import sys
import tempfile
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from competition.submission_common import (  # noqa: E402
    COMMIT_RE,
    GitHubClient,
    SubmissionError,
    isoformat_z,
    load_json,
    parse_timestamp,
    receipt_from_comments,
    safe_extract_submission,
    select_latest_submissions,
    sha256_file,
    utc_now,
)


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "competition" / "config.json"


def api_path(full_name: str, suffix: str) -> str:
    owner, repo = full_name.split("/", 1)
    base = f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
    return base if not suffix else f"{base}/{suffix.lstrip('/')}"


def paginated(client: GitHubClient, path: str) -> list[dict[str, Any]]:
    separator = "&" if "?" in path else "?"
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = client.json("GET", f"{path}{separator}per_page=100&page={page}")
        if not isinstance(batch, list):
            raise RuntimeError(f"GitHub API returned a non-list for {path}")
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def extract_artifact_wrapper(wrapper: Path, archive: Path, max_bytes: int) -> None:
    with zipfile.ZipFile(wrapper) as bundle:
        candidates = []
        for info in bundle.infolist():
            path = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if path.is_absolute() or ".." in path.parts or stat.S_ISLNK(mode):
                raise SubmissionError(f"Artifact ZIP 路径不安全: {info.filename}")
            if not info.is_dir() and path.name == "loomq-submission.tar.gz":
                candidates.append(info)
        if len(candidates) != 1:
            raise SubmissionError("Artifact 必须恰好包含一个 loomq-submission.tar.gz")
        info = candidates[0]
        if info.file_size <= 0 or info.file_size > max_bytes:
            raise SubmissionError("Artifact 内的提交归档超过大小限制")
        with bundle.open(info) as source, archive.open("wb") as output:
            shutil.copyfileobj(source, output)


def validated_receipt(issue: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    required = {
        "team_id", "repository", "commit_sha", "issue_number", "issue_created_at",
        "archive_sha256", "artifact_id", "artifact_name", "submission_path",
    }
    missing = sorted(required - receipt.keys())
    if missing:
        raise SubmissionError("回执缺少字段: " + ", ".join(missing))
    if int(receipt["issue_number"]) != int(issue["number"]):
        raise SubmissionError("回执 Issue 编号不匹配")
    if parse_timestamp(receipt["issue_created_at"]) != parse_timestamp(issue["created_at"]):
        raise SubmissionError("回执 Issue 时间不匹配")
    if not COMMIT_RE.fullmatch(str(receipt["commit_sha"])):
        raise SubmissionError("回执中的 commit SHA 无效")
    if not isinstance(receipt["artifact_id"], int) or receipt["artifact_id"] <= 0:
        raise SubmissionError("回执中的 Artifact ID 无效")
    if not isinstance(receipt["archive_sha256"], str) or len(receipt["archive_sha256"]) != 64:
        raise SubmissionError("回执中的归档哈希无效")
    return receipt


def collect(args: argparse.Namespace) -> int:
    config = load_json(Path(args.config))
    upstream = config["upstream"]
    deadline = parse_timestamp(config["deadline"])
    output = Path(args.output).resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    teams_dir = output / "teams"
    teams_dir.mkdir()
    client = GitHubClient(os.environ["GH_TOKEN"])

    label = urllib.parse.quote("submission:accepted")
    issues = paginated(client, api_path(upstream, f"issues?state=all&labels={label}"))
    receipts: dict[int, dict[str, Any]] = {}
    receipt_errors: dict[int, str] = {}
    for issue in issues:
        if "pull_request" in issue:
            continue
        number = int(issue["number"])
        try:
            comments = paginated(client, api_path(upstream, f"issues/{number}/comments"))
            receipts[number] = validated_receipt(issue, receipt_from_comments(comments))
        except Exception as exc:
            receipt_errors[number] = f"{type(exc).__name__}: {exc}"

    selected = select_latest_submissions(issues, receipts, deadline)
    records: list[dict[str, Any]] = []
    for team_id in sorted(selected):
        issue, receipt = selected[team_id]
        record = {
            "team_id": team_id,
            "status": "ERROR",
            "issue_number": int(issue["number"]),
            "issue_url": issue.get("html_url", ""),
            "issue_created_at": issue["created_at"],
            "repository": receipt["repository"],
            "commit_sha": receipt["commit_sha"],
            "archive_sha256": receipt["archive_sha256"],
            "artifact_id": receipt["artifact_id"],
            "error": "",
        }
        team_root = teams_dir / team_id
        try:
            team_root.mkdir()
            with tempfile.TemporaryDirectory(prefix=f"loomq-{team_id}-", dir=output) as temporary:
                temporary_path = Path(temporary)
                wrapper = temporary_path / "artifact.zip"
                archive = temporary_path / "submission.tar.gz"
                client.download(
                    api_path(upstream, f"actions/artifacts/{receipt['artifact_id']}/zip"),
                    wrapper,
                    int(config["max_archive_bytes"]) + 10 * 1024 * 1024,
                )
                extract_artifact_wrapper(wrapper, archive, int(config["max_archive_bytes"]))
                actual_sha256 = sha256_file(archive)
                if actual_sha256 != receipt["archive_sha256"]:
                    raise SubmissionError(
                        f"Artifact SHA-256 不一致: expected {receipt['archive_sha256']}, got {actual_sha256}"
                    )
                shutil.copy2(archive, team_root / "source.tar.gz")
                safe_extract_submission(
                    archive,
                    team_root / "submission",
                    config["submission_path"],
                    int(config["max_files"]),
                    int(config["max_extracted_bytes"]),
                )
            record["status"] = "COLLECTED"
            record["submission_dir"] = str(team_root / "submission")
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            if team_root.exists():
                shutil.rmtree(team_root)
        records.append(record)

    manifest = {
        "schema_version": "1.0",
        "upstream": upstream,
        "deadline": config["deadline"],
        "collected_at": isoformat_z(utc_now()),
        "selected_count": len(selected),
        "collected_count": sum(record["status"] == "COLLECTED" for record in records),
        "receipt_errors": [{"issue_number": number, "error": error} for number, error in sorted(receipt_errors.items())],
        "submissions": records,
    }
    (output / "intake-manifest.json").write_text(
        # ASCII-only JSON keeps the manifest readable by Windows tools that still
        # default to the local GBK code page.
        json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    columns = [
        "team_id", "status", "issue_number", "issue_url", "issue_created_at",
        "repository", "commit_sha", "archive_sha256", "artifact_id", "submission_dir", "error",
    ]
    with (output / "intake-manifest.csv").open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    print(json.dumps({"selected": len(selected), "collected": manifest["collected_count"], "output": str(output)}))
    return 0 if manifest["collected_count"] == len(selected) and not receipt_errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect accepted LoomQ submissions from GitHub Artifacts")
    parser.add_argument("--output", required=True, help="must be an empty or nonexistent directory")
    parser.add_argument("--config", default=str(CONFIG))
    return parser


def main() -> int:
    return collect(build_parser().parse_args())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"collection failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

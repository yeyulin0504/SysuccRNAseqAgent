"""Download and verify a pinned public benchmark dataset specification."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download public benchmark files and verify size/SHA-256.",
    )
    parser.add_argument("--spec", type=Path, required=True, help="Dataset specification JSON.")
    parser.add_argument("--destination", type=Path, required=True, help="Local dataset directory.")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify files already present without network access.",
    )
    parser.add_argument(
        "--verify-archive-members",
        action="store_true",
        help=(
            "Also verify already-extracted archive_members below archive_members_root; "
            "this never extracts archives."
        ),
    )
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_target(root: Path, relative_name: str) -> Path:
    posix_path = PurePosixPath(relative_name)
    if posix_path.is_absolute() or not posix_path.parts or ".." in posix_path.parts:
        raise ValueError(f"Unsafe dataset relative path: {relative_name}")
    target = (root / Path(*posix_path.parts)).resolve()
    target.relative_to(root)
    return target


def _verify(path: Path, entry: dict[str, Any]) -> tuple[bool, str]:
    expected_size = int(entry["size_bytes"])
    expected_sha256 = str(entry["sha256"]).lower()
    if not path.is_file():
        return False, "missing"
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        return False, f"size mismatch: expected {expected_size}, got {actual_size}"
    actual_sha256 = _sha256(path)
    if actual_sha256 != expected_sha256:
        return False, f"SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
    return True, actual_sha256


def _download(url: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "sysu-rnaseq-agent-benchmark/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return partial


def download_dataset(
    spec_path: Path,
    destination: Path,
    *,
    verify_only: bool,
    verify_archive_members: bool = False,
) -> Path:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if spec.get("schema_version") != 1 or not isinstance(spec.get("files"), list):
        raise ValueError("Dataset spec must use schema_version=1 and contain a files array.")

    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    verified_files: list[dict[str, Any]] = []
    for index, entry in enumerate(spec["files"], start=1):
        relative_name = str(entry["path"])
        target = _safe_target(destination, relative_name)
        ok, detail = _verify(target, entry)
        if not ok:
            if verify_only:
                raise RuntimeError(f"{relative_name}: {detail}")
            if target.exists():
                raise RuntimeError(
                    f"Refusing to overwrite existing unverified file {target}: {detail}"
                )
            print(f"[{index}/{len(spec['files'])}] downloading {relative_name}", flush=True)
            partial = _download(str(entry["url"]), target)
            ok, detail = _verify(partial, entry)
            if not ok:
                partial.unlink(missing_ok=True)
                raise RuntimeError(f"Downloaded file failed verification: {relative_name}: {detail}")
            partial.replace(target)
        else:
            print(f"[{index}/{len(spec['files'])}] verified {relative_name}", flush=True)
        verified_files.append(
            {
                "path": relative_name,
                "size_bytes": target.stat().st_size,
                "sha256": detail,
            }
        )

    verified_archive_members: list[dict[str, Any]] | None = None
    archive_members_root: str | None = None
    if verify_archive_members:
        members = spec.get("archive_members")
        archive_members_root_value = spec.get("archive_members_root")
        if not isinstance(members, list) or not members:
            raise ValueError(
                "--verify-archive-members requires a non-empty archive_members array in the spec."
            )
        if not isinstance(archive_members_root_value, str) or not archive_members_root_value:
            raise ValueError(
                "--verify-archive-members requires archive_members_root in the spec."
            )
        archive_members_root = archive_members_root_value
        members_root = _safe_target(destination, archive_members_root)
        verified_archive_members = []
        for index, entry in enumerate(members, start=1):
            relative_name = str(entry["path"])
            target = _safe_target(members_root, relative_name)
            ok, detail = _verify(target, entry)
            if not ok:
                raise RuntimeError(f"archive member {relative_name}: {detail}")
            print(
                f"[{index}/{len(members)}] verified archive member {relative_name}",
                flush=True,
            )
            verified_archive_members.append(
                {
                    "path": relative_name,
                    "size_bytes": target.stat().st_size,
                    "sha256": detail,
                }
            )

    lock = {
        "schema_version": 1,
        "dataset_id": spec["dataset_id"],
        # Keep the lock portable and avoid disclosing a workstation/user path.
        # The content hash below is the authoritative spec identity.
        "source_spec": spec_path.name,
        "absolute_source_spec_path_saved": False,
        "dataset_spec_sha256": _sha256(spec_path),
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": verified_files,
    }
    if verified_archive_members is not None:
        lock["archive_members_root"] = archive_members_root
        lock["archive_members"] = verified_archive_members
    lock_path = destination / "dataset.lock.json"
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return lock_path


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        lock_path = download_dataset(
            args.spec.resolve(),
            args.destination,
            verify_only=args.verify_only,
            verify_archive_members=args.verify_archive_members,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"dataset error: {exc}", file=sys.stderr)
        return 1
    print(f"Dataset verified. Lock file: {lock_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

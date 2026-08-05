from __future__ import annotations

import re
import tarfile
from pathlib import Path, PurePosixPath


class UnsafeArchiveError(RuntimeError):
    """Raised when a downloaded archive contains an unsafe member."""


def safe_extract_tar_gz(archive_path: Path, destination: Path) -> list[Path]:
    return safe_extract_tar(archive_path, destination)


def safe_extract_tar(archive_path: Path, destination: Path) -> list[Path]:
    """Extract regular files/directories without trusting paths or links.

    Remote result bundles are input from another security boundary.  Validate
    every member before writing anything so an archive cannot escape the
    attempt directory through ``..``, absolute paths, Windows drive paths, or
    symbolic/hard links.
    """

    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, mode="r:*") as archive:
        members = archive.getmembers()
        validated = [_validated_member(member, destination) for member in members]

        extracted: list[Path] = []
        for member, target in zip(members, validated, strict=True):
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                extracted.append(target)
                continue

            source = archive.extractfile(member)
            if source is None:
                raise UnsafeArchiveError(f"Could not read archive member: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            extracted.append(target)

    return extracted


def _validated_member(member: tarfile.TarInfo, destination: Path) -> Path:
    raw_name = member.name
    normalized_name = raw_name.replace("\\", "/")
    member_path = PurePosixPath(normalized_name)

    if (
        not normalized_name
        or member_path.is_absolute()
        or ".." in member_path.parts
        or re.match(r"^[A-Za-z]:", normalized_name)
    ):
        raise UnsafeArchiveError(f"Unsafe archive path: {raw_name}")

    if not (member.isdir() or member.isfile()):
        raise UnsafeArchiveError(
            f"Unsupported archive member type (links/devices are forbidden): {raw_name}"
        )

    target = (destination / Path(*member_path.parts)).resolve()
    try:
        target.relative_to(destination)
    except ValueError as exc:
        raise UnsafeArchiveError(f"Archive member escapes destination: {raw_name}") from exc
    return target

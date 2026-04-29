from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Final


ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DIST_DIR: Final[Path] = ROOT / "dist"
RELEASE_DIR: Final[Path] = DIST_DIR / "release"
PYPROJECT_PATH: Final[Path] = ROOT / "pyproject.toml"
VERSION_PATH: Final[Path] = ROOT / "src" / "chronos" / "version.py"
CHANGELOG_PATH: Final[Path] = ROOT / "CHANGELOG.md"
SPEC_PATH: Final[Path] = ROOT / "chronos.spec"
INNO_SETUP_PATH: Final[Path] = ROOT / "installer" / "windows" / "chronos.iss"

VERSION_RE: Final[re.Pattern[str]] = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
PYPROJECT_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r'(?m)^(version = ")([^"]+)(")$'
)
MODULE_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r'(?m)^(__version__ = ")([^"]+)(")$'
)


@dataclass(frozen=True)
class ReleaseContext:
    tag: str
    version: str
    release_dir: Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and publish chronos releases.")
    parser.add_argument(
        "--tag",
        default=os.environ.get("GITHUB_REF_NAME", ""),
        help="Release tag in the form vX.Y.Z. Defaults to GITHUB_REF_NAME.",
    )
    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="Skip lint/type/test commands.",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Create or update the GitHub release with built artifacts.",
    )
    args = parser.parse_args(argv)

    ctx = build_context(args.tag)
    prepare_release_dir(ctx.release_dir)

    if not args.skip_checks:
        run_quality_gates()

    update_version_files(ctx.version)
    stamp_changelog(ctx.tag)

    artifacts = build_artifacts(ctx)
    write_checksums(ctx.release_dir, artifacts)

    if args.publish:
        publish_release(ctx, artifacts)

    print(f"release complete: {ctx.tag}")
    for artifact in artifacts:
        print(artifact)
    return 0


def build_context(tag: str) -> ReleaseContext:
    match = VERSION_RE.fullmatch(tag.strip())
    if match is None:
        raise SystemExit("expected --tag vX.Y.Z or GITHUB_REF_NAME=vX.Y.Z")
    version = match.group("version")
    release_dir = RELEASE_DIR / tag
    return ReleaseContext(tag=tag, version=version, release_dir=release_dir)


def prepare_release_dir(release_dir: Path) -> None:
    release_dir.mkdir(parents=True, exist_ok=True)


def run_quality_gates() -> None:
    commands = [
        ["uv", "run", "ruff", "check", "src/", "tests/"],
        ["uv", "run", "ruff", "format", "--check", "src/", "tests/"],
        ["uv", "run", "mypy", "src/"],
        ["uv", "run", "basedpyright"],
        [
            "uv",
            "run",
            "python",
            "-m",
            "pytest",
            "--cov=chronos",
            "--cov-branch",
            "--cov-fail-under=85",
            "tests/",
        ],
    ]
    for command in commands:
        run(command)


def update_version_files(version: str) -> None:
    rewrite_with_single_match(
        PYPROJECT_PATH,
        PYPROJECT_VERSION_RE,
        lambda match: f'{match.group(1)}{version}{match.group(3)}',
    )
    rewrite_with_single_match(
        VERSION_PATH,
        MODULE_VERSION_RE,
        lambda match: f'{match.group(1)}{version}{match.group(3)}',
    )


def stamp_changelog(tag: str) -> None:
    heading = f"## [{tag}] - {date.today().isoformat()}"
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    if heading in text:
        return
    marker = "## [Unreleased]"
    if marker not in text:
        raise RuntimeError("CHANGELOG.md is missing the [Unreleased] heading")
    replacement = f"{marker}\n\n{heading}\n"
    CHANGELOG_PATH.write_text(text.replace(marker, replacement, 1), encoding="utf-8")


def build_artifacts(ctx: ReleaseContext) -> list[Path]:
    artifacts: list[Path] = []
    artifacts.extend(build_source_distribution(ctx))
    if sys.platform == "win32":
        artifacts.extend(build_windows_artifacts(ctx))
    return artifacts


def build_source_distribution(ctx: ReleaseContext) -> list[Path]:
    run([sys.executable, "-m", "build", "--sdist", "--outdir", str(ctx.release_dir)])
    return sorted(ctx.release_dir.glob("chronos-*.tar.gz"))


def build_windows_artifacts(ctx: ReleaseContext) -> list[Path]:
    work_dir = ROOT / "build" / "pyinstaller"
    dist_dir = ROOT / "build" / "pyinstaller-dist"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    if dist_dir.exists():
        shutil.rmtree(dist_dir)

    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--workpath",
            str(work_dir),
            "--distpath",
            str(dist_dir),
            str(SPEC_PATH),
        ]
    )

    portable_dir = dist_dir / "chronos"
    if not portable_dir.exists():
        raise RuntimeError("PyInstaller did not produce build/pyinstaller-dist/chronos")

    staged_portable = ctx.release_dir / f"chronos-{ctx.tag}-windows-portable"
    if staged_portable.exists():
        shutil.rmtree(staged_portable)
    shutil.copytree(portable_dir, staged_portable)

    archive_base = ctx.release_dir / f"chronos-{ctx.tag}-windows-portable"
    archive_path = Path(
        shutil.make_archive(
            str(archive_base),
            "zip",
            root_dir=ctx.release_dir,
            base_dir=staged_portable.name,
        )
    )
    installer_path = build_windows_installer(ctx, staged_portable)
    return [archive_path, installer_path]


def build_windows_installer(ctx: ReleaseContext, staged_portable: Path) -> Path:
    iscc = shutil.which("ISCC")
    if iscc is None:
        raise RuntimeError("Inno Setup compiler (ISCC) not found on PATH")
    run(
        [
            iscc,
            f"/DAppVersion={ctx.version}",
            f"/DSourceDir={staged_portable}",
            f"/DOutputDir={ctx.release_dir}",
            str(INNO_SETUP_PATH),
        ]
    )
    installer = ctx.release_dir / f"chronos-{ctx.version}-windows-installer.exe"
    if not installer.exists():
        raise RuntimeError("Inno Setup did not produce the expected installer exe")
    return installer


def write_checksums(release_dir: Path, artifacts: list[Path]) -> Path:
    checksum_path = release_dir / "SHA256SUMS.txt"
    lines = [
        f"{sha256(path)}  {path.name}"
        for path in sorted(artifacts, key=lambda item: item.name)
    ]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    artifacts.append(checksum_path)
    return checksum_path


def publish_release(ctx: ReleaseContext, artifacts: list[Path]) -> None:
    notes_path = write_release_notes(ctx.tag)
    existing = subprocess.run(
        ["gh", "release", "view", ctx.tag],
        cwd=ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if existing.returncode != 0:
        run(
            [
                "gh",
                "release",
                "create",
                ctx.tag,
                "--title",
                ctx.tag,
                "--notes-file",
                str(notes_path),
                *[str(path) for path in artifacts],
            ]
        )
        return
    run(["gh", "release", "upload", ctx.tag, "--clobber", *[str(p) for p in artifacts]])


def write_release_notes(tag: str) -> Path:
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    section = extract_changelog_section(text, tag)
    notes_path = ROOT / "build" / f"{tag}-release-notes.md"
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    notes_path.write_text(section.strip() + "\n", encoding="utf-8")
    return notes_path


def extract_changelog_section(text: str, tag: str) -> str:
    match = re.search(
        rf"(?ms)^## \[{re.escape(tag)}\].*?(?=^## \[|\Z)",
        text,
    )
    if match is None:
        raise RuntimeError(f"CHANGELOG.md does not contain a section for {tag}")
    return match.group(0)


def rewrite_with_single_match(
    path: Path,
    pattern: re.Pattern[str],
    replacement: Callable[[re.Match[str]], str],
) -> None:
    text = path.read_text(encoding="utf-8")
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one match in {path}")
    new_text = pattern.sub(lambda m: replacement(m), text, count=1)
    path.write_text(new_text, encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify that a built Hypergraph wheel satisfies the release contract."""

from __future__ import annotations

import sys
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from tarfile import open as open_tar
from zipfile import ZipFile

EXPECTED_DISTRIBUTION = "hypergraph-ai"
EXPECTED_VERSION = "0.2.0b1"
EXPECTED_CLASSIFIER = "Development Status :: 4 - Beta"
FORBIDDEN_DIRECTORIES = frozenset({"dev", "specs", "tests"})
ASSET_DIRECTORIES = (
    Path("checkpointers/_assets"),
    Path("runners/_shared/assets"),
    Path("viz/assets"),
)
REQUIRED_ASSETS = frozenset(
    {
        "hypergraph/checkpointers/_assets/explorer.js",
        "hypergraph/runners/_shared/assets/inspect.css",
        "hypergraph/runners/_shared/assets/inspect.js",
        "hypergraph/runners/_shared/assets/inspect_transport.js",
        "hypergraph/viz/assets/vendor/reactflow.css",
        "hypergraph/viz/assets/vendor/reactflow.umd.js",
        "hypergraph/viz/assets/viz.js",
        "hypergraph/viz/assets/viz_runtime.js",
    }
)


def _source_contract(repo_root: Path) -> tuple[set[str], set[str]]:
    package_root = repo_root / "src" / "hypergraph"
    packages = {init_file.parent.relative_to(repo_root / "src").as_posix() for init_file in package_root.rglob("__init__.py")}
    assets = {
        (Path("hypergraph") / asset.relative_to(package_root)).as_posix()
        for directory in ASSET_DIRECTORIES
        for asset in (package_root / directory).rglob("*")
        if asset.is_file() and asset.suffix != ".py" and "__pycache__" not in asset.parts
    }
    return packages, assets | REQUIRED_ASSETS


def _metadata(wheel: ZipFile, members: set[str]) -> tuple[str, str, set[str]]:
    metadata_files = [name for name in members if name.endswith(".dist-info/METADATA")]
    if len(metadata_files) != 1:
        raise ValueError(f"expected one METADATA file, found {len(metadata_files)}")

    message = BytesParser().parsebytes(wheel.read(metadata_files[0]))
    return (
        message.get("Name", ""),
        message.get("Version", ""),
        set(message.get_all("Classifier", [])),
    )


def verify_wheel(wheel_path: Path, repo_root: Path) -> list[str]:
    expected_packages, expected_assets = _source_contract(repo_root)
    errors: list[str] = []

    with ZipFile(wheel_path) as wheel:
        members = set(wheel.namelist())
        distribution, version, classifiers = _metadata(wheel, members)

    expected_package_files = {f"{package}/__init__.py" for package in expected_packages}
    missing_packages = sorted(expected_package_files - members)
    missing_assets = sorted(expected_assets - members)
    forbidden_members = sorted(member for member in members if FORBIDDEN_DIRECTORIES.intersection(PurePosixPath(member).parts))

    if distribution != EXPECTED_DISTRIBUTION:
        errors.append(f"distribution is {distribution!r}, expected {EXPECTED_DISTRIBUTION!r}")
    if version != EXPECTED_VERSION:
        errors.append(f"version is {version!r}, expected {EXPECTED_VERSION!r}")
    if EXPECTED_CLASSIFIER not in classifiers:
        errors.append(f"missing classifier {EXPECTED_CLASSIFIER!r}")
    if missing_packages:
        errors.append(f"missing packages: {', '.join(missing_packages)}")
    if missing_assets:
        errors.append(f"missing assets: {', '.join(missing_assets)}")
    if forbidden_members:
        errors.append(f"forbidden members: {', '.join(forbidden_members)}")
    if any(member.startswith("src/") for member in members):
        errors.append("wheel contains the source-layout prefix src/")

    print(f"wheel: {wheel_path.name}")
    print(f"distribution: {distribution} {version}")
    print(f"subpackages: {len(expected_packages) - len(missing_packages)}/{len(expected_packages)} present")
    print(f"assets: {len(expected_assets) - len(missing_assets)}/{len(expected_assets)} present")
    print(f"forbidden directories: {'present' if forbidden_members else 'absent'}")

    return errors


def verify_sdist(sdist_path: Path, repo_root: Path) -> list[str]:
    expected_packages, expected_assets = _source_contract(repo_root)
    errors: list[str] = []

    with open_tar(sdist_path, "r:gz") as archive:
        raw_members = {PurePosixPath(member.name) for member in archive.getmembers() if member.isfile()}

    roots = {member.parts[0] for member in raw_members if member.parts}
    if len(roots) != 1:
        return [f"sdist should have one root directory, found {sorted(roots)}"]

    members = {PurePosixPath(*member.parts[1:]).as_posix() for member in raw_members if len(member.parts) > 1}
    expected_package_files = {f"src/{package}/__init__.py" for package in expected_packages}
    expected_asset_files = {f"src/{asset}" for asset in expected_assets}
    missing_packages = sorted(expected_package_files - members)
    missing_assets = sorted(expected_asset_files - members)
    forbidden_members = sorted(member for member in members if FORBIDDEN_DIRECTORIES.intersection(PurePosixPath(member).parts))

    if "docs/changelog.md" not in members:
        errors.append("sdist is missing docs/changelog.md")
    if missing_packages:
        errors.append(f"sdist missing packages: {', '.join(missing_packages)}")
    if missing_assets:
        errors.append(f"sdist missing assets: {', '.join(missing_assets)}")
    if forbidden_members:
        errors.append(f"sdist forbidden members: {', '.join(forbidden_members)}")

    print(f"sdist: {sdist_path.name}")
    print(f"sdist changelog: {'present' if 'docs/changelog.md' in members else 'missing'}")
    print(f"sdist subpackages: {len(expected_package_files) - len(missing_packages)}/{len(expected_package_files)} present")
    print(f"sdist assets: {len(expected_asset_files) - len(missing_assets)}/{len(expected_asset_files)} present")
    print(f"sdist forbidden directories: {'present' if forbidden_members else 'absent'}")
    return errors


def main() -> int:
    if len(sys.argv) not in {2, 3}:
        print("usage: verify_distribution.py WHEEL [SDIST]", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parents[1]
    wheel_path = Path(sys.argv[1]).resolve()
    if not wheel_path.is_file():
        print(f"wheel does not exist: {wheel_path}", file=sys.stderr)
        return 2

    try:
        errors = verify_wheel(wheel_path, repo_root)
        if len(sys.argv) == 3:
            sdist_path = Path(sys.argv[2]).resolve()
            if not sdist_path.is_file():
                print(f"sdist does not exist: {sdist_path}", file=sys.stderr)
                return 2
            errors.extend(verify_sdist(sdist_path, repo_root))
    except (OSError, ValueError) as error:
        print(f"distribution verification failed: {error}", file=sys.stderr)
        return 1

    if errors:
        print("FAILED")
        for error in errors:
            print(f"- {error}")
        return 1

    print("PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

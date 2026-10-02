#!/usr/bin/env python3
"""Verify the complete release locally; publication is restricted to tag CI."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

import build


def verify_archive(path, target):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError("duplicate bundle entries")
        for name in names:
            build.safe_name(name)
        lines = archive.read("SHA256SUMS").decode("utf-8").splitlines()
        checked = set()
        for line in lines:
            digest, name = line.split("  ", 1)
            if name in checked or name == "SHA256SUMS" or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid internal checksum inventory")
            checked.add(name)
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise ValueError("internal checksum mismatch")
        if checked != set(names) - {"SHA256SUMS"}:
            raise ValueError("incomplete internal checksum inventory")
        suffix = ".exe" if target.startswith("windows") else ""
        required = {"bin/materialize-cli" + suffix, "Cargo.lock", "sources.json", "metadata.json",
                    "notices/Materialize-LICENSE", "rust-dependencies.json"}
        if not required.issubset(checked):
            raise ValueError("required release files missing")
        if archive.read("Cargo.lock") != build.LOCK.read_bytes() or archive.read("sources.json") != build.CONFIG.read_bytes():
            raise ValueError("bundle source/lock identity mismatch")
        if archive.read("notices/Materialize-LICENSE") != (build.HERE / "notices/Materialize-LICENSE").read_bytes():
            raise ValueError("bundle upstream notice mismatch")
        for patch in build.PATCHES:
            if archive.read("patches/" + patch.name) != patch.read_bytes():
                raise ValueError("bundle compatibility patch mismatch")
        metadata = json.loads(archive.read("metadata.json"))
        if (metadata["tool"] != "materialize" or metadata["target"] != target
                or metadata["version"] != build.PINS["version"]
                or metadata["rust_target"] != build.PINS["targets"][target]
                or metadata["lock_sha256"] != build.PINS["lock_sha256"]
                or metadata["sources_sha256"] != build.sha256(build.CONFIG)
                or metadata.get("patches") != {p.name: build.sha256(p) for p in build.PATCHES}
                or metadata["smoke"]["cli"] != "help/version/list-maps executed"):
            raise ValueError("bundle provenance mismatch")


def verify(directory, tag):
    build.check(tag)
    directory = build.generated_path(directory)
    expected = []
    for target in build.PINS["targets"]:
        name = f"materialize-{build.PINS['version']}-{target}.zip"
        expected.extend([name, name + ".sha256"])
    actual = {path.name for path in directory.iterdir()}
    if actual - {"SHA256SUMS"} != set(expected):
        raise ValueError("release requires exactly all four archives and sidecars")
    sums = []
    for target in build.PINS["targets"]:
        path = directory / f"materialize-{build.PINS['version']}-{target}.zip"
        line = f"{build.sha256(path)}  {path.name}\n"
        if path.with_suffix(".zip.sha256").read_bytes() != line.encode():
            raise ValueError("release archive checksum mismatch")
        verify_archive(path, target)
        sums.append(line)
    (directory / "SHA256SUMS").write_bytes("".join(sorted(sums)).encode())
    return [directory / name for name in sorted(expected)] + [directory / "SHA256SUMS"]


def publish(directory, tag):
    # No GitHub subprocess is reached from local tasks, PRs or branch pushes.
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_EVENT_NAME") != "push"
            or os.environ.get("GITHUB_REF") != "refs/tags/" + tag):
        raise ValueError("publication requires a GitHub Actions tag push")
    assets = verify(directory, tag)
    repository = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid GitHub repository")
    notes = ("Native Materialize developer tools. CLI help/version/list-maps passed on all four runners. "
             "GPU map generation was not tested by CI. Verify SHA256SUMS before use; review bundled "
             "dependency notices and metadata. Windows ARM64 and macOS amd64 are unsupported.")
    def gh(*args):
        subprocess.run(["gh", "release", *map(str, args), "--repo", repository], check=True)
    # Create fails if a release already exists; never overwrite immutable assets.
    # The tag already exists. Do not send target_commitish: GitHub can require
    # additional workflow permissions when an explicit target is supplied.
    gh("create", tag, "--verify-tag", "--draft", "--title", tag, "--notes", notes)
    gh("upload", tag, *assets)
    gh("edit", tag, "--draft=false")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify", "publish"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    if args.command == "verify":
        verify(args.directory, args.tag)
    else:
        publish(args.directory, args.tag)

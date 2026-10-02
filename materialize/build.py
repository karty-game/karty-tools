#!/usr/bin/env python3
"""Checksum-verified native Materialize builds; generated files stay in dist/."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tomllib
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG = HERE / "sources.json"
LOCK = HERE / "Cargo.lock"
PINS = json.loads(CONFIG.read_text(encoding="utf-8"))
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXTRACTED = 512 * 1024 * 1024


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).write_bytes((json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def generated_path(path):
    """Do not let work/output cleanup follow symlinks outside repository dist/."""
    base = ROOT / "dist"
    if base.resolve() != base:
        raise ValueError("dist must not be a symlink")
    candidate = Path(path).absolute()
    if not candidate.is_relative_to(base) or candidate == base:
        raise ValueError("generated path must be below repository dist/")
    for ancestor in (candidate, *candidate.parents):
        if ancestor == ROOT:
            break
        if ancestor.is_symlink():
            raise ValueError("generated path must not contain symlinks")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base) or resolved == base:
        raise ValueError("generated path escapes repository dist/")
    return resolved


def run(args, cwd=None, capture=False):
    args = [str(arg) for arg in args]
    print("+", subprocess.list2cmdline(args), flush=True)
    result = subprocess.run(args, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else None


def cargo(*args, cwd=None, capture=False):
    return run(["cargo", "+" + PINS["rust"], *args], cwd=cwd, capture=capture)


def check(tag=None):
    for value, length in ((PINS["source"]["commit"], 40),
                          (PINS["source"]["archive_sha256"], 64),
                          (PINS["source"]["manifest_sha256"], 64),
                          (PINS["lock_sha256"], 64)):
        if not re.fullmatch(r"[0-9a-f]{%d}" % length, value):
            raise ValueError("invalid immutable source/lock pin")
    if not re.fullmatch(r"\d+\.\d+\.\d+", PINS["version"]):
        raise ValueError("version must be a numeric release version")
    if tag is not None and tag != "materialize-v" + PINS["version"]:
        raise ValueError("release tag does not match source manifest version")
    if sha256(LOCK) != PINS["lock_sha256"]:
        raise ValueError("committed Cargo.lock checksum mismatch")
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    for package in lock["package"]:
        if package.get("source"):
            if package["source"] != "registry+https://github.com/rust-lang/crates.io-index":
                raise ValueError("only checksum-locked public registry dependencies allowed")
            if not re.fullmatch(r"[0-9a-f]{64}", package.get("checksum", "")):
                raise ValueError("dependency missing checksum")
        elif (package["name"], package["version"]) != (PINS["source"]["binary"], PINS["version"]):
            raise ValueError("unexpected unlocked package/version")


def native_target():
    systems = {"Linux": "linux", "Darwin": "darwin", "Windows": "windows"}
    arches = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    target = systems.get(platform.system(), "unknown") + "-" + arches.get(platform.machine().lower(), "unknown")
    if target not in PINS["targets"]:
        raise ValueError("unsupported native developer platform: " + target)
    return target


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
            or ":" in name or "\0" in name or any(p.endswith((" ", ".")) for p in path.parts)):
        raise ValueError("unsafe source archive path")
    return path


def extract(archive, destination, prefix):
    """Only regular files/directories, bounded size, exclusive file creation."""
    seen = set()
    total = 0
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            safe_name(member.name)
            if not member.name.startswith(prefix) or member.name == prefix.rstrip("/"):
                continue
            relative = member.name[len(prefix):]
            if not relative:
                continue
            path = safe_name(relative)
            key = path.as_posix().casefold()
            if key in seen or not (member.isdir() or member.isfile()):
                raise ValueError("duplicate archive path, link or special file")
            seen.add(key)
            target = destination / path
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("archive path escapes destination")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                total += member.size
                if total > MAX_EXTRACTED:
                    raise ValueError("source extraction size limit exceeded")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)


def fetch(work):
    pin = PINS["source"]
    cache = generated_path(work / "downloads")
    cache.mkdir(parents=True, exist_ok=True)
    archive = generated_path(cache / (pin["commit"] + ".tar.gz"))
    owner_repo = pin["repository"].removeprefix("https://github.com/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", owner_repo):
        raise ValueError("source must be a public GitHub repository")
    if not archive.exists():
        url = f"https://codeload.github.com/{owner_repo}/tar.gz/{pin['commit']}"
        temporary = generated_path(archive.with_suffix(".partial"))
        try:
            with urllib.request.urlopen(url, timeout=180) as src, temporary.open("wb") as dst:
                total = 0
                while chunk := src.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE:
                        raise ValueError("source download size limit exceeded")
                    dst.write(chunk)
            if sha256(temporary) != pin["archive_sha256"]:
                raise ValueError("source archive checksum mismatch")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    if sha256(archive) != pin["archive_sha256"]:
        raise ValueError("cached source archive checksum mismatch")
    destination = generated_path(work / "sources" / "materialize")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    prefix = f"{owner_repo.split('/')[1]}-{pin['commit']}/{pin['subdirectory']}/"
    extract(archive, destination, prefix)
    if sha256(destination / "Cargo.toml") != pin["manifest_sha256"]:
        raise ValueError("upstream Cargo manifest checksum mismatch")
    manifest = tomllib.loads((destination / "Cargo.toml").read_text(encoding="utf-8"))
    if manifest["package"]["version"] != PINS["version"]:
        raise ValueError("upstream version does not match release manifest")
    if (destination / "LICENSE").read_bytes() != (HERE / "notices" / "Materialize-LICENSE").read_bytes():
        raise ValueError("upstream license differs from reviewed notice")
    if (destination / "Cargo.lock").exists():
        raise ValueError("upstream unexpectedly contains a lock; review lock policy")
    shutil.copyfile(LOCK, destination / "Cargo.lock")
    return destination


def rust_notices(source, bundle):
    cargo("fetch", "--locked", cwd=source)
    metadata = json.loads(cargo("metadata", "--locked", "--format-version", "1", cwd=source, capture=True))
    packages = []
    for package in sorted(metadata["packages"], key=lambda item: item["id"]):
        root = Path(package["manifest_path"]).parent
        files = set()
        for entry in root.iterdir():
            if entry.name.lower().startswith(("license", "licence", "copying", "copyright", "notice")):
                if entry.is_file():
                    files.add(entry)
                elif entry.is_dir():
                    files.update(p for p in entry.rglob("*") if p.is_file())
        if package.get("license_file"):
            files.add(root / package["license_file"])
        copied = []
        for file in sorted(files):
            relative = file.resolve().relative_to(root.resolve())
            target = bundle / "notices" / "rust" / (package["name"] + "-" + package["version"]) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
            copied.append(target.relative_to(bundle).as_posix())
        packages.append({"name": package["name"], "version": package["version"],
                         "source": package["source"], "license": package["license"], "notice_files": copied})
    write_json(bundle / "rust-dependencies.json", packages)
    return [p["name"] + "-" + p["version"] for p in packages if not p["notice_files"]]


def bundle_zip(bundle, output):
    files = sorted(p for p in bundle.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (bundle / "SHA256SUMS").write_bytes("".join(
        f"{sha256(p)}  {p.relative_to(bundle).as_posix()}\n" for p in files).encode())
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(p for p in bundle.rglob("*") if p.is_file()):
            relative = path.relative_to(bundle).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if relative.startswith("bin/") else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    output.with_suffix(".zip.sha256").write_bytes(f"{sha256(output)}  {output.name}\n".encode())


def build(work, output, target):
    if native_target() != target:
        raise ValueError(f"native build required: running {native_target()}, requested {target}")
    check()
    work, output = generated_path(work), generated_path(output)
    rustc = run(["rustc", "+" + PINS["rust"], "--version", "--verbose"], capture=True)
    if f"release: {PINS['rust']}\n" not in rustc:
        raise ValueError("unexpected Rust toolchain")
    source = fetch(work)
    bundle = generated_path(work / "bundles" / target)
    if bundle.exists():
        shutil.rmtree(bundle)
    (bundle / "bin").mkdir(parents=True)
    suffix = ".exe" if target.startswith("windows") else ""
    binary = PINS["source"]["binary"] + suffix
    cargo("build", "--release", "--locked", "--bin", PINS["source"]["binary"],
          "--target", PINS["targets"][target], cwd=source)
    if sha256(source / "Cargo.lock") != PINS["lock_sha256"]:
        raise ValueError("Cargo changed the shared lockfile")
    shutil.copyfile(source / "target" / PINS["targets"][target] / "release" / binary, bundle / "bin" / binary)
    (bundle / "bin" / binary).chmod(0o755)
    missing = rust_notices(source, bundle)
    shutil.copytree(HERE / "notices", bundle / "notices", dirs_exist_ok=True)
    shutil.copyfile(LOCK, bundle / "Cargo.lock")
    shutil.copyfile(CONFIG, bundle / "sources.json")
    from smoke import smoke
    result = smoke(bundle / "bin", generated_path(work / "smoke" / target))
    write_json(bundle / "metadata.json", {
        "schema": 1, "tool": "materialize", "version": PINS["version"], "target": target,
        "rust_target": PINS["targets"][target], "sources_sha256": sha256(CONFIG),
        "lock_sha256": sha256(LOCK), "rustc": rustc, "smoke": result,
        "notice_files_missing": missing,
        "runner": {"system": platform.platform(), "image_os": os.environ.get("ImageOS"),
                   "image_version": os.environ.get("ImageVersion")}})
    output.mkdir(parents=True, exist_ok=True)
    bundle_zip(bundle, output / f"materialize-{PINS['version']}-{target}.zip")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "build"])
    parser.add_argument("--work", type=Path, default=ROOT / "dist" / "materialize")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--target", choices=list(PINS["targets"]))
    parser.add_argument("--tag")
    args = parser.parse_args()
    check(args.tag)
    if args.command == "build":
        build(args.work, args.output or args.work / "artifacts", args.target or native_target())


if __name__ == "__main__":
    main()
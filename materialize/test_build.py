import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build
import release
import smoke


class BuildTests(unittest.TestCase):
    def test_pins_and_shared_lock(self):
        build.check("materialize-v2.0.0")
        self.assertEqual(build.PINS["rust"], "1.99.0")
        self.assertEqual(build.PINS["source"]["commit"], "1a3fe7d052a464d3217ef66d2bce9889d3946792")
        self.assertEqual(set(build.PINS["targets"]), {"linux-amd64", "linux-arm64", "darwin-arm64", "windows-amd64"})
        for tag in ("v2.0.0", "materialize-v2.0.1", "materialize-v2.0.0-rc1", "materialize-v../../bad"):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "tag"):
                build.check(tag)

    def test_tampered_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "Cargo.lock"
            lock.write_bytes(b"tampered")
            with patch.object(build, "LOCK", lock), self.assertRaisesRegex(ValueError, "checksum mismatch"):
                build.check()

    def test_native_targets(self):
        for system, arch, expected in (("Linux", "x86_64", "linux-amd64"), ("Linux", "aarch64", "linux-arm64"),
                                       ("Darwin", "arm64", "darwin-arm64"), ("Windows", "AMD64", "windows-amd64")):
            with patch("build.platform.system", return_value=system), patch("build.platform.machine", return_value=arch):
                self.assertEqual(build.native_target(), expected)
        for system, arch in (("Darwin", "x86_64"), ("Windows", "ARM64")):
            with patch("build.platform.system", return_value=system), patch("build.platform.machine", return_value=arch):
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    build.native_target()

    def test_reject_cross_build_before_writes(self):
        with patch("build.native_target", return_value="linux-amd64"), patch("build.fetch") as fetch:
            with self.assertRaisesRegex(ValueError, "native build required"):
                build.build(Path("unused"), Path("unused"), "windows-amd64")
            fetch.assert_not_called()

    def test_generated_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(build, "ROOT", Path(directory)):
            root = Path(directory)
            self.assertEqual(build.generated_path(root / "dist/work"), (root / "dist/work").resolve())
            for path in (root, root / "dist", root / "source", root / "dist/../source"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    build.generated_path(path)
            (root / "dist").mkdir()
            try:
                (root / "dist/link").symlink_to(root, target_is_directory=True)
            except OSError:
                return  # Windows hosts without symlink privilege still exercise lexical confinement.
            with self.assertRaisesRegex(ValueError, "symlink"):
                build.generated_path(root / "dist/link/work")

    def test_generated_paths_with_parent_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "repository"
            root.mkdir()
            alias = parent / "alias"
            try:
                alias.symlink_to(parent, target_is_directory=True)
            except OSError as error:
                self.skipTest("host cannot create directory symlinks: " + str(error))
            aliased_root = alias / "repository"
            with patch.object(build, "ROOT", aliased_root):
                self.assertEqual(build.generated_path(aliased_root / "dist/work"), root / "dist/work")
                self.assertEqual(build.generated_path(root / "dist/work"), root / "dist/work")
                (root / "dist").mkdir()
                (root / "dist/link").symlink_to(parent, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    build.generated_path(aliased_root / "dist/link/work")

    def test_generated_paths_reject_linked_dist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            outside = root / "outside"
            outside.mkdir()
            try:
                (root / "dist").symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest("host cannot create directory symlinks: " + str(error))
            with patch.object(build, "ROOT", root), self.assertRaisesRegex(ValueError, "symlink"):
                build.generated_path(root / "dist/work")

    def archive(self, path, entries):
        with tarfile.open(path, "w:gz") as archive:
            for name, kind in entries:
                member = tarfile.TarInfo(name)
                if kind == "link":
                    member.type = tarfile.SYMTYPE
                    member.linkname = "../../outside"
                    archive.addfile(member)
                else:
                    member.size = 4
                    archive.addfile(member, io.BytesIO(b"test"))

    def test_safe_subtree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.archive(root / "source.tar.gz", [("repo-pin/Materialize/LICENSE", "file"), ("repo-pin/Other/ignored", "file")])
            build.extract(root / "source.tar.gz", root / "out", "repo-pin/Materialize/")
            self.assertEqual((root / "out/LICENSE").read_bytes(), b"test")
            self.assertEqual(list((root / "out").iterdir()), [root / "out/LICENSE"])

    def test_unsafe_archives(self):
        cases = [[(name, kind)] for name, kind in (("repo-pin/../escape", "file"), ("/absolute", "file"),
                 ("repo-pin/link", "link"), ("repo-pin/a\\b", "file"), ("repo-pin/C:bad", "file"),
                 ("repo-pin/bad.", "file"))]
        cases.append([("repo-pin/a", "file"), ("repo-pin/A", "file")])
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.archive(root / "source.tar.gz", entries)
                with self.assertRaises(ValueError):
                    build.extract(root / "source.tar.gz", root / "out", "repo-pin/")

    def test_extraction_size_bound(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(build, "MAX_EXTRACTED", 3):
            root = Path(directory)
            self.archive(root / "source.tar.gz", [("repo-pin/file", "file")])
            with self.assertRaisesRegex(ValueError, "size limit"):
                build.extract(root / "source.tar.gz", root / "out", "repo-pin/")

    def test_cached_checksum_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(build, "ROOT", Path(directory)):
            work = Path(directory) / "dist/work"
            (work / "downloads").mkdir(parents=True)
            (work / "downloads" / (build.PINS["source"]["commit"] + ".tar.gz")).write_bytes(b"tampered")
            with patch("build.extract") as extract, self.assertRaisesRegex(ValueError, "checksum mismatch"):
                build.fetch(work)
            extract.assert_not_called()

    def test_bad_download_not_cached(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(build, "ROOT", Path(directory)):
            work = Path(directory) / "dist/work"
            with patch("build.urllib.request.urlopen", return_value=io.BytesIO(b"tampered")):
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    build.fetch(work)
            self.assertEqual(list((work / "downloads").iterdir()), [])

    def test_zip_stability_hashes_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            (bundle / "bin").mkdir(parents=True)
            (bundle / "bin/materialize-cli").write_bytes(b"fixture-not-binary")
            build.write_json(bundle / "metadata.json", {"fixture": True})
            first, second = root / "first.zip", root / "second.zip"
            build.bundle_zip(bundle, first)
            build.bundle_zip(bundle, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                for line in archive.read("SHA256SUMS").decode().splitlines():
                    digest, name = line.split("  ", 1)
                    self.assertEqual(digest, hashlib.sha256(archive.read(name)).hexdigest())
                self.assertEqual((archive.getinfo("bin/materialize-cli").external_attr >> 16) & 0o777, 0o755)
                self.assertEqual(archive.getinfo("metadata.json").date_time, (1980, 1, 1, 0, 0, 0))
            self.assertEqual(first.with_suffix(".zip.sha256").read_text().split()[0], build.sha256(first))

    def test_cargo_always_uses_exact_toolchain(self):
        with patch("build.run") as run:
            build.cargo("build", "--locked")
            run.assert_called_once_with(["cargo", "+1.99.0", "build", "--locked"], cwd=None, capture=False)

    def test_smoke_cli_no_gpu(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(build, "ROOT", Path(directory)):
            root = Path(directory)
            for suffix in ("", ".exe"):
                binaries = root / ("windows" if suffix else "unix")
                binaries.mkdir()
                (binaries / ("materialize-cli" + suffix)).touch()
                with patch("smoke.subprocess.run") as run:
                    result = smoke.smoke(binaries, root / "dist/smoke")
                    self.assertEqual(result["gpu"], "not tested")
                    self.assertEqual([call.args[0][1] for call in run.call_args_list], ["--help", "--version", "--list-maps"])
                    self.assertTrue(run.call_args.args[0][0].endswith("materialize-cli" + suffix))

    def test_gpu_outputs_required_and_separate(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(build, "ROOT", Path(directory)):
            root = Path(directory)
            with patch("smoke.subprocess.run"):
                with self.assertRaises(FileNotFoundError):
                    smoke.smoke(root, root / "dist/smoke", gpu=True)
            def generate(args, **kwargs):
                if "--only" in args:
                    out = Path(args[args.index("--output") + 1])
                    for name in ("height", "normal"):
                        (out / ("fixture_" + name + ".png")).write_bytes(smoke.png())
            with patch("smoke.subprocess.run", side_effect=generate):
                result = smoke.smoke(root, root / "dist/smoke", gpu=True)
                self.assertEqual(len(result["maps_sha256"]), 2)
            self.assertEqual(struct.unpack(">II", smoke.png()[16:24]), (32, 32))

    def test_smoke_failure_propagates(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(build, "ROOT", Path(directory)):
            with patch("smoke.subprocess.run", side_effect=subprocess.CalledProcessError(1, "fixture")):
                with self.assertRaises(subprocess.CalledProcessError):
                    smoke.smoke(Path(directory), Path(directory) / "dist/smoke")


class ReleaseTests(unittest.TestCase):
    def make_release(self, directory):
        for target in build.PINS["targets"]:
            bundle = directory.parent / ("bundle-" + target)
            (bundle / "bin").mkdir(parents=True)
            suffix = ".exe" if target.startswith("windows") else ""
            (bundle / "bin" / ("materialize-cli" + suffix)).write_bytes(b"fixture-not-binary")
            shutil.copyfile(build.CONFIG, bundle / "sources.json")
            shutil.copyfile(build.LOCK, bundle / "Cargo.lock")
            shutil.copytree(build.HERE / "notices", bundle / "notices")
            build.write_json(bundle / "rust-dependencies.json", [])
            build.write_json(bundle / "metadata.json", {
                "tool": "materialize", "target": target, "version": build.PINS["version"],
                "rust_target": build.PINS["targets"][target], "lock_sha256": build.PINS["lock_sha256"],
                "sources_sha256": build.sha256(build.CONFIG), "smoke": {"cli": "help/version/list-maps executed"}})
            build.bundle_zip(bundle, directory / f"materialize-{build.PINS['version']}-{target}.zip")

    def test_complete_release_and_tampering(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(build, "ROOT", Path(temporary)):
            directory = Path(temporary) / "dist/release"
            directory.mkdir(parents=True)
            self.make_release(directory)
            assets = release.verify(directory, "materialize-v2.0.0")
            self.assertEqual(len(assets), 9)
            self.assertEqual(len((directory / "SHA256SUMS").read_text().splitlines()), 4)
            self.assertEqual(release.verify(directory, "materialize-v2.0.0"), assets)
            archive = next(directory.glob("*.zip"))
            archive.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                release.verify(directory, "materialize-v2.0.0")

    def test_missing_target_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(build, "ROOT", Path(temporary)):
            directory = Path(temporary) / "dist/release"
            directory.mkdir(parents=True)
            self.make_release(directory)
            next(directory.glob("*linux-arm64.zip")).unlink()
            with self.assertRaisesRegex(ValueError, "all four"):
                release.verify(directory, "materialize-v2.0.0")

    def test_internal_hash_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(build, "ROOT", Path(temporary)):
            directory = Path(temporary) / "dist/release"
            directory.mkdir(parents=True)
            self.make_release(directory)
            archive = next(directory.glob("*linux-amd64.zip"))
            bundle = directory.parent / "bundle-linux-amd64"
            (bundle / "bin/materialize-cli").write_bytes(b"changed")
            # Replace archive content without regenerating its internal inventory.
            with zipfile.ZipFile(archive, "w") as output:
                for path in bundle.rglob("*"):
                    if path.is_file():
                        output.write(path, path.relative_to(bundle).as_posix())
            with self.assertRaisesRegex(ValueError, "internal checksum mismatch"):
                release.verify_archive(archive, "linux-amd64")

    def test_no_publication_locally_or_on_pr(self):
        for env in ({}, {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/tags/materialize-v2.0.0"},
                    {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"}):
            with patch.dict("os.environ", env, clear=True), patch("release.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "tag push"):
                    release.publish(Path("unused"), "materialize-v2.0.0")
                run.assert_not_called()

    def test_draft_upload_then_publish(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/tags/materialize-v2.0.0",
               "GITHUB_REPOSITORY": "example/tools", "GITHUB_SHA": "a" * 40}
        with patch.dict("os.environ", env, clear=True), patch("release.verify", return_value=[Path("asset.zip")]):
            with patch("release.subprocess.run") as run:
                release.publish(Path("unused"), "materialize-v2.0.0")
                self.assertEqual([call.args[0][2] for call in run.call_args_list], ["create", "upload", "edit"])
                self.assertIn("--draft", run.call_args_list[0].args[0])
                self.assertNotIn("--clobber", str(run.call_args_list))


if __name__ == "__main__":
    unittest.main()

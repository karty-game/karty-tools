# Materialize

Builds the Rust/wgpu **Materialize CLI 2.0.0** subtree of AiGameKit, not the
Unity application or the separate repository mentioned by its Cargo manifest.
[sources.json](sources.json) pins upstream commit
`1a3fe7d052a464d3217ef66d2bce9889d3946792`, archive SHA-256
`404f996ca4e3d36c668c31feec89feac578068d133f94efc6249cbb083631abc`,
Rust **1.99.0** and Python **3.12.12**. No Crunch assets are used.

[vramd-platform.patch](patches/vramd-platform.patch) gates the optional vramd
Unix-socket imports and implementation to Unix platforms. Non-Unix callers
receive an explicit `VramdError::Unavailable`; standard GPU map generation is
unchanged. The patch is checked and applied only to verified generated sources,
isolated from parent Git repository discovery, then reverse-checked to ensure
it actually changed the sources. It is bundled with its SHA-256 recorded in
build metadata.

## Build and replay

Use the root mise test/build tasks. Alternatively invoke `python materialize/build.py
build --target <native-target>` with the pinned toolchain on PATH. The script
rejects cross-builds; output defaults to ignored `dist/materialize/artifacts`.
Custom work/output paths must stay inside this repository's real `dist/` tree.
The script checks source SHA-256 before safe subtree extraction and checks the
upstream Cargo manifest and license before compiling.

[Cargo.lock](Cargo.lock) was genuinely generated using Cargo 1.99.0 against that
source. All four runners use the same committed lock (211 resolved packages),
whose SHA-256 is recorded in the manifest. Build, fetch and metadata commands use
`--locked`; builds never run `generate-lockfile`. For an intentional upstream or
dependency update, resolve in a disposable generated source tree with the pinned
Cargo, review the lock/version/licenses, then update the committed lock and
source/manifest/lock hashes together. Do not substitute an unverified hash.

Each `materialize-<version>-<target>.zip` contains the native executable, shared
lock, source pins, [notices](notices/Materialize-LICENSE), available dependency
license/notice files and SPDX identities, metadata, and per-file SHA256SUMS.
Metadata records missing dependency notice files for license review, not a claim
of legal completeness. Each ZIP has a SHA-256 sidecar. File order, timestamps,
permissions and checksum line endings are normalized; runner SDKs, linkers,
system libraries and build paths are not immutable, so **byte-identical binaries
are not promised**. Linux inherits Ubuntu 24.04's libc baseline; macOS deployment
versions, signing/notarization and Windows redistributables need separate review.

## Execution versus GPU support

[smoke.py](smoke.py) executes `--help`, `--version`, and `--list-maps` on every
native build before packaging. These commands do not initialize GPU compute.
For optional real map generation, invoke the smoke script with `--bin
dist/materialize/bundles/<target>/bin --work dist/materialize/gpu-smoke --gpu`.
It generates a PNG fixture and requires actual height/normal PNG outputs with
matching dimensions. GPU/driver/backend failures are errors, never skipped or
reported as success. Vulkan/Metal/DX12 availability is machine-specific; CI does
not run this check and makes no claim of GPU-output determinism or browser/WASM
support. Unit tests use mocks/fixtures and are not native or GPU execution.

The tag-only release path is described in the root README. The release helper
verifies all four targets, source/lock identities and internal/outer checksums
before uploading. Building or checking locally never accesses GitHub releases.
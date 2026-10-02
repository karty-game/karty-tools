# Karty Tools

Public native developer tools, organized as **one lower-case folder per tool**.
Each folder owns its source pins, shared dependency lock, notices, build and smoke
scripts. Builds need no sibling repositories or private credentials.

| Tool | Source and build details | Release tag |
| --- | --- | --- |
| Materialize (Rust/wgpu PBR-map CLI) | [materialize/README.md](materialize/README.md) | `materialize-v2.0.0` |

The [Materialize workflow](.github/workflows/materialize.yml) checks pull requests
and pushes and natively builds `linux-amd64`, `linux-arm64`, `darwin-arm64`, and
`windows-amd64` using Ubuntu 24.04, Ubuntu 24.04 ARM, macOS 14 and Windows 2022.
Every build must execute help/version/list-maps before an archive is produced.
The matrix is configured, not evidence that all platforms have already passed.
GPU map generation is a separate, optional runtime check, not a compilation claim.

`windows-arm64` and `darwin-amd64` are **unsupported developer tool targets**.
These requirements are distinct from Karty game-runtime targets, including
Windows ARM64 and browser/WASM; this repository does not build game hosts. Original CLI scaffolding remains untouched pending
explicit cleanup; this repository's builds do not read it.

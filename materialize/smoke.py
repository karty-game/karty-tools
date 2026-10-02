#!/usr/bin/env python3
"""Native CLI execution; opt-in --gpu performs separate real map generation."""

import argparse
import binascii
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import zlib


def png(size=32):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xffffffff)
    rows = b"".join(b"\0" + b"".join(bytes((x * 255 // (size - 1), y * 255 // (size - 1), 128, 255))
                                   for x in range(size)) for y in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def smoke(binaries, work, gpu=False):
    from build import generated_path
    work = generated_path(work)
    work.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if (binaries / "materialize-cli.exe").is_file() else ""
    executable = (binaries / ("materialize-cli" + suffix)).resolve()
    for arg in ("--help", "--version", "--list-maps"):
        subprocess.run([str(executable), arg], check=True, timeout=60)
    result = {"cli": "help/version/list-maps executed", "gpu": "not tested"}
    if gpu:
        directory = generated_path(work / "maps")
        directory.mkdir(parents=True, exist_ok=True)
        image = directory / "fixture.png"
        image.write_bytes(png())
        expected = [directory / ("fixture_" + name + ".png") for name in ("height", "normal")]
        for path in expected:
            path.unlink(missing_ok=True)
        subprocess.run([str(executable), str(image), "--output", str(directory),
                        "--only", "height,normal", "--format", "png"], check=True, timeout=180)
        hashes = {}
        for path in expected:
            data = path.read_bytes()
            if (len(data) < 33 or not data.startswith(b"\x89PNG\r\n\x1a\n")
                    or data[12:16] != b"IHDR" or struct.unpack(">II", data[16:24]) != (32, 32)):
                raise ValueError("GPU map missing expected PNG dimensions")
            hashes[path.name] = hashlib.sha256(data).hexdigest()
        result.update(gpu="actual height/normal map generation executed", maps_sha256=hashes)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--gpu", action="store_true")
    args = parser.parse_args()
    print(json.dumps(smoke(args.bin, args.work, args.gpu), indent=2))
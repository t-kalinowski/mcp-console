#!/usr/bin/env python3
"""Name one exported OCI image by its manifest digest before sbx template load.

This explicit setup utility neither builds an image nor invokes a provider.
It preserves manifest and layer bytes and verifies the manifest SHA-256.
"""

import argparse
import copy
import hashlib
import io
import json
import tarfile
from pathlib import Path


def qualify(source: Path, destination: Path, repository: str) -> str:
    assert source.resolve() != destination.resolve()
    assert "/" in repository and "@" not in repository
    with tarfile.open(source) as archive:
        index_member = archive.getmember("index.json")
        index = json.load(archive.extractfile(index_member))
        [manifest] = index["manifests"]
        algorithm, digest = manifest["digest"].split(":")
        assert algorithm == "sha256"
        payload = archive.extractfile(f"blobs/sha256/{digest}").read()
        assert hashlib.sha256(payload).hexdigest() == digest
        assert json.loads(payload)["mediaType"] in (
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ), "export one platform image, not a multi-platform index"
        reference = f"{repository}@sha256:{digest}"
        manifest.setdefault("annotations", {}).update(
            {
                "io.containerd.image.name": reference,
                "org.opencontainers.image.ref.name": reference,
            }
        )
        encoded = json.dumps(index).encode()
        with tarfile.open(destination, "w") as output:
            for member in archive:
                if member.name == "manifest.json":
                    # Prefer the OCI index over Docker's legacy tag-only manifest.
                    continue
                if member.name == "index.json":
                    member = copy.copy(member)
                    member.size = len(encoded)
                    output.addfile(member, io.BytesIO(encoded))
                else:
                    output.addfile(
                        member, archive.extractfile(member) if member.isfile() else None
                    )
    return reference


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("repository")
    args = parser.parse_args()
    print(qualify(args.source, args.destination, args.repository))

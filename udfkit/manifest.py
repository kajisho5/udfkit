"""The checksum manifest of an image, and reading an image back against it.

Two files are written beside `disc.iso`:

* `disc.iso.sha256`: the SHA-256 of the whole image, in the format
  `sha256sum -c` and `shasum -a 256 -c` read (`<hex>  disc.iso`).
* `disc.iso.manifest.json`: the same SHA-256, the image size, and for every
  file on the disc its path, first sector, size and SHA-256.

The file hashes are taken from the image itself, not from the source
folder: each file's bytes are read back from the sectors the writer put
them in. A manifest therefore says what the image holds. `verify` reads the
image again in one pass and names each file whose bytes changed; damage
outside every file (the file system structures, padding) shows as an image
hash that differs while every file still matches.
"""

import hashlib
import json
import os

from .errors import UdfError

SECTOR = 2048
FORMAT = "udfkit-manifest/1"
_CHUNK = 1 << 20


def sha256_path(image):
    return image + ".sha256"


def manifest_path(image):
    return image + ".manifest.json"


def _hash_pass(image, extents):
    """Read `image` once. Returns (bytes read, image SHA-256, {path: SHA-256}).
    `extents` is {path: (first sector, bytes)}; files may not overlap."""
    spans = sorted(((s * SECTOR, s * SECTOR + n, p) for p, (s, n) in extents.items()),
                   key=lambda t: (t[0], t[1]))
    digests = {p: hashlib.sha256() for p in extents}
    whole = hashlib.sha256()
    active, nxt, pos = [], 0, 0
    try:
        with open(image, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                end = pos + len(chunk)
                whole.update(chunk)
                while nxt < len(spans) and spans[nxt][0] < end:
                    active.append(spans[nxt])
                    nxt += 1
                for start, stop, path in active:
                    lo, hi = max(start, pos), min(stop, end)
                    if lo < hi:
                        digests[path].update(chunk[lo - pos:hi - pos])
                active = [a for a in active if a[1] > end]
                pos = end
    except OSError as e:
        raise UdfError(f"cannot read {image}: {e.strerror}") from e
    return pos, whole.hexdigest(), {p: d.hexdigest() for p, d in digests.items()}


def write_manifest(image, extents):
    """Write `image`.sha256 and `image`.manifest.json beside `image`.
    `extents` is the "extents" a writer returns ({path: (first sector,
    bytes)}). Returns the two paths written."""
    image = os.path.abspath(image)
    size, whole, digests = _hash_pass(image, extents)
    for path, (sector, n) in extents.items():
        if sector * SECTOR + n > size:
            raise UdfError(f"{path} ends past the end of {image}")
    name = os.path.basename(image)
    doc = {
        "format": FORMAT,
        "image": name,
        "bytes": size,
        "sha256": whole,
        "sector_size": SECTOR,
        "files": [{"path": p, "sector": s, "bytes": n, "sha256": digests[p]}
                  for p, (s, n) in sorted(extents.items(), key=lambda kv: (kv[1][0], kv[0]))],
    }
    out_sum, out_json = sha256_path(image), manifest_path(image)
    with open(out_sum, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{whole}  {name}\n")
    with open(out_json, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return out_sum, out_json


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        raise UdfError(f"{path} does not exist",
                       "write the image with udfkit dvd or udfkit bluray to get a manifest, "
                       "or give one with --manifest") from None
    except (OSError, ValueError) as e:
        raise UdfError(f"cannot read the manifest {path}: {e}") from e
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise UdfError(f"{path} is not a udfkit manifest ({FORMAT})")
    try:
        files = {e["path"]: (int(e["sector"]), int(e["bytes"]), str(e["sha256"]))
                 for e in doc["files"]}
        return int(doc["bytes"]), str(doc["sha256"]), files
    except (KeyError, TypeError, ValueError) as e:
        raise UdfError(f"{path} is not a complete udfkit manifest") from e


def verify(image, manifest=None):
    """Read `image` back against its manifest (`image`.manifest.json unless
    `manifest` is given), and against `image`.sha256 when that is there.

    Returns {"image", "manifest", "ok", "bytes", "expected_bytes", "sha256",
    "expected_sha256", "files", "bad_files": [paths whose bytes differ],
    "outside_files": True when the image differs but every file matches,
    "sha256_file": True/False/None (the .sha256 file agrees / differs / is
    not there)}.
    """
    image = os.path.abspath(image)
    manifest = os.path.abspath(manifest or manifest_path(image))
    want_bytes, want_sha, files = _load(manifest)
    size, whole, digests = _hash_pass(image, {p: (s, n) for p, (s, n, _) in files.items()})
    bad = [p for p, (s, n, h) in sorted(files.items(), key=lambda kv: (kv[1][0], kv[0]))
           if digests[p] != h or s * SECTOR + n > size]
    sum_file = None
    try:
        with open(sha256_path(image), encoding="utf-8") as f:
            sum_file = f.read().split()[:1] == [want_sha]
    except FileNotFoundError:
        pass
    except OSError as e:
        raise UdfError(f"cannot read {sha256_path(image)}: {e.strerror}") from e
    image_ok = size == want_bytes and whole == want_sha
    return {
        "image": image,
        "manifest": manifest,
        "ok": image_ok and not bad and sum_file is not False,
        "bytes": size,
        "expected_bytes": want_bytes,
        "sha256": whole,
        "expected_sha256": want_sha,
        "files": len(files),
        "bad_files": bad,
        "outside_files": not image_ok and not bad,
        "sha256_file": sum_file,
    }

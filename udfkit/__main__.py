"""Command line: `udfkit dvd|bluray SRC IMAGE --label NAME [--no-manifest]`,
`udfkit verify IMAGE [--manifest PATH] [--json]`, `udfkit info IMAGE`,
`udfkit contract [--json]`."""

import argparse
import json
import sys

from . import __version__, bluray, dvd, info, manifest
from .contract import contract
from .errors import UdfError


def main(argv=None):
    p = argparse.ArgumentParser(prog="udfkit", description="Write DVD-Video and Blu-ray disc "
                                "images (UDF) without external tools.")
    p.add_argument("--version", action="version", version=f"udfkit {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    for name, what in (("dvd", "a folder with VIDEO_TS (and optional AUDIO_TS, DVD-ROM files)"),
                       ("bluray", "a folder with BDMV (and optional CERTIFICATE)")):
        s = sub.add_parser(name, help=f"write an image of {what}")
        s.add_argument("src", help=what)
        s.add_argument("image", help="the image file to write")
        s.add_argument("--label", required=True, help="volume label (A-Z, 0-9, _; 1-32)")
        s.add_argument("--no-manifest", action="store_true",
                       help="do not write IMAGE.sha256 and IMAGE.manifest.json beside the image")
    s = sub.add_parser("verify", help="read an image back against its checksum manifest")
    s.add_argument("image")
    s.add_argument("--manifest", help="the manifest (default: IMAGE.manifest.json)")
    s.add_argument("--json", action="store_true", help="print the result as JSON")
    s = sub.add_parser("info", help="the volume names an image carries")
    s.add_argument("image")
    s = sub.add_parser("contract", help="print the machine-readable contract (JSON)")
    s.add_argument("--json", action="store_true", help="accepted; the output is always JSON")
    args = p.parse_args(argv)
    if args.command == "contract":
        print(json.dumps(contract(), indent=2))
        return 0
    try:
        if args.command == "info":
            print(json.dumps(info.volume_names(args.image), indent=2, ensure_ascii=False))
            return 0
        if args.command == "verify":
            return _verify(args)
        writer = dvd if args.command == "dvd" else bluray
        stats = writer.write_image(args.src, args.image, args.label)
        made = [] if args.no_manifest else manifest.write_manifest(stats["path"], stats["extents"])
    except UdfError as e:
        print(f"udfkit: {e.message}", file=sys.stderr)
        if e.hint:
            print(f"  {e.hint}", file=sys.stderr)
        return 1
    print(f"{stats['path']}: {stats['bytes']} bytes, {stats['files']} files")
    for path in made:
        print(f"{path}: written")
    return 0


def _verify(args):
    r = manifest.verify(args.image, args.manifest)
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif r["ok"]:
        print(f"{r['image']}: OK, sha256 {r['sha256']}, {r['files']} files match the manifest")
    if r["ok"]:
        return 0
    name = r["image"]
    if r["bytes"] != r["expected_bytes"]:
        print(f"udfkit: {name} is {r['bytes']} bytes, the manifest says {r['expected_bytes']}",
              file=sys.stderr)
    for path in r["bad_files"]:
        print(f"udfkit: {name}: {path} does not match the manifest", file=sys.stderr)
    if r["outside_files"]:
        print(f"udfkit: {name}: the damage is outside every file (file system structures "
              "or padding)", file=sys.stderr)
    if r["sha256_file"] is False:
        print(f"udfkit: {manifest.sha256_path(name)} does not agree with the manifest",
              file=sys.stderr)
    print("  write the image again from its source folder, or copy it again", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""Command line: `udfkit dvd|bluray SRC IMAGE --label NAME`, `udfkit info IMAGE`."""

import argparse
import json
import sys

from . import __version__, bluray, dvd, info
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
    s = sub.add_parser("info", help="the volume names an image carries")
    s.add_argument("image")
    args = p.parse_args(argv)
    try:
        if args.command == "info":
            print(json.dumps(info.volume_names(args.image), indent=2, ensure_ascii=False))
            return 0
        writer = dvd if args.command == "dvd" else bluray
        stats = writer.write_image(args.src, args.image, args.label)
    except UdfError as e:
        print(f"udfkit: {e.message}", file=sys.stderr)
        if e.hint:
            print(f"  {e.hint}", file=sys.stderr)
        return 1
    print(f"{stats['path']}: {stats['bytes']} bytes, {stats['files']} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())

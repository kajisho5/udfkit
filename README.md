<p><img src="docs/images/logo/lockup-dark.svg" alt="udfkit" height="56"></p>

# udfkit

Write **DVD-Video** and **Blu-ray** disc images in pure Python: no external
programs, no root, no dependencies beyond the standard library.

* **DVD-Video** (`udfkit.dvd`): UDF 1.02 + ISO 9660 "bridge", the file
  systems DVD players and libdvdread (VLC, mpv) read. The VIDEO_TS files are
  placed where their IFO files say they start, as `genisoimage -dvd-video
  -udf` does, and the image comes out the same size with the same sector map.
* **Blu-ray** (`udfkit.bluray`): UDF 2.50 with a metadata partition, as
  BD-ROM discs use and libbluray (through libudfread) reads.
* **Volume names** (`udfkit.info`): the ISO 9660 and UDF names of an image.

Images are deterministic: the same files and date give the same bytes.

日本語の説明は [README.ja.md](README.ja.md) にあります。

## Why

Python's ISO library (pycdlib) writes UDF 2.60 only. DVD players expect UDF
1.02, and Blu-ray discs use 2.50. The other ways to make such images are:

* old command-line tools (mkisofs / genisoimage);
* macOS's own `hdiutil`;
* tools that need a mounted file system and root (udftools).

udfkit runs anywhere Python runs.

## Install

```sh
pip install udfkit
```

A release is made by the version: when the version in `pyproject.toml`
(and `udfkit/__init__.py`) reaches `main` without a tag of its own,
`.github/workflows/release.yml` runs the tests, builds the package, makes
the GitHub release with its tag and publishes to PyPI (trusted publishing,
environment `pypi`). Merging the version bump is all it takes.

## Use

```sh
udfkit dvd    path/to/disc  disc.iso --label MY_DISC   # disc/ holds VIDEO_TS (and AUDIO_TS, extras)
udfkit bluray path/to/bd    bd.iso   --label MY_BD     # bd/ holds BDMV
udfkit info   disc.iso
udfkit contract --json                               # machine-readable contract (AI Video Production OS registry)
```

```python
from udfkit import dvd, bluray

stats = dvd.write_image("disc", "disc.iso", "MY_DISC")
print(stats["bytes"], stats["extents"]["/VIDEO_TS/VTS_01_1.VOB"])   # (first sector, size)

bluray.write_image("bd", "bd.iso", "MY_BD")
```

`dvd.write_image(..., shift=n)` puts `n` empty sectors before VIDEO_TS. It is
used to put a dual-layer disc's layer break on an ECC block (16 sectors).

## Tests

```sh
python -m unittest discover -s tests -t .
```

The tests read every structure back with their own reader, independent of
the writer: every descriptor tag, checksum and CRC, and the UDF and ISO 9660
directories. When these tools are installed, the tests also check:

* **isoinfo** lists and extracts the files of a DVD image;
* **libudfread** (the reader libbluray uses) reads every file of a Blu-ray
  image back;
* **ffmpeg + dvdauthor + genisoimage**: a real VIDEO_TS gives the same image
  size and sector map as `genisoimage -dvd-video -udf`.

udfkit was first written for [oncemore](https://github.com/kajisho5/oncemore),
a DVD and Blu-ray authoring app. There its images are played back with
libdvdnav and libbluray on every change.

## Support

udfkit is free and stays free. If it saves you time, you can support its
development through [GitHub Sponsors](https://github.com/sponsors/kajisho5)
(the **Sponsor** button at the top of this repository). Bug reports help just
as much: [Issues](https://github.com/kajisho5/udfkit/issues).

The logo, colours and writing rules: [docs/DESIGN.md](docs/DESIGN.md).

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

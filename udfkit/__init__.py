"""udfkit: write DVD-Video and Blu-ray disc images in pure Python.

* `udfkit.dvd.write_image(src, out, label)`: a DVD-Video image (UDF 1.02 +
  ISO 9660 "bridge"), its VIDEO_TS files placed where their IFO files say.
* `udfkit.bluray.write_image(src, out, label)`: a Blu-ray image (UDF 2.50
  with a metadata partition), as BD-ROM players and libbluray read it.
* `udfkit.info.volume_names(image)`: the volume names an image carries.
* `udfkit.manifest.write_manifest(image, stats["extents"])` and
  `udfkit.manifest.verify(image)`: a SHA-256 manifest beside an image, and
  the image read back against it.

No external programs and no dependencies beyond the standard library.
"""

__version__ = "0.2.0"

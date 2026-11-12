# ADR 0001: Checksum manifest beside the image, verified by reading the image back

Status: accepted (2026-11-12)

## Context

A disc image goes to a pressing plant or a duplicator, often through several
copies. The person sending it needs to show that the image arriving is the
image that was written, and, when it is not, which part of the disc is
damaged: a broken VOB or m2ts needs the disc rewritten, a broken byte in
padding may not matter.

## Decision

* `udfkit dvd` and `udfkit bluray` write two files beside `IMAGE`
  (`--no-manifest` leaves them out):
  * `IMAGE.sha256`: one line, `<hex>  <image name>`, the format `sha256sum -c`
    and `shasum -a 256 -c` read, for anyone without udfkit;
  * `IMAGE.manifest.json` (`"format": "udfkit-manifest/1"`): the image's size
    and SHA-256, and for every file its path, first sector, size and SHA-256.
* The per-file hashes are taken from the written image at the sectors the
  writer placed each file, not from the source folder. The manifest then
  describes the image itself, and writing it is already one read-back of the
  image.
* `udfkit verify IMAGE` reads the image once, sequentially, and hashes the
  whole image and every file's byte range in the same pass (a 50 GB image is
  read once, not twice). It names every file whose bytes differ. When the
  image hash differs and every file matches, the damage is outside the files
  (file system structures or padding) and is reported as such. When
  `IMAGE.sha256` is present and disagrees with the manifest, that is a
  failure too.
* The file locations come from the writer (`stats["extents"]`), so verify
  does not need its own UDF directory reader. Each file is one contiguous
  extent in both writers.
* The Python writers (`dvd.write_image`, `bluray.write_image`) are unchanged;
  `udfkit.manifest.write_manifest(image, stats["extents"])` is a separate
  call. Existing callers (oncemore vendors the library) get no new files.
* The contract gains `udfkit/verify` and `disc_image.verify` (additive).

## Consequences

* Writing an image now reads it back once more (the manifest pass).
* A manifest is only as good as the image it was taken from: it proves later
  copies match the first write, not that the first write matches the source
  folder. The writer already checks each source file did not change size
  while it was copied.
* Only SHA-256 is written. Other digests (MD5 for older checklists) can be taken from the image with standard tools.

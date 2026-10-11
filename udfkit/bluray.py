"""Write a Blu-ray disc image (UDF 2.50) in pure Python.

Blu-ray discs use UDF 2.50 only, no ISO 9660. What sets 2.50 apart from the
UDF 1.02 of DVD-Video (udfwriter.py) is the metadata partition: the file
system's own structures (file set, directories, file entries) live in a
"metadata file" inside the physical partition, recorded twice (the main
file and its mirror) so one damaged copy does not lose the directory tree.
Readers find it through a second (type 2) partition map.

  sector    contents
  0-15      zeros
  16-18     Volume Recognition Sequence: BEA01, NSR03, TEA01
  32-47     Main Volume Descriptor Sequence: Primary VD, Implementation Use
            VD ("*UDF LV Info"), Partition D, Logical Volume D (a type 1
            map and a metadata partition map), Unallocated Space D,
            Terminating D
  48-63     Reserve Volume Descriptor Sequence (the same)
  64-65     Logical Volume Integrity Sequence: LVID, Terminating D
  256       Anchor Volume Descriptor Pointer
  P = 257   partition block 0: Extended File Entry of the metadata file
            partition block 1: Extended File Entry of the metadata mirror
            partition block 32: the metadata file (M blocks, 32-block units):
              block 0, 1      File Set Descriptor, Terminating D
              then            each directory: File Entry + File Identifier
                              Descriptors (root first, pre-order)
              then            one File Entry per file
            then the file data, each file contiguous, in tree order
            then the metadata mirror (the same M blocks, 32-block aligned)
  N-1       Anchor Volume Descriptor Pointer again

Locations inside the metadata file are metadata-partition blocks (block 0
is the file set); File Entries point at the data with long_ad descriptors
naming the physical partition.
"""

import datetime
import os
import struct
import zlib

from .errors import UdfError
from .dvd import (ACCESS_READ_ONLY, DOMAIN_FLAGS, DOMAIN_ID, FID_DIRECTORY,
                        FID_PARENT, FIRST_UNIQUE_ID, ICB_FLAGS, ICB_STRATEGY,
                        IMPLEMENTATION_ID, INTEGRITY_CLOSE, LV_INFO_ID, MAX_EXTENT,
                        PARTITION_FLAG_ALLOCATED, PERM_DIRECTORY, PERM_FILE, SECTOR,
                        TAG_ANCHOR_POINTER, TAG_FILE_ENTRY, TAG_FILE_IDENTIFIER,
                        TAG_FILE_SET, TAG_IMPLEMENTATION_USE, TAG_LOGICAL_VOLUME,
                        TAG_LOGICAL_VOLUME_INTEGRITY, TAG_PARTITION, TAG_PRIMARY_VOLUME,
                        TAG_TERMINATING, TAG_UNALLOCATED_SPACE, UID_NONE, charspec, cs0,
                        crc_itu_t, dstring, entity_id, tag_checksum, udf_timestamp,
                        validate_label, volume_recognition, _CHUNK, _sectors)

UDF_REVISION = 0x0250
DESCRIPTOR_VERSION = 3              # NSR03
PARTITION_CONTENTS_ID = b"+NSR03"
METADATA_PARTITION_ID = b"*UDF Metadata Partition"
TAG_EXTENDED_FILE_ENTRY = 266
FILE_TYPE_DIRECTORY = 4
FILE_TYPE_FILE = 5
FILE_TYPE_METADATA = 250
FILE_TYPE_METADATA_MIRROR = 251
AD_SHORT, AD_LONG = 0, 1            # ICB flags bits 0-2 (ECMA-167 4/14.6.8)

VRS_SECTOR = 16
MAIN_VDS_SECTOR = 32
RESERVE_VDS_SECTOR = 48
VDS_SECTORS = 16
LVIS_SECTOR = 64
LVIS_SECTORS = 2
ANCHOR_SECTOR = 256
PARTITION_START = 257
META_UNIT = 32                      # allocation and alignment unit of the metadata file
META_START = 32                     # partition block where the metadata file begins
PHYSICAL, METADATA = 0, 1           # partition reference numbers (map order)


def _tagged(buf, tag_id, location):
    struct.pack_into("<HHBBHHHI", buf, 0, tag_id, DESCRIPTOR_VERSION, 0, 0, 0,
                     crc_itu_t(buf[16:]), len(buf) - 16, location)
    buf[4] = tag_checksum(buf)
    return bytes(buf)


def _domain_id():
    return entity_id(DOMAIN_ID, struct.pack("<HB", UDF_REVISION, DOMAIN_FLAGS))


def _impl_id():
    return entity_id(IMPLEMENTATION_ID)


def _align(n, unit):
    return -(-n // unit) * unit


# ==============================================================================
# volume structures
# ==============================================================================

def anchor_pointer(location):
    buf = bytearray(512)
    struct.pack_into("<IIII", buf, 16, VDS_SECTORS * SECTOR, MAIN_VDS_SECTOR,
                     VDS_SECTORS * SECTOR, RESERVE_VDS_SECTOR)
    return _tagged(buf, TAG_ANCHOR_POINTER, location)


def primary_volume(location, label, volume_set, date):
    buf = bytearray(512)
    buf[24:56] = dstring(label, 32)
    struct.pack_into("<HHHHII", buf, 56, 1, 1, 2, 2, 1, 1)
    buf[72:200] = dstring(volume_set, 128)
    buf[200:264] = charspec()
    buf[264:328] = charspec()
    buf[376:388] = udf_timestamp(date)
    buf[388:420] = _impl_id()
    return _tagged(buf, TAG_PRIMARY_VOLUME, location)


def implementation_use_volume(location, label):
    buf = bytearray(512)
    struct.pack_into("<I", buf, 16, 1)
    buf[20:52] = entity_id(LV_INFO_ID, struct.pack("<H", UDF_REVISION))
    buf[52:116] = charspec()
    buf[116:244] = dstring(label, 128)
    buf[352:384] = _impl_id()
    return _tagged(buf, TAG_IMPLEMENTATION_USE, location)


def partition(location, start, length):
    buf = bytearray(512)
    struct.pack_into("<IHH", buf, 16, 2, PARTITION_FLAG_ALLOCATED, 0)
    buf[24:56] = entity_id(PARTITION_CONTENTS_ID)
    struct.pack_into("<III", buf, 184, ACCESS_READ_ONLY, start, length)
    buf[196:228] = _impl_id()
    return _tagged(buf, TAG_PARTITION, location)


def metadata_partition_map(meta_block, mirror_block):
    """UDF 2.50 2.2.10 Metadata Partition Map (a type 2 map, 64 bytes)."""
    m = bytearray(64)
    m[0], m[1] = 2, 64
    m[4:36] = entity_id(METADATA_PARTITION_ID, struct.pack("<H", UDF_REVISION))
    struct.pack_into("<HHIIIIHB", m, 36, 1, 0, meta_block, mirror_block, 0xFFFFFFFF,
                     META_UNIT, META_UNIT, 1)        # no bitmap; the mirror duplicates
    return bytes(m)


def logical_volume(location, label, meta_block, mirror_block):
    maps = struct.pack("<BBHH", 1, 6, 1, 0) + metadata_partition_map(meta_block, mirror_block)
    buf = bytearray(440 + len(maps))
    struct.pack_into("<I", buf, 16, 3)
    buf[20:84] = charspec()
    buf[84:212] = dstring(label, 128)
    struct.pack_into("<I", buf, 212, SECTOR)
    buf[216:248] = _domain_id()
    # the File Set Descriptor: metadata partition, block 0
    struct.pack_into("<IIH", buf, 248, 2 * SECTOR, 0, METADATA)
    struct.pack_into("<II", buf, 264, len(maps), 2)
    buf[272:304] = _impl_id()
    struct.pack_into("<II", buf, 432, LVIS_SECTORS * SECTOR, LVIS_SECTOR)
    buf[440:] = maps
    return _tagged(buf, TAG_LOGICAL_VOLUME, location)


def unallocated_space(location):
    buf = bytearray(24)
    struct.pack_into("<II", buf, 16, 4, 0)
    return _tagged(buf, TAG_UNALLOCATED_SPACE, location)


def terminating(location):
    return _tagged(bytearray(512), TAG_TERMINATING, location)


def logical_volume_integrity(location, date, next_unique_id, sizes, files, directories):
    """Closed LVID; `sizes` is the size of each partition (physical, metadata)."""
    n = len(sizes)
    buf = bytearray(80 + 8 * n + 46)
    buf[16:28] = udf_timestamp(date)
    struct.pack_into("<I", buf, 28, INTEGRITY_CLOSE)
    struct.pack_into("<Q", buf, 40, next_unique_id)
    struct.pack_into("<II", buf, 72, n, 46)
    for i, size in enumerate(sizes):
        struct.pack_into("<II", buf, 80 + 4 * i, 0, 0)               # free space: none
        struct.pack_into("<I", buf, 80 + 4 * n + 4 * i, size)
    imp = 80 + 8 * n
    buf[imp:imp + 32] = _impl_id()
    struct.pack_into("<IIHHH", buf, imp + 32, files, directories,
                     UDF_REVISION, UDF_REVISION, UDF_REVISION)
    return _tagged(buf, TAG_LOGICAL_VOLUME_INTEGRITY, location)


# ==============================================================================
# file structures
# ==============================================================================

def file_set(label, root_block, date):
    buf = bytearray(512)
    buf[16:28] = udf_timestamp(date)
    struct.pack_into("<HHIIII", buf, 28, 3, 3, 1, 1, 0, 0)
    buf[48:112] = charspec()
    buf[112:240] = dstring(label, 128)
    buf[240:304] = charspec()
    buf[304:336] = dstring(label, 32)
    struct.pack_into("<IIH", buf, 400, SECTOR, root_block, METADATA)
    buf[416:448] = _domain_id()
    return _tagged(buf, TAG_FILE_SET, 0)


def file_identifier(block, name, characteristics, icb_block, unique_id):
    ident = cs0(name) if name is not None else b""
    if len(ident) > 255:
        raise UdfError(f"file name too long for UDF: {name!r}")
    buf = bytearray((38 + len(ident) + 3) & ~3)
    struct.pack_into("<HBB", buf, 16, 1, characteristics, len(ident))
    struct.pack_into("<IIHHIH", buf, 20, SECTOR, icb_block, METADATA, 0,
                     unique_id & 0xFFFFFFFF, 0)
    buf[38:38 + len(ident)] = ident
    return _tagged(buf, TAG_FILE_IDENTIFIER, block)


def _extents(size, block):
    out, remaining = [], size
    while remaining > 0:
        chunk = min(remaining, MAX_EXTENT)
        out.append((chunk, block))
        block += chunk // SECTOR
        remaining -= chunk
    return out


def _icb_tag(buf, file_type, ad_type):
    struct.pack_into("<IHHHBB", buf, 16, 0, ICB_STRATEGY, 0, 1, 0, file_type)
    struct.pack_into("<H", buf, 34, ICB_FLAGS | ad_type)


def file_entry(block, is_dir, size, data_block, unique_id, link_count, date):
    """File Entry in the metadata partition. A directory's identifiers are
    in the metadata partition too (short_ad); a file's data is in the
    physical partition (long_ad)."""
    if is_dir:
        ads = b"".join(struct.pack("<II", n, b) for n, b in _extents(size, data_block))
    else:
        ads = b"".join(struct.pack("<IIH6x", n, b, PHYSICAL)
                       for n, b in _extents(size, data_block))
    if 176 + len(ads) > SECTOR:
        raise UdfError("file too large for one UDF File Entry")
    buf = bytearray(176 + len(ads))
    _icb_tag(buf, FILE_TYPE_DIRECTORY if is_dir else FILE_TYPE_FILE,
             AD_SHORT if is_dir else AD_LONG)
    struct.pack_into("<IIIHBBI", buf, 36, UID_NONE, UID_NONE,
                     PERM_DIRECTORY if is_dir else PERM_FILE, link_count, 0, 0, 0)
    struct.pack_into("<QQ", buf, 56, size, _sectors(size))
    stamp = udf_timestamp(date)
    buf[72:84] = buf[84:96] = buf[96:108] = stamp
    struct.pack_into("<I", buf, 108, 1)
    buf[128:160] = _impl_id()
    struct.pack_into("<QII", buf, 160, unique_id, 0, len(ads))
    buf[176:] = ads
    return _tagged(buf, TAG_FILE_ENTRY, block)


def metadata_file_entry(block, file_type, start, blocks, date):
    """ECMA-167 4/14.17 Extended File Entry of the metadata file or its
    mirror: one short_ad extent in the physical partition."""
    ads = struct.pack("<II", blocks * SECTOR, start)
    buf = bytearray(216 + len(ads))
    _icb_tag(buf, file_type, AD_SHORT)
    struct.pack_into("<IIIHBBI", buf, 36, UID_NONE, UID_NONE, 0, 1, 0, 0, 0)
    struct.pack_into("<QQQ", buf, 56, blocks * SECTOR, blocks * SECTOR, blocks)
    stamp = udf_timestamp(date)
    buf[80:92] = buf[92:104] = buf[104:116] = buf[116:128] = stamp
    struct.pack_into("<I", buf, 128, 1)
    buf[168:200] = _impl_id()
    struct.pack_into("<QII", buf, 200, 0, 0, len(ads))
    buf[216:] = ads
    return _tagged(buf, TAG_EXTENDED_FILE_ENTRY, block)


# ==============================================================================
# the tree
# ==============================================================================

class _Node:
    __slots__ = ("name", "src", "is_dir", "size", "children", "parent", "fe_block",
                 "dir_bytes", "unique_id", "block")

    def __init__(self, name, src, is_dir, size=0, parent=None):
        self.name, self.src, self.is_dir, self.size, self.parent = name, src, is_dir, size, parent
        self.children = []
        self.fe_block = self.dir_bytes = self.unique_id = self.block = None

    def path(self):
        parts, n = [], self
        while n.parent is not None:
            parts.append(n.name)
            n = n.parent
        return "/" + "/".join(reversed(parts))

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


def scan_tree(src_dir, skip=None):
    """Everything under src_dir except hidden entries, by name."""
    root = _Node("", src_dir, True)

    def scan(node, path):
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except OSError as e:
            raise UdfError(f"cannot read {path}: {e.strerror}") from e
        for e in entries:
            if e.name.startswith(".") or os.path.realpath(e.path) == skip:
                continue
            if e.is_dir():
                child = _Node(e.name, e.path, True, parent=node)
                scan(child, e.path)
            elif e.is_file():
                child = _Node(e.name, e.path, False, e.stat().st_size, parent=node)
            else:
                continue
            node.children.append(child)
    scan(root, src_dir)
    return root


# ==============================================================================
# writing
# ==============================================================================

def write_image(src_dir, out_path, label, *, date=None, on_progress=None):
    """Write a UDF 2.50 image of `src_dir` (a BDMV disc folder) to out_path.
    Returns statistics like udfkit.dvd.write_image."""
    validate_label(label)
    if date is None:
        date = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    out_path = os.path.abspath(out_path)
    root = scan_tree(src_dir, skip=os.path.realpath(out_path))
    if not any(c.name == "BDMV" and c.is_dir for c in root.children):
        raise UdfError(f"{src_dir} has no BDMV directory")
    layout = _plan(root)
    try:
        _write(root, layout, out_path, label, date, on_progress)
    except BaseException:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise
    files = [n for n in root.walk() if not n.is_dir]
    return {"path": out_path, "sectors": layout["total"], "bytes": layout["total"] * SECTOR,
            "files": len(files), "directories": len(layout["dirs"]), "label": label,
            "udf_revision": "2.50",
            "extents": {n.path(): (PARTITION_START + n.block, n.size) for n in files}}


def _plan(root):
    dirs = [n for n in root.walk() if n.is_dir]
    files = [n for n in root.walk() if not n.is_dir]
    block, uid = 2, FIRST_UNIQUE_ID                 # after the file set and its terminator
    for d in dirs:
        if d is root:
            d.unique_id = 0
        else:
            d.unique_id, uid = uid, uid + 1
        d.dir_bytes = len(file_identifier(0, None, 0, 0, 0)) + sum(
            len(file_identifier(0, c.name, 0, 0, 0)) for c in d.children)
        d.fe_block = block
        block += 1 + _sectors(d.dir_bytes)
    for f in files:
        f.unique_id, uid = uid, uid + 1
        f.fe_block = block
        block += 1
    meta_blocks = _align(block, META_UNIT)
    pos = META_START + meta_blocks
    for f in files:
        f.block = pos if f.size else 0
        pos += _sectors(f.size)
    mirror = _align(pos, META_UNIT)
    length = mirror + meta_blocks
    return {"dirs": dirs, "files": files, "meta_blocks": meta_blocks, "mirror": mirror,
            "data_start": META_START + meta_blocks, "data_end": pos,
            "partition_length": length, "total": PARTITION_START + length + 1,
            "next_unique_id": uid}


def _metadata(root, layout, label, date):
    """The metadata file's blocks."""
    meta = bytearray(layout["meta_blocks"] * SECTOR)

    def put(block, data):
        meta[block * SECTOR:block * SECTOR + len(data)] = data

    put(0, file_set(label, root.fe_block, date))
    put(1, terminating(1))
    for d in layout["dirs"]:
        links = 1 + sum(1 for c in d.children if c.is_dir)
        first = d.fe_block + 1
        put(d.fe_block, file_entry(d.fe_block, True, d.dir_bytes, first, d.unique_id, links,
                                   date))
        parent = d.parent or d
        fids = bytearray(file_identifier(first, None, FID_DIRECTORY | FID_PARENT,
                                         parent.fe_block, parent.unique_id))
        for c in d.children:
            fids += file_identifier(first + len(fids) // SECTOR, c.name,
                                    FID_DIRECTORY if c.is_dir else 0, c.fe_block, c.unique_id)
        put(first, fids)
    for f in layout["files"]:
        put(f.fe_block, file_entry(f.fe_block, False, f.size, f.block, f.unique_id, 1, date))
    return bytes(meta)


def _write(root, layout, out_path, label, date, on_progress):
    p_start, p_len = PARTITION_START, layout["partition_length"]
    head = bytearray((p_start + META_START) * SECTOR)

    def put(sector, data):
        head[sector * SECTOR:sector * SECTOR + len(data)] = data

    for i, ident in enumerate((b"BEA01", b"NSR03", b"TEA01")):
        put(VRS_SECTOR + i, volume_recognition(ident))
    utc = date if date.tzinfo else date.replace(tzinfo=datetime.timezone.utc)
    volume_set = "%08X%08X" % (int(utc.timestamp()) & 0xFFFFFFFF,
                               zlib.crc32(label.encode("ascii")))
    for base in (MAIN_VDS_SECTOR, RESERVE_VDS_SECTOR):
        put(base + 0, primary_volume(base + 0, label, volume_set, date))
        put(base + 1, implementation_use_volume(base + 1, label))
        put(base + 2, partition(base + 2, p_start, p_len))
        put(base + 3, logical_volume(base + 3, label, 0, 1))
        put(base + 4, unallocated_space(base + 4))
        put(base + 5, terminating(base + 5))
    put(LVIS_SECTOR, logical_volume_integrity(
        LVIS_SECTOR, date, layout["next_unique_id"], [p_len, layout["meta_blocks"]],
        len(layout["files"]), len(layout["dirs"])))
    put(LVIS_SECTOR + 1, terminating(LVIS_SECTOR + 1))
    put(ANCHOR_SECTOR, anchor_pointer(ANCHOR_SECTOR))
    put(p_start + 0, metadata_file_entry(0, FILE_TYPE_METADATA, META_START,
                                         layout["meta_blocks"], date))
    put(p_start + 1, metadata_file_entry(1, FILE_TYPE_METADATA_MIRROR, layout["mirror"],
                                         layout["meta_blocks"], date))
    meta = _metadata(root, layout, label, date)

    total_bytes = layout["total"] * SECTOR
    done = 0

    def progress(n):
        nonlocal done
        done += n
        if on_progress:
            on_progress(min(1.0, done / total_bytes))

    zeros = bytes(_CHUNK)
    with open(out_path, "wb") as out:
        out.write(head)
        out.write(meta)
        progress(len(head) + len(meta))
        for f in layout["files"]:
            if not f.size:
                continue
            _copy_file(f, out, progress)
            pad = -f.size % SECTOR
            out.write(zeros[:pad])
            progress(pad)
        gap = (layout["mirror"] - layout["data_end"]) * SECTOR
        out.write(zeros[:gap] if gap <= len(zeros) else bytes(gap))
        out.write(meta)
        out.write(anchor_pointer(layout["total"] - 1).ljust(SECTOR, b"\0"))
        progress(gap + len(meta) + SECTOR)
        if out.tell() != total_bytes:
            raise UdfError(f"internal error: wrote {out.tell()} bytes, "
                                f"expected {total_bytes}")


def _copy_file(node, out, progress):
    remaining = node.size
    try:
        with open(node.src, "rb") as f:
            while remaining > 0:
                chunk = f.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
                progress(len(chunk))
            extra = f.read(1)
    except OSError as e:
        raise UdfError(f"cannot read {node.src}: {e.strerror}") from e
    if remaining or extra:
        raise UdfError(f"{node.src} changed size while the image was written")

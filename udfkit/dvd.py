"""Write a DVD-Video disc image (UDF 1.02 + ISO 9660 "bridge") in pure Python.

DVD players and libdvdread find VIDEO_TS through UDF; older systems and
`isoinfo` read the ISO 9660 side. Both file systems describe the same file
data, which is stored once. The layout follows what `genisoimage -dvd-video
-udf` writes, so images are the same size and have the same structure:

  sector    contents
  0-15      system area (zeros)
  16        ISO 9660 Primary Volume Descriptor
  17        ISO 9660 Volume Descriptor Set Terminator
  18-20     UDF Volume Recognition Sequence: BEA01, NSR02, TEA01
  32-47     UDF Main Volume Descriptor Sequence: Primary VD, Implementation
            Use VD ("*UDF LV Info"), Partition D, Logical Volume D, Unallocated
            Space D, Terminating D (16 sectors reserved, ECMA-167 3/8.4.2)
  48-63     UDF Reserve Volume Descriptor Sequence (the same, relocated)
  64-65     UDF Logical Volume Integrity Sequence: LVID, Terminating D
  256       UDF Anchor Volume Descriptor Pointer
  P = 257   UDF partition start (partition block 0):
              block 0, 1      File Set Descriptor, Terminating D
              then            each directory: File Entry + its File
                              Identifier Descriptors (root first, pre-order)
              then            one File Entry per file
            ISO 9660 type L and type M path tables (each rounded up to an
            even number of sectors, as genisoimage does)
            ISO 9660 directories (path table order)
  D         file data, sector aligned, each file contiguous: VIDEO_TS first
            (DVD order, see below), then AUDIO_TS, then DVD-ROM content
  E         end of the partition; E .. N-1 hold the Anchor Volume Descriptor
            Pointer again (151 sectors, like genisoimage, which pads 150
            sectors after the end anchor). UDF wants an anchor at 256 and N-1;
            filling the whole tail means a reader that gets the disc size
            slightly wrong still finds one.

The 150-sector tail is not required by UDF; it is genisoimage's default
`-pad` and keeps the image exactly as large as the one genisoimage writes for
the same input, so size estimates made for either tool hold for both.

VIDEO_TS file order and placement
  VIDEO_TS.IFO, VIDEO_TS.VOB, VIDEO_TS.BUP, then for each title set NN:
  VTS_NN_0.IFO, VTS_NN_0.VOB, VTS_NN_1.VOB .. VTS_NN_9.VOB, VTS_NN_0.BUP.
  Players address title sets and their parts by sector offsets stored in the
  IFO files, so when the IFOs can be read their numbers decide where each file
  starts (relative to VIDEO_TS.IFO): the VMG menu VOB at vmgm_vobs, the VMG
  BUP so that it ends at vmg_last_sector, each title set at the start sector
  of the title search pointer table, and within a title set the menu VOB at
  vtsm_vobs, the title VOBs from vtstt_vobs on and the BUP ending at
  vts_last_sector. dvdauthor, for one, declares a 32-sector VMG even without
  a VIDEO_TS.VOB, so there is a gap between VIDEO_TS.IFO and VIDEO_TS.BUP,
  exactly as in genisoimage's image. Everything else is packed without gaps.
  Files that are not IFOs (test data) are simply packed in DVD order.

File names
  UDF keeps every name as it is (OSTA CS0: 8-bit when all characters are
  Latin-1, otherwise 16-bit; characters beyond U+FFFF become UTF-16
  surrogate pairs, which UDF 1.02 readers show as two unknown characters).
  Names in VIDEO_TS are upper-cased and must be 8.3.
  ISO 9660 gets level-1 names: upper case, A-Z 0-9 _, 8.3 for files (`NAME.EXT;1`,
  the dot is always there), 8 characters for directories. Other names are
  mapped: accents are stripped (NFKD), other characters become "_", the name
  is cut to 8.3 and a clash with a name already in the directory gets a
  "_N" suffix (`My long name.jpeg` -> `MY_LONG_.JPE`, then `MY_LON_1.JPE`).
  Names that are already valid keep their name. Hidden entries (".name",
  e.g. .DS_Store) are left out at every level.

Limits: VIDEO_TS files must not exceed 1 GiB (the DVD-Video limit); other
files must fit an ISO 9660 extent (< 4 GiB) and are stored in UDF as extents
of at most 0x3FFFF800 bytes. ISO 9660 allows 8 directory levels.

Timestamps all come from one `date` (default: now, UTC), so the same input
and date give a byte-identical image.
"""

import datetime
import os
import re
import struct
import unicodedata
import zlib

from .errors import UdfError

SECTOR = 2048

# --- sector map (absolute sectors) -------------------------------------------
ISO_PVD_SECTOR = 16
ISO_TERMINATOR_SECTOR = 17
VRS_SECTOR = 18                    # BEA01, NSR02, TEA01
MAIN_VDS_SECTOR = 32
RESERVE_VDS_SECTOR = 48
VDS_SECTORS = 16                   # ECMA-167 3/8.4.2: at least 16 sectors
LVIS_SECTOR = 64                   # Logical Volume Integrity Sequence
LVIS_SECTORS = 2
ANCHOR_SECTOR = 256                # ECMA-167 3/8.4.2.1, UDF 2.2.3
PARTITION_START = 257
TAIL_SECTORS = 151                 # end anchor + genisoimage's 150-sector pad

# --- ECMA-167 descriptor tag identifiers (3/7.2.1, 4/7.2.1) -------------------
TAG_PRIMARY_VOLUME = 1
TAG_ANCHOR_POINTER = 2
TAG_IMPLEMENTATION_USE = 4
TAG_PARTITION = 5
TAG_LOGICAL_VOLUME = 6
TAG_UNALLOCATED_SPACE = 7
TAG_TERMINATING = 8
TAG_LOGICAL_VOLUME_INTEGRITY = 9
TAG_FILE_SET = 256
TAG_FILE_IDENTIFIER = 257
TAG_FILE_ENTRY = 261
DESCRIPTOR_VERSION = 2             # NSR02 (UDF 1.02)

# --- other field values ----------------------------------------------------------
UDF_REVISION = 0x0102
DOMAIN_ID = b"*OSTA UDF Compliant"
DOMAIN_FLAGS = 0x03                # hard + soft write protect (read-only disc)
LV_INFO_ID = b"*UDF LV Info"
IMPLEMENTATION_ID = b"*udfkit"
PARTITION_CONTENTS_ID = b"+NSR02"
OSTA_CHARSET = b"OSTA Compressed Unicode"
PARTITION_FLAG_ALLOCATED = 1       # ECMA-167 3/10.5.3
ACCESS_READ_ONLY = 1               # ECMA-167 3/10.5.7
INTEGRITY_CLOSE = 1                # ECMA-167 3/10.10.3
FILE_TYPE_DIRECTORY = 4            # ECMA-167 4/14.6.6
FILE_TYPE_FILE = 5
ICB_STRATEGY = 4                   # ECMA-167 4/A.2 (UDF 1.02: strategy 4 only)
# ICB tag flags, ECMA-167 4/14.6.8: short_ad (0), non-relocatable, archive,
# contiguous - the same set genisoimage writes
ICB_FLAGS = 0x0010 | 0x0020 | 0x0200
FID_DIRECTORY = 0x02               # ECMA-167 4/14.4.3
FID_PARENT = 0x08
# permissions, ECMA-167 4/14.9.5: read for owner, group, other (+ execute
# = search for directories); no write on read-only media
PERM_FILE = 0x1000 | 0x0080 | 0x0004
PERM_DIRECTORY = PERM_FILE | 0x0400 | 0x0020 | 0x0001
UID_NONE = 0xFFFFFFFF              # UDF 3.3.3.3: "not specified"
MAX_EXTENT = 0x3FFFF800            # < 2^30, a whole number of blocks
FIRST_UNIQUE_ID = 16               # UDF 3.2.1.1: 1..15 are reserved

GIB = 1 << 30
DVD_FILE_LIMIT = GIB
ISO_FILE_LIMIT = 0xFFFFFFFF
ISO_MAX_DEPTH = 8                  # ISO 9660 6.8.2.1
LABEL_RE = re.compile(r"[A-Z0-9_]{1,32}")
_DCHARS_RE = re.compile(r"[^A-Z0-9_]")
_83_RE = re.compile(r"([A-Z0-9_]{1,8})(?:\.([A-Z0-9_]{0,3}))?")
_VMG_RE = re.compile(r"VIDEO_TS\.(IFO|VOB|BUP)")
_VTS_RE = re.compile(r"VTS_(\d\d)_(\d)\.(IFO|VOB|BUP)")
_CHUNK = 1 << 20


# ==============================================================================
# primitives
# ==============================================================================

def _crc_table():
    table = []
    for byte in range(256):
        r = byte << 8
        for _ in range(8):
            r = ((r << 1) ^ 0x1021) if r & 0x8000 else (r << 1)
        table.append(r & 0xFFFF)
    return table


_CRC_TABLE = _crc_table()


def crc_itu_t(data):
    """CRC-ITU-T (polynomial x^16 + x^12 + x^5 + 1, initial 0, no final xor),
    the descriptor CRC of ECMA-167 3/7.2.6."""
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


def tag_checksum(tag):
    """ECMA-167 3/7.2.3: sum of tag bytes 0-3 and 5-15, modulo 256."""
    return (sum(tag[0:4]) + sum(tag[5:16])) & 0xFF


def _tagged(buf, tag_id, location):
    """Fill in the 16-byte descriptor tag (ECMA-167 3/7.2) of `buf`, whose
    length is the descriptor length; the CRC covers everything after the tag."""
    struct.pack_into("<HHBBHHHI", buf, 0, tag_id, DESCRIPTOR_VERSION, 0, 0, 0,
                     crc_itu_t(buf[16:]), len(buf) - 16, location)
    buf[4] = tag_checksum(buf)
    return bytes(buf)


def cs0(text):
    """OSTA CS0 d-characters (UDF 2.1.1): compression ID 8 + Latin-1 bytes when
    every character fits, else 16 + UTF-16BE."""
    if all(ord(c) < 256 for c in text):
        return b"\x08" + text.encode("latin-1")
    return b"\x10" + text.encode("utf-16-be", "surrogatepass")


def dstring(text, size):
    """Fixed-size dstring (ECMA-167 1/7.2.12): CS0 characters, zero padding,
    used length in the last byte. Cut on a character boundary if too long."""
    out = bytearray(size)
    if not text:
        return bytes(out)
    data = cs0(text)
    if len(data) > size - 1:
        step = 1 if data[0] == 8 else 2
        data = data[:1 + (size - 2) // step * step]
    out[:len(data)] = data
    out[-1] = len(data)
    return bytes(out)


def charspec():
    """ECMA-167 1/7.2.1 charspec: type 0 (CS0) "OSTA Compressed Unicode"."""
    return bytes(1) + OSTA_CHARSET.ljust(63, b"\0")


def entity_id(ident, suffix=b"", flags=0):
    """ECMA-167 1/7.4 regid: flags, 23-byte identifier, 8-byte suffix."""
    return bytes([flags]) + ident.ljust(23, b"\0") + suffix.ljust(8, b"\0")


def _domain_id():
    return entity_id(DOMAIN_ID, struct.pack("<HB", UDF_REVISION, DOMAIN_FLAGS))


def _impl_id():
    return entity_id(IMPLEMENTATION_ID)


def _local_time(date):
    """(naive local time, offset in minutes) for timestamps that carry a zone.
    Naive datetimes are taken as UTC; offsets that are not whole quarter hours
    (ISO 9660 cannot store them) are converted to UTC."""
    if date.tzinfo is None or date.utcoffset() is None:
        return date, 0
    seconds = date.utcoffset().total_seconds()
    if seconds % 900:
        return date.astimezone(datetime.timezone.utc).replace(tzinfo=None), 0
    return date.replace(tzinfo=None), int(seconds // 60)


def udf_timestamp(date):
    """ECMA-167 1/7.3 timestamp, type 1 (local time) with the zone offset in
    minutes as a 12-bit two's complement number."""
    t, offset = _local_time(date)
    us = t.microsecond
    return struct.pack("<HHBBBBBBBB", 0x1000 | (offset & 0x0FFF), t.year, t.month, t.day,
                       t.hour, t.minute, t.second, us // 10000, us // 100 % 100, us % 100)


def iso_dir_date(date):
    """ISO 9660 9.1.5 recording date: 7 bytes, offset in 15-minute units."""
    t, offset = _local_time(date)
    return struct.pack("<BBBBBBb", t.year - 1900, t.month, t.day, t.hour, t.minute,
                       t.second, offset // 15)


def iso_volume_date(date):
    """ISO 9660 8.4.26.1 date: 16 digits (hundredths last) + offset byte."""
    t, offset = _local_time(date)
    digits = "%04d%02d%02d%02d%02d%02d%02d" % (t.year, t.month, t.day, t.hour, t.minute,
                                              t.second, t.microsecond // 10000)
    return digits.encode("ascii") + struct.pack("<b", offset // 15)


_ISO_NO_DATE = b"0" * 16 + b"\0"


def _both16(v):
    return struct.pack("<H", v) + struct.pack(">H", v)


def _both32(v):
    return struct.pack("<I", v) + struct.pack(">I", v)


def _sectors(nbytes):
    return (nbytes + SECTOR - 1) // SECTOR


# ==============================================================================
# UDF volume structures
# ==============================================================================

def volume_recognition(ident):
    """ECMA-167 2/9.1 volume structure descriptor (BEA01, NSR02, TEA01)."""
    return b"\0" + ident + b"\x01" + bytes(SECTOR - 7)


def anchor_pointer(location, main_vds, reserve_vds):
    """ECMA-167 3/10.2 Anchor Volume Descriptor Pointer."""
    buf = bytearray(512)
    struct.pack_into("<IIII", buf, 16, VDS_SECTORS * SECTOR, main_vds,
                     VDS_SECTORS * SECTOR, reserve_vds)
    return _tagged(buf, TAG_ANCHOR_POINTER, location)


def primary_volume(location, label, volume_set, date):
    """ECMA-167 3/10.1 Primary Volume Descriptor (UDF 2.2.2). libdvdread reads
    the disc title from its Volume Identifier."""
    buf = bytearray(512)
    struct.pack_into("<II", buf, 16, 0, 0)           # VDS number, PVD number
    buf[24:56] = dstring(label, 32)
    struct.pack_into("<HHHHII", buf, 56, 1, 1, 2, 2, 1, 1)   # vol seq, max, interchange
    buf[72:200] = dstring(volume_set, 128)               # levels 2/2, charset lists 1/1
    buf[200:264] = charspec()
    buf[264:328] = charspec()
    buf[376:388] = udf_timestamp(date)
    buf[388:420] = _impl_id()
    return _tagged(buf, TAG_PRIMARY_VOLUME, location)


def implementation_use_volume(location, label):
    """ECMA-167 3/10.4 + UDF 2.2.7: "*UDF LV Info" Implementation Use VD."""
    buf = bytearray(512)
    struct.pack_into("<I", buf, 16, 1)
    buf[20:52] = entity_id(LV_INFO_ID, struct.pack("<H", UDF_REVISION))
    buf[52:116] = charspec()                         # LVICharset
    buf[116:244] = dstring(label, 128)               # LogicalVolumeIdentifier
    # LVInfo1..3 (3 x 36 bytes) stay empty
    buf[352:384] = _impl_id()
    return _tagged(buf, TAG_IMPLEMENTATION_USE, location)


def partition(location, start, length):
    """ECMA-167 3/10.5 Partition Descriptor (UDF 2.2.14): partition 0,
    read-only, no space bitmaps."""
    buf = bytearray(512)
    struct.pack_into("<IHH", buf, 16, 2, PARTITION_FLAG_ALLOCATED, 0)
    buf[24:56] = entity_id(PARTITION_CONTENTS_ID)
    struct.pack_into("<III", buf, 184, ACCESS_READ_ONLY, start, length)
    buf[196:228] = _impl_id()
    return _tagged(buf, TAG_PARTITION, location)


def logical_volume(location, label, integrity_sector):
    """ECMA-167 3/10.6 Logical Volume Descriptor (UDF 2.2.4) with one type 1
    partition map; the File Set Descriptor is at partition block 0."""
    buf = bytearray(446)
    struct.pack_into("<I", buf, 16, 3)
    buf[20:84] = charspec()
    buf[84:212] = dstring(label, 128)
    struct.pack_into("<I", buf, 212, SECTOR)         # logical block size
    buf[216:248] = _domain_id()
    # LogicalVolumeContentsUse: long_ad of the File Set Descriptor sequence
    struct.pack_into("<IIH", buf, 248, 2 * SECTOR, 0, 0)
    struct.pack_into("<II", buf, 264, 6, 1)          # map table length, maps
    buf[272:304] = _impl_id()
    struct.pack_into("<II", buf, 432, LVIS_SECTORS * SECTOR, integrity_sector)
    # type 1 partition map (3/10.7.2): type, length, volume seq 1, partition 0
    struct.pack_into("<BBHH", buf, 440, 1, 6, 1, 0)
    return _tagged(buf, TAG_LOGICAL_VOLUME, location)


def unallocated_space(location):
    """ECMA-167 3/10.8 Unallocated Space Descriptor: no free extents."""
    buf = bytearray(24)
    struct.pack_into("<II", buf, 16, 4, 0)
    return _tagged(buf, TAG_UNALLOCATED_SPACE, location)


def terminating(location):
    """ECMA-167 3/10.9 Terminating Descriptor."""
    return _tagged(bytearray(512), TAG_TERMINATING, location)


def logical_volume_integrity(location, date, next_unique_id, partition_length,
                             files, directories):
    """ECMA-167 3/10.10 Logical Volume Integrity Descriptor (UDF 2.2.6),
    closed, with the UDF implementation use area (46 bytes)."""
    buf = bytearray(88 + 46)
    buf[16:28] = udf_timestamp(date)
    struct.pack_into("<I", buf, 28, INTEGRITY_CLOSE)
    # next integrity extent (32..39) = none; contents use: next unique id
    struct.pack_into("<Q", buf, 40, next_unique_id)
    struct.pack_into("<IIII", buf, 72, 1, 46, 0, partition_length)  # free 0, size
    buf[88:120] = _impl_id()
    struct.pack_into("<IIHHH", buf, 120, files, directories,
                     UDF_REVISION, UDF_REVISION, UDF_REVISION)
    return _tagged(buf, TAG_LOGICAL_VOLUME_INTEGRITY, location)


# ==============================================================================
# UDF file structures (locations are partition block numbers)
# ==============================================================================

def file_set(block, label, root_block, date):
    """ECMA-167 4/14.1 File Set Descriptor (UDF 2.3.2)."""
    buf = bytearray(512)
    buf[16:28] = udf_timestamp(date)
    struct.pack_into("<HHIIII", buf, 28, 3, 3, 1, 1, 0, 0)
    buf[48:112] = charspec()
    buf[112:240] = dstring(label, 128)               # logical volume identifier
    buf[240:304] = charspec()
    buf[304:336] = dstring(label, 32)                # file set identifier
    struct.pack_into("<IIH", buf, 400, SECTOR, root_block, 0)   # root ICB
    buf[416:448] = _domain_id()
    return _tagged(buf, TAG_FILE_SET, block)


def file_identifier(block, name, characteristics, icb_block, unique_id):
    """ECMA-167 4/14.4 File Identifier Descriptor, padded to 4 bytes. `name`
    None is the parent entry. The ICB long_ad carries the UDF unique id
    (UDF 2.3.4.3). `block` is the block where the descriptor starts."""
    ident = cs0(name) if name is not None else b""
    if len(ident) > 255:
        raise UdfError(f"file name too long for UDF: {name!r}",
                            hint="UDF allows 254 Latin-1 or 127 other characters")
    length = (38 + len(ident) + 3) & ~3
    buf = bytearray(length)
    struct.pack_into("<HBB", buf, 16, 1, characteristics, len(ident))
    struct.pack_into("<IIHHIH", buf, 20, SECTOR, icb_block, 0, 0,
                     unique_id & 0xFFFFFFFF, 0)
    buf[38:38 + len(ident)] = ident
    return _tagged(buf, TAG_FILE_IDENTIFIER, block)


def file_entry(block, is_dir, size, data_block, unique_id, link_count, date):
    """ECMA-167 4/14.9 File Entry with short_ad allocation descriptors (one
    per extent of at most MAX_EXTENT bytes; none for an empty file)."""
    ads = bytearray()
    remaining, pos = size, data_block
    while remaining > 0:
        chunk = min(remaining, MAX_EXTENT)
        ads += struct.pack("<II", chunk, pos)
        pos += chunk // SECTOR
        remaining -= chunk
    if 176 + len(ads) > SECTOR:
        raise UdfError("file too large for one UDF File Entry")
    buf = bytearray(176 + len(ads))
    # ICB tag (4/14.6)
    struct.pack_into("<IHHHBB", buf, 16, 0, ICB_STRATEGY, 0, 1, 0,
                     FILE_TYPE_DIRECTORY if is_dir else FILE_TYPE_FILE)
    struct.pack_into("<H", buf, 34, ICB_FLAGS)
    struct.pack_into("<IIIHBBI", buf, 36, UID_NONE, UID_NONE,
                     PERM_DIRECTORY if is_dir else PERM_FILE, link_count, 0, 0, 0)
    struct.pack_into("<QQ", buf, 56, size, _sectors(size))
    stamp = udf_timestamp(date)
    buf[72:84] = buf[84:96] = buf[96:108] = stamp   # access, modification, attribute
    struct.pack_into("<I", buf, 108, 1)             # checkpoint
    buf[128:160] = _impl_id()
    struct.pack_into("<QII", buf, 160, unique_id, 0, len(ads))
    buf[176:] = ads
    return _tagged(buf, TAG_FILE_ENTRY, block)


# ==============================================================================
# ISO 9660 structures
# ==============================================================================

def iso_directory_record(ident, sector, size, is_dir, date):
    """ISO 9660 9.1 directory record; `ident` is bytes (b"\\0" = ".",
    b"\\1" = "..")."""
    length = 33 + len(ident) + (0 if len(ident) % 2 else 1)
    return (struct.pack("<BB", length, 0) + _both32(sector) + _both32(size)
            + iso_dir_date(date) + bytes([2 if is_dir else 0, 0, 0]) + _both16(1)
            + bytes([len(ident)]) + ident + bytes(length - 33 - len(ident)))


def iso_primary_volume(label, volume_sectors, path_table_size, l_table, m_table,
                       root_record, date):
    """ISO 9660 8.4 Primary Volume Descriptor."""
    buf = bytearray(SECTOR)
    buf[0:7] = b"\x01CD001\x01"
    buf[8:40] = b" " * 32                            # system identifier
    buf[40:72] = label.encode("ascii").ljust(32)
    buf[80:88] = _both32(volume_sectors)
    buf[120:124] = _both16(1)                        # volume set size
    buf[124:128] = _both16(1)                        # volume sequence number
    buf[128:132] = _both16(SECTOR)
    buf[132:140] = _both32(path_table_size)
    struct.pack_into("<II", buf, 140, l_table, 0)
    struct.pack_into(">II", buf, 148, m_table, 0)
    buf[156:190] = root_record
    buf[190:702] = b" " * 512                        # volume set .. preparer
    buf[574:702] = b"UDFKIT".ljust(128)            # application identifier
    buf[702:813] = b" " * 111                        # copyright, abstract, biblio
    buf[813:830] = buf[830:847] = iso_volume_date(date)
    buf[847:864] = buf[864:881] = _ISO_NO_DATE       # expiration, effective
    buf[881] = 1                                     # file structure version
    return bytes(buf)


def iso_terminator():
    """ISO 9660 8.3 Volume Descriptor Set Terminator."""
    return b"\xffCD001\x01" + bytes(SECTOR - 7)


def path_table_record(ident, sector, parent, big_endian):
    """ISO 9660 9.4 path table record."""
    fmt = ">IH" if big_endian else "<IH"
    rec = bytes([len(ident), 0]) + struct.pack(fmt, sector, parent) + ident
    return rec + (b"\0" if len(ident) % 2 else b"")


# ==============================================================================
# the file tree
# ==============================================================================

class _Node:
    __slots__ = ("name", "src", "is_dir", "size", "children", "parent", "iso_name",
                 "fe_block", "dir_bytes", "unique_id", "sector", "iso_sector",
                 "iso_size", "number", "depth")

    def __init__(self, name, src, is_dir, size=0, parent=None):
        self.name, self.src, self.is_dir, self.size = name, src, is_dir, size
        self.parent = parent
        self.children = []
        self.iso_name = None
        self.fe_block = self.dir_bytes = self.unique_id = None
        self.sector = self.iso_sector = self.iso_size = self.number = None
        self.depth = 0 if parent is None else parent.depth + 1

    def path(self):
        parts, n = [], self
        while n.parent is not None:
            parts.append(n.name)
            n = n.parent
        return "/" + "/".join(reversed(parts))

    def walk(self):
        """Pre-order: this node, then its children's subtrees in order."""
        yield self
        for c in self.children:
            yield from c.walk()


def validate_label(label):
    if not isinstance(label, str) or not LABEL_RE.fullmatch(label):
        raise UdfError(
            f"invalid volume label {label!r}",
            hint="DVD labels are 1-32 characters from A-Z, 0-9 and _")
    return label


def dvd_sort_key(name):
    """Order of the VIDEO_TS files on the disc: the VMG, then each title set
    (IFO, menu VOB, title VOBs 1-9, BUP); anything else last, by name."""
    m = _VMG_RE.fullmatch(name)
    if m:
        return (0, 0, "IVB".index(m.group(1)[0]), "")
    m = _VTS_RE.fullmatch(name)
    if m:
        vts, part, ext = int(m.group(1)), int(m.group(2)), m.group(3)
        if vts >= 1 and (ext == "VOB" or part == 0):
            return (1, vts, {"IFO": 0, "BUP": 11}.get(ext, 1 + part), "")
    return (2, 0, 0, name)


def _scan_dir(node, path, skip, in_video_ts=False):
    """Add the entries of `path` to `node`, recursively."""
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError as e:
        raise UdfError(f"cannot read {path}: {e.strerror}") from e
    for e in entries:
        if e.name.startswith(".") or os.path.realpath(e.path) == skip:
            continue
        if e.is_dir():
            if in_video_ts:
                raise UdfError(f"VIDEO_TS must contain only files, found folder {e.name!r}")
            child = _Node(e.name, e.path, True, parent=node)
            if child.depth >= ISO_MAX_DEPTH:
                raise UdfError(
                    f"{child.path()} is nested too deeply for ISO 9660",
                    hint=f"ISO 9660 allows {ISO_MAX_DEPTH} directory levels including the root")
            _scan_dir(child, e.path, skip,
                      in_video_ts=node.parent is None and e.name == "VIDEO_TS")
        elif e.is_file():
            size = e.stat().st_size
            name = e.name.upper() if in_video_ts else e.name
            child = _Node(name, e.path, False, size, parent=node)
            if in_video_ts:
                if not _83_RE.fullmatch(name):
                    raise UdfError(f"VIDEO_TS/{e.name} is not an 8.3 file name",
                                        hint="DVD-Video files are named like VTS_01_1.VOB")
                if size > DVD_FILE_LIMIT:
                    raise UdfError(
                        f"VIDEO_TS/{e.name} is {size} bytes; DVD-Video files must not "
                        f"exceed 1 GiB ({DVD_FILE_LIMIT} bytes)")
            elif size > ISO_FILE_LIMIT:
                raise UdfError(f"{child.path()} is too large for ISO 9660 (4 GiB limit)")
        else:
            continue
        node.children.append(child)
    if in_video_ts:
        names = [c.name for c in node.children]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise UdfError(f"VIDEO_TS has names differing only in case: {sorted(dup)}")
        node.children.sort(key=lambda c: dvd_sort_key(c.name))


def scan_tree(src_dir, skip=None):
    """Read `src_dir` into a _Node tree: VIDEO_TS, AUDIO_TS (empty if absent),
    then the other entries by name."""
    video = os.path.join(src_dir, "VIDEO_TS")
    if not os.path.isdir(video):
        raise UdfError(f"{src_dir} has no VIDEO_TS directory")
    root = _Node("", src_dir, True)
    _scan_dir(root, src_dir, skip)
    by_name = {c.name: c for c in root.children}
    vts = by_name["VIDEO_TS"]
    ats = by_name.get("AUDIO_TS")
    if ats is None or not ats.is_dir:
        if ats is not None:
            raise UdfError(f"{src_dir}/AUDIO_TS is not a directory")
        ats = _Node("AUDIO_TS", None, True, parent=root)
    rest = [c for c in root.children if c.name not in ("VIDEO_TS", "AUDIO_TS")]
    root.children = [vts, ats] + rest
    return root


# --- ISO 9660 names --------------------------------------------------------------

def _iso_parts(name, is_dir):
    """(base, ext) of the level-1 mapping of `name` (before collision fixes)."""
    def clean(part):
        part = unicodedata.normalize("NFKD", part).encode("ascii", "ignore").decode()
        return _DCHARS_RE.sub("_", part.upper())

    if is_dir:
        base, ext = name, None
    elif "." in name[1:]:
        base, ext = name.rsplit(".", 1)
    else:
        base, ext = name, ""
    base = clean(base)[:8] or "_"
    if ext is not None:
        ext = clean(ext)[:3]
    return base, ext


def _iso_valid(name, is_dir):
    if is_dir:
        return re.fullmatch(r"[A-Z0-9_]{1,8}", name) is not None
    m = _83_RE.fullmatch(name)
    return m is not None and "." in name


def assign_iso_names(directory):
    """Give each child of `directory` a unique ISO 9660 level-1 name. Names
    that are already valid keep it; the others are mapped (see module doc)."""
    taken = set()
    valid = [c for c in directory.children if _iso_valid(c.name, c.is_dir)]
    others = [c for c in directory.children if not _iso_valid(c.name, c.is_dir)]
    for c in valid:
        base, _, ext = c.name.partition(".")
        c.iso_name = (base, ext if not c.is_dir else None)
        taken.add(c.iso_name)
    for c in others:
        base, ext = _iso_parts(c.name, c.is_dir)
        n = 0
        while (base, ext) in taken:
            n += 1
            sfx = f"_{n}"
            base = _iso_parts(c.name, c.is_dir)[0][:8 - len(sfx)] + sfx
        c.iso_name = (base, ext)
        taken.add(c.iso_name)


def _iso_ident(node):
    base, ext = node.iso_name
    if node.is_dir:
        return base.encode("ascii")
    return f"{base}.{ext};1".encode("ascii")


# ==============================================================================
# VIDEO_TS placement from the IFO files
# ==============================================================================

def _read_ifo(path, magic):
    """The IFO header fields that place files (big-endian sector numbers), or
    None if the file is not an IFO of that kind.

    VMG (VIDEO_TS.IFO): vmg_last_sector 0x0C, vmgm_vobs 0xC0, and from the
    title search pointer table (TT_SRPT, its sector at 0xC4) the start sector
    of each title set. VTS (VTS_NN_0.IFO): vts_last_sector 0x0C, vtsm_vobs
    0xC0, vtstt_vobs 0xC4. All relative to the start of the IFO.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(SECTOR)
            if head[:12] != magic or len(head) < 0xC8:
                return None
            last, menu, c4 = (struct.unpack_from(">I", head, o)[0] for o in (0x0C, 0xC0, 0xC4))
            if magic == b"DVDVIDEO-VTS":
                return {"last_sector": last, "menu_vobs": menu, "title_vobs": c4}
            # TT_SRPT: u16 title count, 6 bytes, then 12-byte entries with the
            # title set number at +6 and its start sector at +8
            f.seek(c4 * SECTOR)
            tt = f.read(SECTOR)
            starts = {}
            count = struct.unpack_from(">H", tt, 0)[0] if len(tt) >= 8 else 0
            for off in range(8, min(8 + 12 * count, len(tt) - 11), 12):
                vts, start = tt[off + 6], struct.unpack_from(">I", tt, off + 8)[0]
                if starts.setdefault(vts, start) != start:
                    raise UdfError(
                        f"VIDEO_TS.IFO gives two start sectors for title set {vts}")
            return {"last_sector": last, "menu_vobs": menu, "vts_start": starts}
    except OSError as e:
        raise UdfError(f"cannot read {path}: {e.strerror}") from e


def video_ts_layout(video_ts):
    """Sector offsets (relative to VIDEO_TS.IFO) for the files of VIDEO_TS, in
    disc order. Returns ([(node, offset)], total sectors, ifo_layout_used)."""
    files = {c.name: c for c in video_ts.children}
    placed = []
    pos = 0
    used_ifo = False

    def place(name, at=None, why=""):
        nonlocal pos
        node = files.pop(name, None)
        if node is None:
            return
        if at is None:
            at = pos
        if at < pos:
            raise UdfError(
                f"VIDEO_TS/{name} must start at sector {at} of the DVD ({why}), "
                f"but the files before it need {pos} sectors",
                hint="the IFO files do not match the VOB sizes; author the disc again")
        placed.append((node, at))
        pos = at + _sectors(node.size)

    def bup_at(last_sector, name):
        """The BUP ends at the last sector of its VMG / VTS."""
        node = files.get(name)
        return last_sector + 1 - _sectors(node.size) if node else None

    vmg = _read_ifo(files["VIDEO_TS.IFO"].src, b"DVDVIDEO-VMG") if "VIDEO_TS.IFO" in files \
        else None
    place("VIDEO_TS.IFO", 0)
    if vmg:
        used_ifo = True
        place("VIDEO_TS.VOB", vmg["menu_vobs"] or None, "vmgm_vobs in VIDEO_TS.IFO")
        place("VIDEO_TS.BUP", bup_at(vmg["last_sector"], "VIDEO_TS.BUP"),
              "vmg_last_sector in VIDEO_TS.IFO")
    else:
        place("VIDEO_TS.VOB")
        place("VIDEO_TS.BUP")
    title_sets = sorted({dvd_sort_key(n)[1] for n in files if dvd_sort_key(n)[0] == 1})
    for vts in title_sets:
        ifo_name = f"VTS_{vts:02d}_0.IFO"
        start = vmg["vts_start"].get(vts) if vmg else None
        if ifo_name not in files:
            raise UdfError(f"VIDEO_TS has files of title set {vts} but no {ifo_name}")
        place(ifo_name, start, f"title set {vts} start sector in VIDEO_TS.IFO")
        base = placed[-1][1]
        info = _read_ifo(placed[-1][0].src, b"DVDVIDEO-VTS")
        if info:
            used_ifo = True
            menu = info["menu_vobs"]
            place(f"VTS_{vts:02d}_0.VOB", base + menu if menu else None,
                  f"vtsm_vobs in {ifo_name}")
            title = info["title_vobs"]
            for part in range(1, 10):
                at = base + title if part == 1 and title else None
                place(f"VTS_{vts:02d}_{part}.VOB", at, f"vtstt_vobs in {ifo_name}")
            bup = f"VTS_{vts:02d}_0.BUP"
            last = bup_at(info["last_sector"], bup)
            place(bup, base + last if last is not None else None,
                  f"vts_last_sector in {ifo_name}")
        else:
            for part in range(0, 10):
                place(f"VTS_{vts:02d}_{part}.VOB")
            place(f"VTS_{vts:02d}_0.BUP")
    for node in video_ts.children:           # anything else, in sort order
        if node.name in files:
            place(node.name)
    return placed, pos, used_ifo


# ==============================================================================
# the writer
# ==============================================================================

def write_image(src_dir, out_path, label, *, date=None, on_progress=None, shift=0):
    """Write a UDF 1.02 + ISO 9660 DVD-Video image of `src_dir` to `out_path`.

    `src_dir` holds VIDEO_TS (required), AUDIO_TS (created empty in the image
    if missing) and optionally DVD-ROM files and folders. `date` (a datetime)
    stamps every structure; it defaults to now (UTC). `on_progress(fraction)`
    is called as data is written. `shift` empty sectors go before VIDEO_TS
    (to put a dual-layer disc's layer break on an ECC block). Returns
    statistics: sectors, bytes, files, directories, the UDF volume name,
    whether the IFOs placed the VIDEO_TS files, and {"/path": (first sector,
    bytes)} for every file.
    """
    validate_label(label)
    if date is None:
        date = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    if not 1970 <= date.year <= 2155:
        raise UdfError(f"date {date} is out of range for ISO 9660 (1970-2155)")
    out_path = os.path.abspath(out_path)
    root = scan_tree(src_dir, skip=os.path.realpath(out_path))
    layout = _plan(root, date, shift)
    try:
        _write(root, layout, out_path, label, date, on_progress)
    except BaseException:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise
    files = [n for n in root.walk() if not n.is_dir]
    return {
        "path": out_path,
        "sectors": layout["total"],
        "bytes": layout["total"] * SECTOR,
        "files": len(files),
        "directories": sum(1 for n in root.walk() if n.is_dir),
        "label": label,
        "udf_volume": dstring_text(dstring(label, 32)),
        "ifo_layout": layout["ifo_layout"],
        "partition_start": PARTITION_START,
        "data_start": layout["data_start"],
        "extents": {n.path(): (n.sector, n.size) for n in files},
        "iso_names": {n.path(): _iso_path(n) for n in root.walk() if n.parent is not None},
    }


def plan_image(src_dir):
    """Where an image of `src_dir` would put VIDEO_TS: {"video_ts": sector of
    VIDEO_TS.IFO, "total": sectors} (nothing is written)."""
    root = scan_tree(src_dir)
    layout = _plan(root, datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc))
    return {"video_ts": layout["data_start"], "total": layout["total"]}


def dstring_text(raw):
    """Decode a dstring back to text (for reporting)."""
    length = raw[-1]
    if not length:
        return ""
    body = raw[1:length]
    return body.decode("latin-1") if raw[0] == 8 else body.decode("utf-16-be", "replace")


def _iso_path(node):
    parts = []
    while node.parent is not None:
        parts.append(_iso_ident(node).decode("ascii"))
        node = node.parent
    return "/" + "/".join(reversed(parts))


def _plan(root, date, shift=0):
    """Assign every structure its place. Returns a dict with the sector map."""
    dirs = [n for n in root.walk() if n.is_dir]
    files = [n for n in root.walk() if not n.is_dir]

    # UDF directories: File Entry followed by the File Identifier Descriptors
    block = 2                                    # after the FSD and its terminator
    uid = FIRST_UNIQUE_ID
    for d in dirs:
        if d is root:
            d.unique_id = 0                      # UDF 3.2.1.1: the root has 0
        else:
            d.unique_id, uid = uid, uid + 1
        d.dir_bytes = len(file_identifier(0, None, 0, 0, 0)) + sum(
            len(file_identifier(0, c.name, 0, 0, 0)) for c in d.children)
        d.fe_block = block
        block += 1 + _sectors(d.dir_bytes)
    for f in files:
        f.unique_id = uid
        uid += 1
        f.fe_block = block
        block += 1
    sector = PARTITION_START + block

    # ISO 9660: names, path table (level, parent number, name), directories
    for d in dirs:
        assign_iso_names(d)
    # breadth first, each parent's subdirectories by name: ISO 9660 6.9.1
    # orders the path table by level, parent number, then name
    ordered = [root]
    i = 0
    while i < len(ordered):
        d = ordered[i]
        d.number = i + 1
        ordered.extend(sorted((c for c in d.children if c.is_dir), key=lambda c: c.iso_name[0]))
        i += 1
    path_table_size = sum(len(path_table_record(_pt_ident(d), 0, 0, False)) for d in ordered)
    path_blocks = _sectors(path_table_size)
    path_blocks += path_blocks & 1               # even, as genisoimage
    l_table, m_table = sector, sector + path_blocks
    sector += 2 * path_blocks
    for d in ordered:
        d.iso_size = _iso_dir_size(d, date)
        d.iso_sector = sector
        sector += d.iso_size // SECTOR

    # file data: VIDEO_TS by the IFOs, then everything else in tree order
    data_start = sector
    video_ts = root.children[0]
    placed, vts_len, ifo_layout = video_ts_layout(video_ts)
    data = [(n, data_start + shift + off) for n, off in placed]
    sector = data_start + shift + vts_len
    for f in files:
        if f.parent is video_ts:
            continue
        data.append((f, sector))
        sector += _sectors(f.size)
    for n, s in data:
        n.sector = s
    end = sector
    return {"dirs": dirs, "files": files, "ordered": ordered, "data": data,
            "path_table_size": path_table_size, "l_table": l_table, "m_table": m_table,
            "data_start": data_start, "end": end, "total": end + TAIL_SECTORS,
            "next_unique_id": uid, "ifo_layout": ifo_layout}


def _pt_ident(d):
    return b"\0" if d.parent is None else _iso_ident(d)


def _iso_children(d):
    """Directory records in ISO 9660 9.3 order: by name, then extension."""
    return sorted(d.children, key=lambda c: (c.iso_name[0], c.iso_name[1] or ""))


def _iso_records(d, date):
    """The records of directory `d` (sizes/sectors filled in when known)."""
    parent = d.parent or d
    recs = [iso_directory_record(b"\0", d.iso_sector or 0, d.iso_size or 0, True, date),
            iso_directory_record(b"\1", parent.iso_sector or 0, parent.iso_size or 0, True,
                                 date)]
    for c in _iso_children(d):
        if c.is_dir:
            recs.append(iso_directory_record(_iso_ident(c), c.iso_sector or 0,
                                             c.iso_size or 0, True, date))
        else:
            recs.append(iso_directory_record(_iso_ident(c), c.sector or 0, c.size, False,
                                             date))
    return recs


def _pack_records(recs):
    """Lay records out in sectors; a record never crosses a sector boundary."""
    out = bytearray()
    for r in recs:
        room = SECTOR - len(out) % SECTOR
        if len(r) > room:
            out += bytes(room)
        out += r
    out += bytes(-len(out) % SECTOR)
    return bytes(out)


def _iso_dir_size(d, date):
    return len(_pack_records(_iso_records(d, date)))


def _write(root, layout, out_path, label, date, on_progress):
    total = layout["total"]
    data_start = layout["data_start"]
    meta = bytearray(data_start * SECTOR)

    def put(sector, data):
        meta[sector * SECTOR:sector * SECTOR + len(data)] = data

    p_start = PARTITION_START
    p_length = layout["end"] - p_start

    # ISO 9660 volume descriptors
    root_record = iso_directory_record(b"\0", root.iso_sector, root.iso_size, True, date)
    put(ISO_PVD_SECTOR, iso_primary_volume(label, total, layout["path_table_size"],
                                           layout["l_table"], layout["m_table"],
                                           root_record, date))
    put(ISO_TERMINATOR_SECTOR, iso_terminator())
    for i, ident in enumerate((b"BEA01", b"NSR02", b"TEA01")):
        put(VRS_SECTOR + i, volume_recognition(ident))

    # UDF volume descriptor sequences (main and reserve), integrity, anchor
    # UDF 2.2.2.5: the volume set identifier starts with 16 "unique" hex
    # digits; derive them from the date and label so output is reproducible
    utc = date if date.tzinfo else date.replace(tzinfo=datetime.timezone.utc)
    volume_set = "%08X%08X" % (int(utc.timestamp()) & 0xFFFFFFFF,
                               zlib.crc32(label.encode("ascii")))
    for base in (MAIN_VDS_SECTOR, RESERVE_VDS_SECTOR):
        put(base + 0, primary_volume(base + 0, label, volume_set, date))
        put(base + 1, implementation_use_volume(base + 1, label))
        put(base + 2, partition(base + 2, p_start, p_length))
        put(base + 3, logical_volume(base + 3, label, LVIS_SECTOR))
        put(base + 4, unallocated_space(base + 4))
        put(base + 5, terminating(base + 5))
    put(LVIS_SECTOR, logical_volume_integrity(
        LVIS_SECTOR, date, layout["next_unique_id"], p_length,
        len(layout["files"]), len(layout["dirs"])))
    put(LVIS_SECTOR + 1, terminating(LVIS_SECTOR + 1))
    put(ANCHOR_SECTOR, anchor_pointer(ANCHOR_SECTOR, MAIN_VDS_SECTOR, RESERVE_VDS_SECTOR))

    # UDF partition: file set, directories, file entries
    put(p_start + 0, file_set(0, label, root.fe_block, date))
    put(p_start + 1, terminating(1))
    for d in layout["dirs"]:
        links = 1 + sum(1 for c in d.children if c.is_dir)
        put(p_start + d.fe_block, file_entry(d.fe_block, True, d.dir_bytes, d.fe_block + 1,
                                             d.unique_id, links, date))
        first = d.fe_block + 1
        parent = d.parent or d
        fids = bytearray(file_identifier(first, None, FID_DIRECTORY | FID_PARENT,
                                         parent.fe_block, parent.unique_id))
        for c in d.children:
            fids += file_identifier(first + len(fids) // SECTOR, c.name,
                                    FID_DIRECTORY if c.is_dir else 0, c.fe_block, c.unique_id)
        put(p_start + first, fids)
    for f in layout["files"]:
        data_block = f.sector - p_start if f.size else 0
        put(p_start + f.fe_block, file_entry(f.fe_block, False, f.size, data_block,
                                             f.unique_id, 1, date))

    # ISO 9660 path tables and directories
    for big, sector in ((False, layout["l_table"]), (True, layout["m_table"])):
        table = b"".join(path_table_record(_pt_ident(d), d.iso_sector,
                                           (d.parent or d).number, big)
                         for d in layout["ordered"])
        put(sector, table)
    for d in layout["ordered"]:
        put(d.iso_sector, _pack_records(_iso_records(d, date)))

    # stream everything to the file
    done = 0
    total_bytes = total * SECTOR

    def progress(n):
        nonlocal done
        done += n
        if on_progress:
            on_progress(min(1.0, done / total_bytes))

    if on_progress:
        on_progress(0.0)
    zeros = bytes(_CHUNK)
    with open(out_path, "wb") as out:
        out.write(meta)
        progress(len(meta))
        pos = data_start
        for node, sector in layout["data"]:
            _write_zeros(out, (sector - pos) * SECTOR, zeros)
            progress((sector - pos) * SECTOR)
            _copy_file(node, out, progress)
            pad = -node.size % SECTOR
            out.write(zeros[:pad])
            pos = sector + _sectors(node.size)
        _write_zeros(out, (layout["end"] - pos) * SECTOR, zeros)
        progress((layout["end"] - pos) * SECTOR)
        for s in range(layout["end"], total):
            out.write(anchor_pointer(s, MAIN_VDS_SECTOR, RESERVE_VDS_SECTOR).ljust(SECTOR, b"\0"))
        progress((total - layout["end"]) * SECTOR)
        if out.tell() != total_bytes:
            raise UdfError(f"internal error: wrote {out.tell()} bytes, "
                                f"expected {total_bytes}")


def _write_zeros(out, n, zeros):
    while n > 0:
        k = min(n, len(zeros))
        out.write(zeros[:k])
        n -= k


def _copy_file(node, out, progress):
    """Copy a file's bytes, checking it did not change size since the scan."""
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
    progress(-node.size % SECTOR)

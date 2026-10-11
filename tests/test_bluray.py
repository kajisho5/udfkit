"""The built-in UDF 2.50 writer (Blu-ray images): its structures, and the
files read back with libudfread, the reader libbluray opens images with."""

import datetime
import os
import shutil
import struct
import tempfile
import unittest

from udfkit import bluray as udf250
from udfkit.errors import UdfError
from udfkit.dvd import SECTOR, crc_itu_t, tag_checksum

try:
    from . import udfread_sim
    LIBUDFREAD = udfread_sim.load_library()
except Exception:  # pragma: no cover
    LIBUDFREAD = None

DATE = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)


def tag(data, sector=None):
    """(tag id, version, location) of a descriptor, its checksum and CRC checked."""
    tid, ver, _, _, _, crc, length, loc = struct.unpack("<HHBBHHHI", data[:16])
    assert tag_checksum(data[:16]) == data[4], "tag checksum"
    assert crc_itu_t(data[16:16 + length]) == crc, "descriptor CRC"
    return tid, ver, loc


class TestUdf250(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="udfkit-udf250-")
        src = self.src = os.path.join(self.tmp, "disc")
        self.files = {
            "BDMV/index.bdmv": b"INDX0200" + bytes(100),
            "BDMV/MovieObject.bdmv": b"MOBJ0200" + bytes(30),
            "BDMV/STREAM/00001.m2ts": os.urandom(192 * 5000),
            "BDMV/STREAM/00002.m2ts": os.urandom(SECTOR * 3),      # exactly 3 sectors
            "BDMV/PLAYLIST/00001.mpls": b"MPLS0200",
            "BDMV/AUXDATA/empty.bin": b"",
            "CERTIFICATE/BACKUP/.hidden": b"not on the disc",
            "Extras/日本語.txt": "ファイル名".encode("utf-8"),
        }
        for rel, data in self.files.items():
            path = os.path.join(src, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        os.makedirs(os.path.join(src, "BDMV", "JAR"))
        self.iso = os.path.join(self.tmp, "bd.iso")
        self.info = udf250.write_image(src, self.iso, "MY_BD", date=DATE)
        with open(self.iso, "rb") as f:
            self.data = f.read()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sector(self, n):
        return self.data[n * SECTOR:(n + 1) * SECTOR]

    def test_volume_structures(self):
        self.assertEqual(len(self.data), self.info["sectors"] * SECTOR)
        self.assertEqual([self.sector(16 + i)[1:6] for i in range(3)],
                         [b"BEA01", b"NSR03", b"TEA01"])
        self.assertEqual(self.data[:16 * SECTOR], bytes(16 * SECTOR))
        for n in (256, self.info["sectors"] - 1):       # both anchors
            self.assertEqual(tag(self.sector(n)), (2, 3, n))
        main = [tag(self.sector(32 + i))[0] for i in range(6)]
        self.assertEqual(main, [1, 4, 5, 6, 7, 8])      # PVD, IUVD, PD, LVD, USD, TD
        self.assertEqual([tag(self.sector(48 + i))[0] for i in range(6)], main)
        pd = self.sector(34)
        self.assertEqual(pd[25:31], b"+NSR03")
        start, length = struct.unpack("<II", pd[188:196])
        self.assertEqual(start + length + 1, self.info["sectors"])
        lvd = self.sector(35)
        self.assertEqual(lvd[217:236], b"*OSTA UDF Compliant")
        self.assertEqual(struct.unpack("<H", lvd[240:242])[0], 0x0250)
        maps_len, n_maps = struct.unpack("<II", lvd[264:272])
        self.assertEqual((maps_len, n_maps), (70, 2))
        self.assertEqual(lvd[440:446], struct.pack("<BBHH", 1, 6, 1, 0))
        meta_map = lvd[446:510]
        self.assertEqual(meta_map[:2], b"\x02\x40")
        self.assertEqual(meta_map[5:28], b"*UDF Metadata Partition")
        _, _, meta, mirror, bitmap, unit, align, flags = struct.unpack("<HHIIIIHB",
                                                                       meta_map[36:59])
        self.assertEqual((meta, mirror, bitmap, unit, align, flags),
                         (0, 1, 0xFFFFFFFF, 32, 32, 1))
        # the metadata file and its mirror: same blocks, both 32-block aligned
        efes = [self.sector(start + b) for b in (meta, mirror)]
        self.assertEqual([tag(e)[0] for e in efes], [266, 266])
        self.assertEqual([e[16 + 11] for e in efes], [250, 251])   # file types
        ads = [struct.unpack("<II", e[216:224]) for e in efes]
        self.assertEqual(ads[0][0], ads[1][0])
        self.assertTrue(all(b % 32 == 0 for _, b in ads))
        n = ads[0][0]
        copy = [self.data[(start + b) * SECTOR:(start + b) * SECTOR + n] for _, b in ads]
        self.assertEqual(copy[0], copy[1])
        self.assertEqual(tag(copy[0][:512]), (256, 3, 0))          # file set at block 0
        # integrity: closed, UDF 2.50 read/write revisions, 2 partitions
        lvid = self.sector(64)
        self.assertEqual(tag(lvid)[0], 9)
        self.assertEqual(struct.unpack("<II", lvid[72:80]), (2, 46))
        imp = 80 + 16
        files, dirs, rmin, wmin, wmax = struct.unpack("<IIHHH", lvid[imp + 32:imp + 46])
        self.assertEqual((files, dirs, rmin, wmin, wmax), (7, 9, 0x250, 0x250, 0x250))

    def test_file_data_is_contiguous_and_in_place(self):
        for path, (sector, size) in self.info["extents"].items():
            want = self.files[path[1:]]
            self.assertEqual(size, len(want))
            if size:
                self.assertEqual(self.data[sector * SECTOR:sector * SECTOR + size], want, path)

    @unittest.skipUnless(LIBUDFREAD, "libudfread not installed")
    def test_libudfread_reads_every_file(self):
        with udfread_sim.Image(LIBUDFREAD, self.iso) as image:
            self.assertEqual(image.volume_id(), "MY_BD")
            for rel, data in self.files.items():
                got = image.read("/" + rel)
                if rel.endswith(".hidden"):
                    self.assertIsNone(got)
                else:
                    self.assertEqual(got, data, rel)
            self.assertIsNone(image.read("/BDMV/nothing"))

    def test_needs_bdmv(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        with self.assertRaises(UdfError):
            udf250.write_image(empty, os.path.join(self.tmp, "x.iso"), "X")

    def test_long_files_use_several_extents(self):
        size = udf250.MAX_EXTENT * 2 + 5000
        fe = udf250.file_entry(10, False, size, 1000, 20, 1, DATE)
        n_ads = struct.unpack("<I", fe[172:176])[0]
        self.assertEqual(n_ads, 3 * 16)                            # long_ad
        ads = [struct.unpack("<IIH", fe[176 + 16 * i:186 + 16 * i]) for i in range(3)]
        self.assertEqual([a[0] for a in ads], [udf250.MAX_EXTENT] * 2 + [5000])
        self.assertEqual([a[1] for a in ads],
                         [1000, 1000 + udf250.MAX_EXTENT // SECTOR,
                          1000 + 2 * udf250.MAX_EXTENT // SECTOR])
        self.assertEqual({a[2] for a in ads}, {0})                 # the physical partition


if __name__ == "__main__":
    unittest.main()

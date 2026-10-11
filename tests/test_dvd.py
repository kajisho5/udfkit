"""The built-in UDF 1.02 + ISO 9660 image writer (udfkit.dvd).

Reference byte strings marked "genisoimage" were taken from an image that
`genisoimage -dvd-video -udf -V SAMPLE_DISC` (cdrkit 1.1.11) wrote for the
sample project; the CRC example is the one given in ECMA-167 3/7.2.6.

The image is read back with a small reader below (independent of the writer:
it follows AVDP -> VDS -> FSD -> File Entries -> FIDs, and the ISO 9660 PVD ->
directory records and path tables) that also checks every descriptor tag.
"""

import datetime
import os
import shutil
import struct
import subprocess
import tempfile
import unittest

from udfkit import dvd as udfwriter
from udfkit.errors import UdfError
from udfkit.info import _dstring, volume_names



def have(*names):
    return all(shutil.which(n) for n in names)

SECTOR = 2048
UTC = datetime.timezone.utc
DATE = datetime.datetime(2026, 10, 10, 14, 4, 59, tzinfo=UTC)


# =============================================================================
# an independent reader for the tests
# =============================================================================

def _check_tag(test, block, location, what):
    tid, ver, cks, _, _, crc, crc_len, loc = struct.unpack_from("<HHBBHHHI", block, 0)
    test.assertEqual(cks, (sum(block[0:4]) + sum(block[5:16])) & 0xFF, f"{what}: checksum")
    test.assertEqual(crc, udfwriter.crc_itu_t(block[16:16 + crc_len]), f"{what}: CRC")
    test.assertEqual(loc, location, f"{what}: tag location")
    test.assertEqual(ver, 2, f"{what}: descriptor version")
    return tid


def read_udf(test, data):
    """{path: (absolute sector or None, size)} for files, set of dirs, and
    the partition start, read through UDF."""
    sec = lambda n: data[n * SECTOR:(n + 1) * SECTOR]   # noqa: E731
    test.assertEqual(sec(18)[1:6] + sec(19)[1:6] + sec(20)[1:6], b"BEA01NSR02TEA01")
    avdp = sec(256)
    test.assertEqual(_check_tag(test, avdp, 256, "AVDP"), 2)
    main_len, main_loc = struct.unpack_from("<II", avdp, 16)
    # the last sector is an anchor too
    last = len(data) // SECTOR - 1
    test.assertEqual(_check_tag(test, sec(last), last, "end AVDP"), 2)
    seen = []
    part_start = fsd_block = None
    for n in range(main_loc, main_loc + main_len // SECTOR):
        d = sec(n)
        tid = _check_tag(test, d, n, f"VDS sector {n}")
        seen.append(tid)
        if tid == 5:
            part_start, part_len = struct.unpack_from("<II", d, 188)
        elif tid == 6:
            fsd_block = struct.unpack_from("<I", d, 252)[0]
            test.assertEqual(struct.unpack_from("<I", d, 212)[0], SECTOR)
        elif tid == 8:
            break
    test.assertEqual(seen, [1, 4, 5, 6, 7, 8])
    fsd = sec(part_start + fsd_block)
    test.assertEqual(_check_tag(test, fsd, fsd_block, "FSD"), 256)
    root_block = struct.unpack_from("<I", fsd, 404)[0]
    files, dirs, unique_ids = {}, set(), []

    def entry(block):
        fe = sec(part_start + block)
        test.assertEqual(_check_tag(test, fe, block, f"FE {block}"), 261)
        ftype = fe[27]
        size = struct.unpack_from("<Q", fe, 56)[0]
        unique_ids.append(struct.unpack_from("<Q", fe, 160)[0])
        l_ea, l_ad = struct.unpack_from("<II", fe, 168)
        ads = [struct.unpack_from("<II", fe, 176 + l_ea + i) for i in range(0, l_ad, 8)]
        test.assertEqual(sum(a[0] for a in ads), size)
        return ftype, size, ads

    def walk(block, path):
        ftype, size, ads = entry(block)
        test.assertEqual(ftype, 4)
        dirs.add(path or "/")
        (length, pos), = ads
        raw = data[(part_start + pos) * SECTOR:(part_start + pos) * SECTOR + length]
        p = 0
        first = True
        while p < length:
            fid = raw[p:]
            l_fi, icb, l_iu = fid[19], struct.unpack_from("<I", fid, 24)[0], \
                struct.unpack_from("<H", fid, 36)[0]
            n = (38 + l_fi + l_iu + 3) & ~3
            test.assertEqual(_check_tag(test, fid[:n], pos + p // SECTOR, "FID"), 257)
            if first:
                test.assertEqual(fid[18], 0x0A, "parent entry first")
                first = False
            else:
                name = _dstring(bytes(fid[38 + l_iu:38 + l_iu + l_fi]) + bytes([l_fi]))
                if fid[18] & 2:
                    walk(icb, f"{path}/{name}")
                else:
                    ftype, fsize, fads = entry(icb)
                    test.assertEqual(ftype, 5)
                    for a, b in zip(fads, fads[1:]):   # contiguous extents
                        test.assertEqual(a[1] + a[0] // SECTOR, b[1])
                    files[f"{path}/{name}"] = (part_start + fads[0][1] if fads else None,
                                               fsize)
            p += n

    walk(root_block, "")
    test.assertEqual(len(set(unique_ids)), len(unique_ids), "unique ids are unique")
    return files, dirs, part_start


def read_iso(test, data):
    """{iso path: (sector, size)} for files and set of dirs, through ISO 9660;
    also checks the path tables against the directories."""
    pvd = data[16 * SECTOR:17 * SECTOR]
    test.assertEqual(pvd[0:7], b"\x01CD001\x01")
    test.assertEqual(data[17 * SECTOR:17 * SECTOR + 7], b"\xffCD001\x01")
    test.assertEqual(struct.unpack_from("<I", pvd, 80)[0], len(data) // SECTOR)
    test.assertEqual(struct.unpack_from(">I", pvd, 84)[0], len(data) // SECTOR)
    files, dirs = {}, {}

    def records(sector, size):
        raw = data[sector * SECTOR:sector * SECTOR + size]
        p = 0
        while p < size:
            n = raw[p]
            if n == 0:                          # rest of the sector is empty
                p = (p // SECTOR + 1) * SECTOR
                continue
            test.assertLessEqual(p % SECTOR + n, SECTOR, "record crosses a sector")
            rec = raw[p:p + n]
            loc, ln = struct.unpack_from("<I", rec, 2)[0], struct.unpack_from("<I", rec, 10)[0]
            test.assertEqual(struct.unpack_from(">I", rec, 6)[0], loc)
            test.assertEqual(struct.unpack_from(">I", rec, 14)[0], ln)
            yield rec[33:33 + rec[32]], loc, ln, bool(rec[25] & 2)
            p += n

    def walk(sector, size, path):
        dirs[path or "/"] = sector
        names = []
        for ident, loc, ln, is_dir in records(sector, size):
            if ident in (b"\0", b"\1"):
                continue
            name = ident.decode("ascii")
            names.append(name)
            if is_dir:
                walk(loc, ln, f"{path}/{name}")
            else:
                files[f"{path}/{name}"] = (loc, ln)
        key = [(n.split(";")[0].partition(".")[0], n.split(";")[0].partition(".")[2])
               for n in names]
        test.assertEqual(key, sorted(key), f"ISO 9660 9.3 order in {path or '/'}")

    root = pvd[156:190]
    walk(struct.unpack_from("<I", root, 2)[0], struct.unpack_from("<I", root, 10)[0], "")
    # path tables: both byte orders list every directory at its extent
    pt_size = struct.unpack_from("<I", pvd, 132)[0]
    l_loc, m_loc = struct.unpack_from("<I", pvd, 140)[0], struct.unpack_from(">I", pvd, 148)[0]
    for loc, fmt in ((l_loc, "<IH"), (m_loc, ">IH")):
        raw = data[loc * SECTOR:loc * SECTOR + pt_size]
        p, extents = 0, []
        while p < pt_size:
            n = raw[p]
            extents.append(struct.unpack_from(fmt, raw, p + 2)[0])
            p += 8 + n + (n & 1)
        test.assertEqual(sorted(extents), sorted(dirs.values()))
    return files, set(dirs)


# =============================================================================
# fixtures
# =============================================================================

def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def fake_video_ts(root, sectors=None):
    """VIDEO_TS with the right names and small fake (non-IFO) contents."""
    sectors = sectors or {"VIDEO_TS.IFO": 2, "VIDEO_TS.VOB": 3, "VIDEO_TS.BUP": 2,
                          "VTS_01_0.IFO": 2, "VTS_01_0.VOB": 4, "VTS_01_1.VOB": 5,
                          "VTS_01_2.VOB": 1, "VTS_01_0.BUP": 2,
                          "VTS_02_0.IFO": 1, "VTS_02_1.VOB": 3, "VTS_02_0.BUP": 1}
    for i, (name, n) in enumerate(sorted(sectors.items())):
        _write(os.path.join(root, "VIDEO_TS", name), bytes([65 + i]) * (n * SECTOR))
    return sectors


def ifo_video_ts(root, vmg_last=31, vts_start=32):
    """VIDEO_TS whose IFOs declare a layout with gaps (like dvdauthor's): a
    32-sector VMG without a menu VOB, and a title set whose title VOBs start
    one sector after the end of its menu VOB."""
    vmg = bytearray(2 * SECTOR)
    vmg[0:12] = b"DVDVIDEO-VMG"
    struct.pack_into(">I", vmg, 0x0C, vmg_last)       # vmg_last_sector
    struct.pack_into(">I", vmg, 0x1C, 1)              # vmgi_last_sector
    struct.pack_into(">I", vmg, 0xC0, 0)              # no VMG menu VOB
    struct.pack_into(">I", vmg, 0xC4, 1)              # TT_SRPT in sector 1
    struct.pack_into(">H", vmg, SECTOR, 2)            # two titles, both in VTS 1
    for i in range(2):
        e = SECTOR + 8 + 12 * i
        vmg[e + 6] = 1
        struct.pack_into(">I", vmg, e + 8, vts_start)
    vts = bytearray(SECTOR)
    vts[0:12] = b"DVDVIDEO-VTS"
    struct.pack_into(">I", vts, 0x0C, 8)              # vts_last_sector
    struct.pack_into(">I", vts, 0x1C, 0)              # vtsi_last_sector
    struct.pack_into(">I", vts, 0xC0, 1)              # vtsm_vobs
    struct.pack_into(">I", vts, 0xC4, 5)              # vtstt_vobs (gap at 4)
    d = os.path.join(root, "VIDEO_TS")
    _write(os.path.join(d, "VIDEO_TS.IFO"), vmg)
    _write(os.path.join(d, "VIDEO_TS.BUP"), vmg)
    _write(os.path.join(d, "VTS_01_0.IFO"), vts)
    _write(os.path.join(d, "VTS_01_0.BUP"), vts)
    _write(os.path.join(d, "VTS_01_0.VOB"), b"m" * (3 * SECTOR))
    _write(os.path.join(d, "VTS_01_1.VOB"), b"t" * (2 * SECTOR))
    _write(os.path.join(d, "VTS_01_2.VOB"), b"u" * SECTOR)


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="udfkit-udf-")
        self.src = os.path.join(self.tmp, "disc")
        os.makedirs(self.src)
        self.img = os.path.join(self.tmp, "disc.iso")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, label="TEST_DISC", **kw):
        stats = udfwriter.write_image(self.src, self.img, label, date=DATE, **kw)
        with open(self.img, "rb") as f:
            self.data = f.read()
        return stats


# =============================================================================
# primitives
# =============================================================================

class TestPrimitives(unittest.TestCase):
    def test_crc_ecma_example(self):
        # ECMA-167 3/7.2.6: "the CRC of the three bytes #70 #6A #77 is #3299"
        self.assertEqual(udfwriter.crc_itu_t(bytes([0x70, 0x6A, 0x77])), 0x3299)
        self.assertEqual(udfwriter.crc_itu_t(b""), 0)

    def test_terminating_descriptor_matches_genisoimage(self):
        # genisoimage, sector 37: Terminating Descriptor of the main VDS
        ref = bytes.fromhex("08000200200000000000f00125000000") + bytes(496)
        self.assertEqual(udfwriter.terminating(37), ref)
        self.assertEqual(udfwriter.tag_checksum(ref), 0x20)

    def test_anchor_matches_genisoimage(self):
        # genisoimage, sector 256: main VDS at 32, reserve at 48, 16 sectors each
        ref = bytes.fromhex("02000200ce00000001d7f00100010000"
                            "00800000200000000080000030000000") + bytes(480)
        self.assertEqual(udfwriter.anchor_pointer(256, 32, 48), ref)

    def test_unallocated_space_matches_genisoimage(self):
        # genisoimage, sector 36: USD, VDS number 4, no extents
        ref = bytes.fromhex("07000200b10000006d0f0800240000000400000000000000")
        self.assertEqual(udfwriter.unallocated_space(36), ref)

    def test_file_identifiers_match_genisoimage(self):
        # genisoimage, sector 260 (root directory, partition block 3): the parent
        # entry (root FE at block 2, unique id 0) and AUDIO_TS (FE at block 4,
        # unique id 261 - genisoimage uses the FE's sector number)
        parent = bytes.fromhex("01010200c80000004762180003000000"
                               "01000a000008000002000000000000000000000000000000")
        self.assertEqual(udfwriter.file_identifier(3, None, 0x0A, 2, 0), parent)
        audio = bytes.fromhex("0101020083000000" "6af2200003000000"
                              "0100020900080000" "0400000000000000"
                              "05010000000008" "415544494f5f5453" "00")
        self.assertEqual(udfwriter.file_identifier(3, "AUDIO_TS", 0x02, 4, 261), audio)

    def test_dstring(self):
        # genisoimage, UDF PVD volume identifier
        ref = bytes.fromhex("0853414d504c455f44495343000000000000000000000000000000000000000c")
        self.assertEqual(udfwriter.dstring("SAMPLE_DISC", 32), ref)
        self.assertEqual(udfwriter.dstring("", 32), bytes(32))
        wide = udfwriter.dstring("ディスク", 32)
        self.assertEqual(wide[0], 16)
        self.assertEqual(_dstring(wide), "ディスク")
        # 32 characters do not fit a 32-byte dstring: 30 are kept
        self.assertEqual(_dstring(udfwriter.dstring("A" * 32, 32)), "A" * 30)
        self.assertEqual(_dstring(udfwriter.dstring("ア" * 20, 32)), "ア" * 15)

    def test_cs0(self):
        self.assertEqual(udfwriter.cs0("Café"), b"\x08Caf\xe9")
        self.assertEqual(udfwriter.cs0("写"), b"\x10\x51\x99")

    def test_udf_timestamp(self):
        # genisoimage, UDF PVD recording time 2026-10-10 14:04:59 UTC
        self.assertEqual(udfwriter.udf_timestamp(DATE), bytes.fromhex("0010ea070a0a0e043b000000"))
        jst = datetime.datetime(2026, 1, 2, 3, 4, 5, 678901,
                                tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
        ts = udfwriter.udf_timestamp(jst)
        self.assertEqual(struct.unpack_from("<H", ts)[0], 0x1000 | 540)   # type 1, +540 min
        self.assertEqual(list(ts[2:]), [0xEA, 0x07, 1, 2, 3, 4, 5, 67, 89, 1])
        est = DATE.astimezone(datetime.timezone(datetime.timedelta(hours=-5)))
        self.assertEqual(struct.unpack_from("<H", udfwriter.udf_timestamp(est))[0],
                         0x1000 | (-300 & 0xFFF))
        naive = datetime.datetime(2026, 10, 10, 14, 4, 59)
        self.assertEqual(udfwriter.udf_timestamp(naive), udfwriter.udf_timestamp(DATE))

    def test_iso_dates(self):
        # genisoimage, PVD root directory record and creation date
        at = datetime.datetime(2026, 10, 10, 14, 4, 56, tzinfo=UTC)
        self.assertEqual(udfwriter.iso_dir_date(at), bytes([126, 10, 10, 14, 4, 56, 0]))
        self.assertEqual(udfwriter.iso_volume_date(DATE), b"2026101014045900\x00")
        jst = DATE.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
        self.assertEqual(udfwriter.iso_dir_date(jst)[6], 36)

    def test_labels(self):
        for good in ("SAMPLE_DISC", "A", "X" * 32, "DISC_2026"):
            self.assertEqual(udfwriter.validate_label(good), good)
        for bad in ("", "lower", "X" * 33, "A-B", "SPACE BAR", "ÄB", None):
            with self.assertRaises(UdfError):
                udfwriter.validate_label(bad)

    def test_dvd_order(self):
        names = ["VTS_02_0.BUP", "VTS_01_1.VOB", "VIDEO_TS.BUP", "VTS_01_0.BUP",
                 "VTS_10_0.IFO", "VTS_01_0.VOB", "VIDEO_TS.VOB", "VTS_01_9.VOB",
                 "VTS_02_0.IFO", "VTS_01_0.IFO", "VIDEO_TS.IFO", "JUNK.TXT", "VTS_01_2.VOB"]
        self.assertEqual(sorted(names, key=udfwriter.dvd_sort_key), [
            "VIDEO_TS.IFO", "VIDEO_TS.VOB", "VIDEO_TS.BUP",
            "VTS_01_0.IFO", "VTS_01_0.VOB", "VTS_01_1.VOB", "VTS_01_2.VOB", "VTS_01_9.VOB",
            "VTS_01_0.BUP", "VTS_02_0.IFO", "VTS_02_0.BUP", "VTS_10_0.IFO", "JUNK.TXT"])

    def test_iso_names(self):
        d = udfwriter._Node("", None, True)
        for name, is_dir in (("README.TXT", False), ("readme.txt", False), ("noext", False),
                             ("Behind the scenes.mpeg", False), ("Café.jpg", False),
                             ("写真.png", False), ("Extras", True), ("a.b.c", False),
                             ("VIDEO_TS", True), ("Long folder name", True)):
            d.children.append(udfwriter._Node(name, None, is_dir, parent=d))
        udfwriter.assign_iso_names(d)
        got = {c.name: udfwriter._iso_ident(c).decode() for c in d.children}
        self.assertEqual(got, {
            "README.TXT": "README.TXT;1", "readme.txt": "README_1.TXT;1",
            "noext": "NOEXT.;1", "Behind the scenes.mpeg": "BEHIND_T.MPE;1",
            "Café.jpg": "CAFE.JPG;1", "写真.png": "_.PNG;1", "Extras": "EXTRAS",
            "a.b.c": "A_B.C;1", "VIDEO_TS": "VIDEO_TS", "Long folder name": "LONG_FOL"})


# =============================================================================
# structure of written images
# =============================================================================

class TestStructure(_TmpCase):
    def test_fake_video_ts(self):
        sizes = fake_video_ts(self.src)
        st = self.build()
        self.assertFalse(st["ifo_layout"])
        self.assertEqual(st["sectors"] * SECTOR, len(self.data))
        self.assertEqual(len(self.data) % SECTOR, 0)
        udf_files, udf_dirs, part = read_udf(self, self.data)
        iso_files, iso_dirs = read_iso(self, self.data)
        self.assertEqual(udf_dirs, {"/", "/VIDEO_TS", "/AUDIO_TS"})
        self.assertEqual(iso_dirs, {"/", "/VIDEO_TS", "/AUDIO_TS"})
        # DVD order, each file right after the previous one
        order = sorted(sizes, key=udfwriter.dvd_sort_key)
        pos = st["data_start"]
        for name in order:
            sector, size = udf_files[f"/VIDEO_TS/{name}"]
            self.assertEqual((sector, size), (pos, sizes[name] * SECTOR), name)
            # ISO 9660 points to the same extent
            self.assertEqual(iso_files[f"/VIDEO_TS/{name};1"], (sector, size))
            with open(os.path.join(self.src, "VIDEO_TS", name), "rb") as f:
                self.assertEqual(self.data[sector * SECTOR:sector * SECTOR + size], f.read())
            pos += sizes[name]
        self.assertEqual(st["extents"]["/VIDEO_TS/VIDEO_TS.IFO"][0], st["data_start"])
        # partition from 257 to the end of the data, anchors after it
        self.assertEqual(part, 257)
        self.assertEqual(st["sectors"], pos + 151)
        self.assertEqual(st["files"], len(sizes))
        names = volume_names(self.img)
        self.assertEqual(names, {"iso9660": "TEST_DISC", "udf_volume": "TEST_DISC",
                                 "udf_logical": "TEST_DISC"})

    def test_ifo_layout(self):
        ifo_video_ts(self.src)
        st = self.build()
        self.assertTrue(st["ifo_layout"])
        rel = {k.split("/")[-1]: v[0] - st["data_start"] for k, v in st["extents"].items()}
        # VMG: IFO 0-1, gap, BUP ends at vmg_last_sector 31; VTS 1 at 32:
        # IFO 32, menu VOB 33-35, gap, title VOBs from 32 + 5, BUP at 32 + 8
        self.assertEqual(rel, {"VIDEO_TS.IFO": 0, "VIDEO_TS.BUP": 30, "VTS_01_0.IFO": 32,
                               "VTS_01_0.VOB": 33, "VTS_01_1.VOB": 37, "VTS_01_2.VOB": 39,
                               "VTS_01_0.BUP": 40})
        self.assertEqual(st["sectors"], st["data_start"] + 41 + 151)
        udf_files, _, _ = read_udf(self, self.data)
        iso_files, _ = read_iso(self, self.data)
        for k, (sector, size) in udf_files.items():
            self.assertEqual(iso_files[k + ";1"], (sector, size))
        # gaps are zero-filled
        gap = st["data_start"] + 2
        self.assertEqual(self.data[gap * SECTOR:(gap + 28) * SECTOR], bytes(28 * SECTOR))

    def test_ifo_layout_conflict(self):
        # the IFO says the VMG ends at sector 2, but IFO + BUP need 4
        ifo_video_ts(self.src, vmg_last=2)
        with self.assertRaises(UdfError) as cm:
            self.build()
        self.assertIn("VIDEO_TS.BUP", str(cm.exception))
        self.assertFalse(os.path.exists(self.img))

    def test_dvd_rom_content(self):
        fake_video_ts(self.src, {"VIDEO_TS.IFO": 1, "VIDEO_TS.BUP": 1})
        os.makedirs(os.path.join(self.src, "AUDIO_TS"))
        content = {
            "README.TXT": b"fictional readme\n",
            # two names that become the same ISO 9660 name (not case twins:
            # macOS file systems usually ignore case)
            "Read me.txt": b"first\n",
            "read-me.txt": b"second\n",
            "Extras/Behind the scenes (long name).mpeg": b"x" * 5000,
            "Extras/photos/Café Ünïcode.jpg": b"j" * 3000,
            "Extras/photos/写真ギャラリー.png": b"p" * 100,
            "Extras/empty.dat": b"",
            "Extras/a/b/c/d/e/f/deep.txt": b"seven levels down\n",
            "big.bin": bytes(range(256)) * 300,
            ".hidden": b"left out",
            "Extras/.DS_Store": b"left out",
        }
        for rel, data in content.items():
            _write(os.path.join(self.src, rel), data)
        os.makedirs(os.path.join(self.src, "Empty Folder"))
        st = self.build()
        udf_files, udf_dirs, _ = read_udf(self, self.data)
        iso_files, iso_dirs = read_iso(self, self.data)
        expected = {"/" + k: v for k, v in content.items() if "/." not in "/" + k}
        expected.update({f"/VIDEO_TS/{n}": None for n in ("VIDEO_TS.IFO", "VIDEO_TS.BUP")})
        self.assertEqual(set(udf_files), set(expected))
        self.assertIn("/Empty Folder", udf_dirs)
        self.assertIn("/Extras/a/b/c/d/e/f", udf_dirs)
        self.assertIn("/AUDIO_TS", udf_dirs)
        self.assertEqual(len(iso_files), len(udf_files))
        self.assertEqual(len(iso_dirs), len(udf_dirs))
        for path, data in expected.items():
            sector, size = udf_files[path]
            iso = st["iso_names"][path]
            self.assertEqual(iso_files[iso], (sector or iso_files[iso][0], size), path)
            if data is not None:
                self.assertEqual(size, len(data))
                if data:
                    self.assertEqual(self.data[sector * SECTOR:sector * SECTOR + size], data)
        self.assertEqual(st["iso_names"]["/Read me.txt"], "/READ_ME.TXT;1")
        self.assertEqual(st["iso_names"]["/read-me.txt"], "/READ_M_1.TXT;1")
        self.assertEqual(st["iso_names"]["/Extras/photos/写真ギャラリー.png"],
                         "/EXTRAS/PHOTOS/_.PNG;1")
        # DVD-ROM data comes after VIDEO_TS
        vts_end = max(s + (n + SECTOR - 1) // SECTOR for p, (s, n) in udf_files.items()
                      if p.startswith("/VIDEO_TS/"))
        self.assertTrue(all(s >= vts_end for p, (s, n) in udf_files.items()
                            if not p.startswith("/VIDEO_TS/") and s))

    def test_audio_ts_created(self):
        fake_video_ts(self.src, {"VIDEO_TS.IFO": 1})
        self.build()
        _, udf_dirs, _ = read_udf(self, self.data)
        _, iso_dirs = read_iso(self, self.data)
        self.assertIn("/AUDIO_TS", udf_dirs)
        self.assertIn("/AUDIO_TS", iso_dirs)
        self.assertFalse(os.path.exists(os.path.join(self.src, "AUDIO_TS")))

    def test_lowercase_video_ts_names_upper_cased(self):
        _write(os.path.join(self.src, "VIDEO_TS", "video_ts.ifo"), bytes(SECTOR))
        st = self.build()
        self.assertIn("/VIDEO_TS/VIDEO_TS.IFO", st["extents"])

    def test_long_label(self):
        fake_video_ts(self.src, {"VIDEO_TS.IFO": 1})
        label = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
        st = self.build(label)
        self.assertEqual(st["udf_volume"], label[:30])
        self.assertEqual(volume_names(self.img), {"iso9660": label, "udf_volume": label[:30],
                                                  "udf_logical": label})

    def test_progress(self):
        fake_video_ts(self.src)
        seen = []
        self.build(on_progress=seen.append)
        self.assertEqual(seen[0], 0.0)
        self.assertEqual(seen[-1], 1.0)
        self.assertEqual(seen, sorted(seen))

    def test_output_inside_source_is_skipped(self):
        fake_video_ts(self.src, {"VIDEO_TS.IFO": 1})
        self.img = os.path.join(self.src, "disc.iso")
        _write(self.img, b"old image")
        st = self.build()
        self.assertNotIn("/disc.iso", st["extents"])


class TestDeterminism(_TmpCase):
    def test_same_input_same_bytes(self):
        fake_video_ts(self.src)
        _write(os.path.join(self.src, "Extras", "notes.txt"), b"fictional notes\n")
        self.build()
        first = self.data
        self.build()
        self.assertEqual(first, self.data)
        udfwriter.write_image(self.src, self.img, "TEST_DISC",
                              date=DATE + datetime.timedelta(seconds=1))
        with open(self.img, "rb") as f:
            self.assertNotEqual(first, f.read())


class TestErrors(_TmpCase):
    def assertFails(self, text, label="TEST_DISC"):
        with self.assertRaises(UdfError) as cm:
            udfwriter.write_image(self.src, self.img, label, date=DATE)
        self.assertIn(text, str(cm.exception))
        self.assertFalse(os.path.exists(self.img))

    def test_missing_video_ts(self):
        self.assertFails("no VIDEO_TS")

    def test_bad_label(self):
        fake_video_ts(self.src, {"VIDEO_TS.IFO": 1})
        for bad in ("my disc", "x" * 33, ""):
            self.assertFails("invalid volume label", label=bad)

    def test_file_over_1_gib(self):
        fake_video_ts(self.src, {"VIDEO_TS.IFO": 1})
        path = os.path.join(self.src, "VIDEO_TS", "VTS_01_1.VOB")
        with open(path, "wb") as f:
            f.truncate(udfwriter.GIB + SECTOR)       # sparse
        self.assertFails("1 GiB")

    def test_non_83_name_in_video_ts(self):
        fake_video_ts(self.src, {"VIDEO_TS.IFO": 1})
        _write(os.path.join(self.src, "VIDEO_TS", "notes.text"), b"x")
        self.assertFails("8.3")

    def test_folder_in_video_ts(self):
        fake_video_ts(self.src, {"VIDEO_TS.IFO": 1})
        os.makedirs(os.path.join(self.src, "VIDEO_TS", "SUB"))
        self.assertFails("only files")

    def test_too_deep(self):
        fake_video_ts(self.src, {"VIDEO_TS.IFO": 1})
        _write(os.path.join(self.src, *"abcdefgh", "x.txt"), b"x")
        self.assertFails("nested too deeply")

    def test_title_set_without_ifo(self):
        fake_video_ts(self.src, {"VIDEO_TS.IFO": 1, "VTS_01_1.VOB": 1})
        self.assertFails("VTS_01_0.IFO")


# =============================================================================
# cross-checks with external tools (skipped when they are missing)
# =============================================================================

@unittest.skipUnless(have("isoinfo"), "isoinfo not installed")
class TestIsoinfo(_TmpCase):
    def test_isoinfo_lists_and_extracts(self):
        sizes = fake_video_ts(self.src)
        _write(os.path.join(self.src, "Extras", "Read me.txt"), b"fictional\n")
        self.build("ISOINFO_TEST")
        d = subprocess.run(["isoinfo", "-d", "-i", self.img], capture_output=True,
                           text=True, check=True).stdout
        self.assertIn("Volume id: ISOINFO_TEST\n", d)
        self.assertIn(f"Volume size is: {len(self.data) // SECTOR}\n", d)
        p = subprocess.run(["isoinfo", "-l", "-i", self.img], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stderr, "")
        for name in sizes:
            self.assertIn(f" {name};1 ", p.stdout)
            out = subprocess.run(["isoinfo", "-i", self.img, "-x", f"/VIDEO_TS/{name};1"],
                                 capture_output=True, check=True).stdout
            with open(os.path.join(self.src, "VIDEO_TS", name), "rb") as f:
                self.assertEqual(out, f.read(), name)
        out = subprocess.run(["isoinfo", "-i", self.img, "-x", "/EXTRAS/READ_ME.TXT;1"],
                             capture_output=True, check=True).stdout
        self.assertEqual(out, b"fictional\n")


@unittest.skipUnless(have("ffmpeg", "dvdauthor", "genisoimage"),
                     "needs ffmpeg, dvdauthor and genisoimage")
class TestRealDisc(unittest.TestCase):
    """A real (small) VIDEO_TS made by ffmpeg and dvdauthor, written by
    udfkit and by `genisoimage -dvd-video -udf`: the same image size, the
    same UDF sector map, and every file's bytes where both file systems say."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="udfkit-real-")
        mpg = os.path.join(cls.tmp, "t.mpg")
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=size=720x480:rate=30000/1001:duration=3", "-f", "lavfi", "-i",
                        "sine=d=3:r=48000", "-target", "ntsc-dvd", "-shortest", mpg], check=True)
        cls.src = os.path.join(cls.tmp, "disc")
        env = dict(os.environ, VIDEO_FORMAT="NTSC")
        for args in (["-t", mpg], ["-T"]):
            subprocess.run(["dvdauthor", "-o", cls.src] + args, check=True, env=env,
                           capture_output=True)
        cls.img = os.path.join(cls.tmp, "udfkit.iso")
        cls.stats = udfwriter.write_image(cls.src, cls.img, "SAMPLE_DISC", date=DATE)
        cls.ref = os.path.join(cls.tmp, "genisoimage.iso")
        subprocess.run(["genisoimage", "-quiet", "-dvd-video", "-udf", "-V", "SAMPLE_DISC",
                        "-o", cls.ref, cls.src], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_same_as_genisoimage(self):
        self.assertTrue(self.stats["ifo_layout"])
        self.assertEqual(os.path.getsize(self.ref), self.stats["bytes"])
        with open(self.ref, "rb") as f:
            ref_files, _, ref_part = read_udf(self, f.read())
        with open(self.img, "rb") as f:
            data = f.read()
        files, _, part = read_udf(self, data)
        self.assertEqual(part, ref_part)
        self.assertEqual(files, ref_files)
        iso_files, _ = read_iso(self, data)
        for path, (sector, size) in files.items():
            self.assertEqual(iso_files[path + ";1"], (sector, size))
            with open(os.path.join(self.src, path.lstrip("/")), "rb") as f:
                self.assertEqual(data[sector * SECTOR:sector * SECTOR + size], f.read(), path)


if __name__ == "__main__":
    unittest.main()

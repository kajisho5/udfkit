"""The checksum manifest and `verify`: a corrupted byte is named by the file it
belongs to, and checked against sha256sum where that is installed."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from udfkit import bluray, dvd, manifest
from udfkit.__main__ import main
from udfkit.errors import UdfError

from .test_dvd import fake_video_ts

SECTOR = 2048


def flip(path, offset):
    with open(path, "r+b") as f:
        f.seek(offset)
        b = f.read(1)
        f.seek(offset)
        f.write(bytes([b[0] ^ 0xFF]))


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(list(args))
    return rc, out.getvalue(), err.getvalue()


class TestDvdManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="udfkit-sum-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        src = os.path.join(self.tmp, "disc")
        fake_video_ts(src)
        self.img = os.path.join(self.tmp, "d.iso")
        rc, out, _ = run_cli("dvd", src, self.img, "--label", "MY_DISC")
        self.assertEqual(rc, 0)
        self.assertIn(f"{self.img}.manifest.json: written", out)
        with open(self.img + ".manifest.json", encoding="utf-8") as f:
            self.doc = json.load(f)
        self.extents = {e["path"]: (e["sector"], e["bytes"]) for e in self.doc["files"]}

    def test_manifest_contents(self):
        with open(self.img, "rb") as f:
            data = f.read()
        self.assertEqual(self.doc["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.doc["bytes"], len(data))
        self.assertEqual(len(self.doc["files"]), 11)
        for e in self.doc["files"]:
            body = data[e["sector"] * SECTOR:e["sector"] * SECTOR + e["bytes"]]
            self.assertEqual(hashlib.sha256(body).hexdigest(), e["sha256"], e["path"])
        with open(self.img + ".sha256", encoding="utf-8") as f:
            self.assertEqual(f.read(), f"{self.doc['sha256']}  d.iso\n")

    def test_clean_image_verifies(self):
        rc, out, err = run_cli("verify", self.img)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("OK", out)
        self.assertIn("11 files match", out)

    def test_corrupted_byte_names_its_file(self):
        target = "/VIDEO_TS/VTS_01_1.VOB"
        sector, size = self.extents[target]
        flip(self.img, sector * SECTOR + size // 2)
        r = manifest.verify(self.img)
        self.assertFalse(r["ok"])
        self.assertEqual(r["bad_files"], [target])
        self.assertFalse(r["outside_files"])
        rc, _, err = run_cli("verify", self.img)
        self.assertEqual(rc, 1)
        self.assertIn(f"udfkit: {self.img}: {target} does not match the manifest", err)
        self.assertEqual(err.count("does not match"), 1)
        rc, out, _ = run_cli("verify", self.img, "--json")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["bad_files"], [target])

    def test_first_and_last_byte_of_a_file(self):
        target = "/VIDEO_TS/VIDEO_TS.IFO"
        sector, size = self.extents[target]
        for offset in (sector * SECTOR, sector * SECTOR + size - 1):
            flip(self.img, offset)
            self.assertEqual(manifest.verify(self.img)["bad_files"], [target])
            flip(self.img, offset)          # back as it was
        self.assertTrue(manifest.verify(self.img)["ok"])

    def test_damage_outside_files(self):
        flip(self.img, 16 * SECTOR + 100)           # the ISO 9660 primary volume descriptor
        r = manifest.verify(self.img)
        self.assertFalse(r["ok"])
        self.assertEqual(r["bad_files"], [])
        self.assertTrue(r["outside_files"])
        rc, _, err = run_cli("verify", self.img)
        self.assertEqual(rc, 1)
        self.assertIn("outside every file", err)

    def test_truncated_image(self):
        with open(self.img, "r+b") as f:
            f.truncate(self.doc["bytes"] - 10 * SECTOR)
        r = manifest.verify(self.img)
        self.assertFalse(r["ok"])
        rc, _, err = run_cli("verify", self.img)
        self.assertEqual(rc, 1)
        self.assertIn("the manifest says", err)

    def test_sha256_file_disagrees(self):
        with open(self.img + ".sha256", "w", encoding="utf-8") as f:
            f.write("0" * 64 + "  d.iso\n")
        r = manifest.verify(self.img)
        self.assertEqual((r["ok"], r["sha256_file"], r["bad_files"]), (False, False, []))

    def test_no_manifest(self):
        os.remove(self.img + ".manifest.json")
        with self.assertRaises(UdfError):
            manifest.verify(self.img)
        rc, _, err = run_cli("verify", self.img)
        self.assertEqual(rc, 1)
        self.assertIn("does not exist", err)

    def test_manifest_elsewhere(self):
        other = os.path.join(self.tmp, "elsewhere.json")
        os.rename(self.img + ".manifest.json", other)
        rc, _, _ = run_cli("verify", self.img, "--manifest", other)
        self.assertEqual(rc, 0)

    def test_not_a_manifest(self):
        with open(self.img + ".manifest.json", "w", encoding="utf-8") as f:
            f.write("{}")
        with self.assertRaises(UdfError):
            manifest.verify(self.img)

    @unittest.skipUnless(shutil.which("sha256sum"), "sha256sum is not installed")
    def test_sha256sum_reads_it(self):
        r = subprocess.run(["sha256sum", "-c", "d.iso.sha256"], cwd=self.tmp,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_no_manifest_flag(self):
        src = os.path.join(self.tmp, "disc")
        img = os.path.join(self.tmp, "n.iso")
        rc, out, _ = run_cli("dvd", src, img, "--label", "N", "--no-manifest")
        self.assertEqual(rc, 0)
        self.assertNotIn("written", out)
        self.assertFalse(os.path.exists(img + ".manifest.json"))
        self.assertFalse(os.path.exists(img + ".sha256"))


class TestBlurayManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="udfkit-sum-bd-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        src = os.path.join(self.tmp, "bd")
        files = {"BDMV/index.bdmv": b"INDX0200" + bytes(100),
                 "BDMV/STREAM/00001.m2ts": os.urandom(192 * 300),
                 "BDMV/AUXDATA/empty.bin": b"",
                 "Extras/日本語.txt": "ファイル名".encode("utf-8")}
        for rel, data in files.items():
            path = os.path.join(src, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        self.img = os.path.join(self.tmp, "bd.iso")
        self.stats = bluray.write_image(src, self.img, "MY_BD")
        manifest.write_manifest(self.img, self.stats["extents"])

    def test_corrupted_byte_names_its_file(self):
        self.assertTrue(manifest.verify(self.img)["ok"])
        target = "/BDMV/STREAM/00001.m2ts"
        sector, size = self.stats["extents"][target]
        flip(self.img, sector * SECTOR + 1234)
        r = manifest.verify(self.img)
        self.assertEqual((r["ok"], r["bad_files"]), (False, [target]))

    def test_unicode_path(self):
        with open(self.img + ".manifest.json", encoding="utf-8") as f:
            self.assertIn("/Extras/日本語.txt", f.read())
        target = "/Extras/日本語.txt"
        sector, _ = self.stats["extents"][target]
        flip(self.img, sector * SECTOR)
        self.assertEqual(manifest.verify(self.img)["bad_files"], [target])


class TestPythonApi(unittest.TestCase):
    def test_write_then_verify(self):
        tmp = tempfile.mkdtemp(prefix="udfkit-sum-api-")
        self.addCleanup(shutil.rmtree, tmp, True)
        src = os.path.join(tmp, "disc")
        fake_video_ts(src)
        img = os.path.join(tmp, "d.iso")
        stats = dvd.write_image(src, img, "API")
        written = manifest.write_manifest(img, stats["extents"])
        self.assertEqual(written, (img + ".sha256", img + ".manifest.json"))
        r = manifest.verify(img)
        self.assertTrue(r["ok"])
        self.assertEqual(r["files"], stats["files"])


if __name__ == "__main__":
    unittest.main()

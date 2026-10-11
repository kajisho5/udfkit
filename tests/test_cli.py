"""The command line, on a small DVD and Blu-ray folder."""

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from udfkit.__main__ import main

from .test_dvd import fake_video_ts


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="udfkit-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_dvd_and_info(self):
        src = os.path.join(self.tmp, "disc")
        fake_video_ts(src)
        img = os.path.join(self.tmp, "d.iso")
        rc, out, _ = self.run_cli("dvd", src, img, "--label", "MY_DISC")
        self.assertEqual(rc, 0)
        self.assertIn("bytes", out)
        rc, out, _ = self.run_cli("info", img)
        self.assertEqual(rc, 0)
        self.assertIn('"udf_volume": "MY_DISC"', out)

    def test_bluray(self):
        src = os.path.join(self.tmp, "bd")
        os.makedirs(os.path.join(src, "BDMV", "STREAM"))
        with open(os.path.join(src, "BDMV", "index.bdmv"), "wb") as f:
            f.write(b"INDX0200" + bytes(32))
        rc, _, _ = self.run_cli("bluray", src, os.path.join(self.tmp, "b.iso"), "--label", "BD")
        self.assertEqual(rc, 0)

    def test_error(self):
        rc, _, err = self.run_cli("dvd", self.tmp, os.path.join(self.tmp, "x.iso"),
                                  "--label", "bad label")
        self.assertEqual(rc, 1)
        self.assertIn("udfkit:", err)

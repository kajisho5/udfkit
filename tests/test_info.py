import unittest

from udfkit import info as udfinfo


class TestDstring(unittest.TestCase):
    def test_8bit(self):
        raw = bytes([8]) + b"SAMPLE_DISC" + bytes(32 - 13) + bytes([12])
        self.assertEqual(udfinfo._dstring(raw), "SAMPLE_DISC")

    def test_16bit(self):
        body = "ディスク".encode("utf-16-be")
        raw = bytes([16]) + body + bytes(32 - 2 - len(body)) + bytes([1 + len(body)])
        self.assertEqual(udfinfo._dstring(raw), "ディスク")

    def test_empty(self):
        self.assertEqual(udfinfo._dstring(bytes(32)), "")


if __name__ == "__main__":
    unittest.main()

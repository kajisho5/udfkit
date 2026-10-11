"""Read the volume names out of a UDF (+ ISO 9660) image.

libdvdread, and therefore libdvdnav, VLC and mpv, takes the disc title from
the UDF Primary Volume Descriptor and falls back to ISO 9660 only when the
image has no UDF. It is used to check that a label actually landed in an image.
"""

import struct

SECTOR = 2048


def _dstring(raw):
    """OSTA CS0 dstring: [compression id][chars...][length byte]."""
    if not raw:
        return ""
    length = raw[-1]
    if length == 0:
        return ""
    comp, body = raw[0], raw[1:length]
    if comp == 8:
        return body.decode("latin-1")
    if comp == 16:
        return body.decode("utf-16-be", "replace")
    return ""


def volume_names(path):
    """{"iso9660": str|None, "udf_volume": str|None, "udf_logical": str|None}."""
    out = {"iso9660": None, "udf_volume": None, "udf_logical": None}
    with open(path, "rb") as f:
        f.seek(16 * SECTOR)
        pvd = f.read(SECTOR)
        if pvd[1:6] == b"CD001" and pvd[0] == 1:
            out["iso9660"] = pvd[40:72].decode("ascii", "replace").rstrip(" \x00")
        f.seek(256 * SECTOR)
        avdp = f.read(SECTOR)
        if len(avdp) < 24 or struct.unpack_from("<H", avdp, 0)[0] != 2:
            return out
        length, loc = struct.unpack_from("<II", avdp, 16)
        for i in range(max(1, length // SECTOR)):
            f.seek((loc + i) * SECTOR)
            d = f.read(SECTOR)
            tag = struct.unpack_from("<H", d, 0)[0]
            if tag == 1:
                out["udf_volume"] = _dstring(d[24:56])
            elif tag == 6:
                out["udf_logical"] = _dstring(d[84:212])
            elif tag == 8:
                break
    return out

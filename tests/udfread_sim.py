"""Read files out of a UDF image with libudfread (via ctypes).

libudfread is the UDF reader libbluray (and so VLC, mpv, Kodi) opens Blu-ray
images with; it understands UDF 2.50 metadata partitions. Only used by
tests; skipped when it is not installed.
"""

import ctypes
import ctypes.util

_CANDIDATES = ("libudfread.so.0", "libudfread.so", "libudfread.0.dylib", "libudfread.dylib",
               "/opt/homebrew/lib/libudfread.dylib", "/usr/local/lib/libudfread.dylib")


def load_library():
    names = [n for n in (ctypes.util.find_library("udfread"),) if n] + list(_CANDIDATES)
    for n in names:
        try:
            lib = ctypes.CDLL(n)
        except OSError:
            continue
        P = ctypes.c_void_p
        lib.udfread_init.restype = P
        lib.udfread_open.argtypes = [P, ctypes.c_char_p]
        lib.udfread_open.restype = ctypes.c_int
        lib.udfread_close.argtypes = [P]
        lib.udfread_get_volume_id.restype = ctypes.c_char_p
        lib.udfread_get_volume_id.argtypes = [P]
        lib.udfread_file_open.restype = P
        lib.udfread_file_open.argtypes = [P, ctypes.c_char_p]
        lib.udfread_file_size.restype = ctypes.c_int64
        lib.udfread_file_size.argtypes = [P]
        lib.udfread_file_read.restype = ctypes.c_ssize_t
        lib.udfread_file_read.argtypes = [P, ctypes.c_void_p, ctypes.c_size_t]
        lib.udfread_file_close.argtypes = [P]
        return lib
    return None


class Image:
    def __init__(self, lib, path):
        self.lib = lib
        self.udf = lib.udfread_init()
        if lib.udfread_open(self.udf, path.encode()) < 0:
            lib.udfread_close(self.udf)
            raise RuntimeError(f"libudfread cannot open {path}")

    def close(self):
        if self.udf:
            self.lib.udfread_close(self.udf)
            self.udf = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def volume_id(self):
        return self.lib.udfread_get_volume_id(self.udf).decode()

    def read(self, path):
        """The whole file at `path` ("/BDMV/index.bdmv"), or None."""
        f = self.lib.udfread_file_open(self.udf, path.encode())
        if not f:
            return None
        try:
            size = self.lib.udfread_file_size(f)
            buf = ctypes.create_string_buffer(max(size, 1))
            got = 0
            while got < size:
                n = self.lib.udfread_file_read(f, ctypes.byref(buf, got), size - got)
                if n <= 0:
                    break
                got += n
            return buf.raw[:got]
        finally:
            self.lib.udfread_file_close(f)

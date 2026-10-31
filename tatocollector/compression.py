"""Scoped bounds for Telethon's RPC and service-update gzip entry points."""

import zlib


MAX_COMPRESSED_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 1_048_576


def _read_compressed_bytes(reader):
    try:
        first = reader.read_byte()
        if first == 255:
            raise ValueError('gzip_invalid_bytes')
        if first == 254:
            length = int.from_bytes(reader.read(3), 'little')
            header_size = 4
        else:
            length = first
            header_size = 1
    except BufferError:
        raise ValueError('gzip_invalid_bytes') from None

    if length > MAX_COMPRESSED_BYTES:
        raise ValueError('gzip_input_too_large')
    try:
        packed = reader.read(length)
        reader.read(-(header_size + length) % 4)
    except BufferError:
        raise ValueError('gzip_invalid_bytes') from None
    return packed


def _decompress(reader):
    packed = _read_compressed_bytes(reader)
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        content = inflater.decompress(packed, MAX_OUTPUT_BYTES + 1)
    except zlib.error:
        raise ValueError('gzip_invalid_or_truncated') from None
    if len(content) > MAX_OUTPUT_BYTES:
        raise ValueError('gzip_output_too_large')
    if not inflater.eof:
        raise ValueError('gzip_invalid_or_truncated')
    if inflater.unused_data or inflater.unconsumed_tail:
        raise ValueError('gzip_trailing_data')
    return content


class BoundedGzip:
    """Temporarily guard both GzipPacked descriptors for one connection scope.

    The Telethon class is process-wide; callers must own the scope and exit it
    before another owner changes these descriptors. No dependency file is edited.
    """

    def __enter__(self):
        from telethon.tl.core import GzipPacked

        if getattr(self, '_active', False):
            raise RuntimeError('bounded_gzip_already_active')
        self._gzip_packed = GzipPacked
        self._read = GzipPacked.__dict__['read']
        self._from_reader = GzipPacked.__dict__['from_reader']

        def read(reader):
            try:
                constructor = reader.read_int(signed=False)
            except BufferError:
                raise ValueError('gzip_invalid_bytes') from None
            if constructor != GzipPacked.CONSTRUCTOR_ID:
                raise ValueError('gzip_constructor_mismatch')
            return _decompress(reader)

        def from_reader(cls, reader):
            return cls(_decompress(reader))

        GzipPacked.read = staticmethod(read)
        GzipPacked.from_reader = classmethod(from_reader)
        self._active = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self._gzip_packed.read = self._read
        finally:
            self._gzip_packed.from_reader = self._from_reader
            self._active = False

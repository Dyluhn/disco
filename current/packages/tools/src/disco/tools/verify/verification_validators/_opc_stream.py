"""Bounded validation of raw OPC ZIP member streams."""

from __future__ import annotations

import struct
import zipfile
import zlib
from typing import IO

_ZIP_LOCAL_HEADER = struct.Struct("<4s5H3L2H")
_ZIP_LOCAL_SIGNATURE = b"PK\x03\x04"


def _zip64_field(extra: bytes) -> bytes | None:
    offset = 0
    while offset + 4 <= len(extra):
        field_id, field_size = struct.unpack_from("<HH", extra, offset)
        offset += 4
        field = extra[offset : offset + field_size]
        offset += field_size
        if field_id == 0x0001:
            return field
    return None


def _zip64_local_sizes(
    member: zipfile.ZipInfo,
    local_size: int,
    local_compressed: int,
    local_extra: bytes,
) -> tuple[int, int, str | None]:
    actual_size = local_size
    actual_compressed = local_compressed
    if local_size != 0xFFFFFFFF and local_compressed != 0xFFFFFFFF:
        return actual_size, actual_compressed, None
    zip64 = _zip64_field(local_extra)
    if zip64 is None:
        return (
            actual_size,
            actual_compressed,
            f"pptx member {member.filename} has no ZIP64 size metadata",
        )
    offset = 0
    if local_size == 0xFFFFFFFF:
        if len(zip64) < offset + 8:
            return (
                actual_size,
                actual_compressed,
                f"pptx member {member.filename} has truncated ZIP64 metadata",
            )
        actual_size = struct.unpack_from("<Q", zip64, offset)[0]
        offset += 8
    if local_compressed == 0xFFFFFFFF:
        if len(zip64) < offset + 8:
            return (
                actual_size,
                actual_compressed,
                f"pptx member {member.filename} has truncated ZIP64 metadata",
            )
        actual_compressed = struct.unpack_from("<Q", zip64, offset)[0]
    return actual_size, actual_compressed, None


def _local_sizes_problem(
    member: zipfile.ZipInfo,
    local_flags: int,
    local_crc: int,
    local_compressed: int,
    local_size: int,
    local_extra: bytes,
) -> str | None:
    if local_flags & 0x08:
        return None
    actual_size, actual_compressed, zip64_problem = _zip64_local_sizes(
        member, local_size, local_compressed, local_extra
    )
    if zip64_problem is not None:
        return zip64_problem
    if (
        local_crc != member.CRC
        or actual_compressed != member.compress_size
        or actual_size != member.file_size
    ):
        return (
            f"pptx member {member.filename} disagrees between local and central "
            "metadata"
        )
    return None


def _position_at_member_data(handle: IO[bytes], member: zipfile.ZipInfo) -> str | None:
    handle.seek(member.header_offset)
    raw_header = handle.read(_ZIP_LOCAL_HEADER.size)
    if len(raw_header) != _ZIP_LOCAL_HEADER.size:
        return f"pptx member {member.filename} has a truncated local header"
    (
        signature,
        _version,
        local_flags,
        local_method,
        _mtime,
        _mdate,
        local_crc,
        local_compressed,
        local_size,
        name_size,
        extra_size,
    ) = _ZIP_LOCAL_HEADER.unpack(raw_header)
    if (
        signature != _ZIP_LOCAL_SIGNATURE
        or local_method != member.compress_type
        or local_flags != member.flag_bits
    ):
        return f"pptx member {member.filename} has inconsistent local metadata"
    local_name = handle.read(name_size)
    local_extra = handle.read(extra_size)
    if len(local_name) != name_size or len(local_extra) != extra_size:
        return (
            f"pptx member {member.filename} has truncated local name/extra metadata"
        )
    name_encoding = "utf-8" if local_flags & 0x0800 else "cp437"
    try:
        expected_local_name = member.orig_filename.encode(name_encoding)
    except UnicodeEncodeError:
        return (
            f"pptx member {member.filename} has an invalid local filename encoding"
        )
    if local_name != expected_local_name:
        return f"pptx member {member.filename} disagrees on its local filename"
    return _local_sizes_problem(
        member,
        local_flags,
        local_crc,
        local_compressed,
        local_size,
        local_extra,
    )


def _stored_stream_problem(handle: IO[bytes], member: zipfile.ZipInfo) -> str | None:
    if member.compress_size != member.file_size:
        return (
            f"pptx stored member {member.filename} has inconsistent compressed size"
        )
    remaining = member.compress_size
    produced = 0
    crc = 0
    while remaining:
        chunk = handle.read(min(64 * 1024, remaining))
        if not chunk:
            return (
                f"pptx member {member.filename} has a truncated compressed stream"
            )
        remaining -= len(chunk)
        produced += len(chunk)
        crc = zlib.crc32(chunk, crc)
    if produced != member.file_size:
        return (
            f"pptx member {member.filename} has inconsistent stored stream boundaries"
        )
    if (crc & 0xFFFFFFFF) != member.CRC:
        return f"pptx member {member.filename} failed its CRC check"
    return None


def _deflated_stream_problem(
    handle: IO[bytes], member: zipfile.ZipInfo
) -> str | None:
    remaining = member.compress_size
    produced = 0
    crc = 0
    decompressor = zlib.decompressobj(-15)
    while remaining:
        chunk = handle.read(min(64 * 1024, remaining))
        if not chunk:
            return (
                f"pptx member {member.filename} has a truncated compressed stream"
            )
        remaining -= len(chunk)
        pending = chunk
        while pending:
            data = decompressor.decompress(
                pending, member.file_size + 1 - produced
            )
            produced += len(data)
            crc = zlib.crc32(data, crc)
            if produced > member.file_size:
                return (
                    f"pptx member {member.filename} expands beyond its declared size"
                )
            pending = decompressor.unconsumed_tail
            if pending and produced >= member.file_size + 1:
                return (
                    f"pptx member {member.filename} expands beyond its declared size"
                )
    tail = decompressor.flush(member.file_size + 1 - produced)
    produced += len(tail)
    crc = zlib.crc32(tail, crc)
    if (
        produced != member.file_size
        or not decompressor.eof
        or bool(decompressor.unused_data)
        or bool(decompressor.unconsumed_tail)
    ):
        return (
            f"pptx member {member.filename} has inconsistent deflate stream boundaries"
        )
    if (crc & 0xFFFFFFFF) != member.CRC:
        return f"pptx member {member.filename} failed its CRC check"
    return None


def _member_stream_problem(handle: IO[bytes], member: zipfile.ZipInfo) -> str | None:
    """Validate local metadata and the real compressed stream under strict bounds."""
    if member.flag_bits & 0x1:
        return (
            f"pptx member {member.filename} is encrypted and cannot be safely "
            "inspected"
        )
    if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        return (
            f"pptx member {member.filename} uses unsupported compression method "
            f"{member.compress_type}"
        )
    local_problem = _position_at_member_data(handle, member)
    if local_problem is not None:
        return local_problem
    if member.compress_type == zipfile.ZIP_STORED:
        return _stored_stream_problem(handle, member)
    return _deflated_stream_problem(handle, member)

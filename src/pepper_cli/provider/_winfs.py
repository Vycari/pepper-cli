"""Handle-tied checks for the filesystem provider where ``dir_fd`` is missing.

Windows has no ``openat``/``O_NOFOLLOW``, so :mod:`pepper_cli.provider.fs`
cannot bind an open to the path it checked by walking descriptors. What it can
do is ask the open handle itself where it lives: ``GetFinalPathNameByHandleW``
answers for the object that was actually opened, after every reparse point was
followed, so a containment check on that answer cannot be raced by swapping a
name back and forth around it. Directories are opened with
``FILE_FLAG_BACKUP_SEMANTICS`` and listed *through the handle*
(``GetFileInformationByHandleEx``), so the names a listing returns come from
the directory that passed the check, not from a second lookup by pathname.

The two pure helpers (prefix stripping and the directory-record parser) are
importable and tested everywhere; the ``kernel32`` bindings exist only on
Windows.
"""

from __future__ import annotations

import struct
import sys

_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"

#: ``FILE_FULL_DIR_INFO`` up to ``FileName``: NextEntryOffset, FileIndex, four
#: FILETIMEs, EndOfFile, AllocationSize, FileAttributes, FileNameLength (in
#: bytes), EaSize. Every field is fixed-width, so the layout is the same on
#: 32- and 64-bit Windows.
_FULL_DIR_INFO = struct.Struct("<II4qqqIII")

FILE_ATTRIBUTE_DIRECTORY = 0x10


def strip_extended_prefix(path: str) -> str:
    r"""``\\?\C:\x`` → ``C:\x`` and ``\\?\UNC\srv\share`` → ``\\srv\share``.

    ``GetFinalPathNameByHandleW`` always answers in the extended form, while a
    root resolved with ``Path.resolve`` does not; the containment test needs
    both spelled the same way, as ``ntpath.realpath`` spells them.
    """
    if path.startswith(_EXTENDED_UNC):
        return "\\\\" + path[len(_EXTENDED_UNC) :]
    if path.startswith(_EXTENDED):
        return path[len(_EXTENDED) :]
    return path


def parse_full_dir_info(buf: bytes) -> list[tuple[str, bool]]:
    """``(name, is_dir)`` for each ``FILE_FULL_DIR_INFO`` record in ``buf``.

    Records chain through ``NextEntryOffset`` (0 ends the chain). ``.`` and
    ``..`` are dropped, as ``os.scandir`` drops them.
    """
    entries: list[tuple[str, bool]] = []
    offset = 0
    while True:
        fields = _FULL_DIR_INFO.unpack_from(buf, offset)
        next_offset, attributes, name_bytes = fields[0], fields[8], fields[9]
        start = offset + _FULL_DIR_INFO.size
        name = buf[start : start + name_bytes].decode("utf-16-le")
        if name not in {".", ".."}:
            entries.append((name, bool(attributes & FILE_ATTRIBUTE_DIRECTORY)))
        if next_offset == 0:
            return entries
        offset += next_offset


if sys.platform == "win32":
    import ctypes
    import msvcrt
    import os
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL

    _GetFinalPathNameByHandleW = _kernel32.GetFinalPathNameByHandleW
    _GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _GetFinalPathNameByHandleW.restype = wintypes.DWORD

    _GetFileInformationByHandleEx = _kernel32.GetFileInformationByHandleEx
    _GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _GetFileInformationByHandleEx.restype = wintypes.BOOL

    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_READ_ATTRIBUTES = 0x0080
    _SYNCHRONIZE = 0x00100000
    _FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FULL_DIRECTORY_INFO = 14
    _FILE_FULL_DIRECTORY_RESTART_INFO = 15
    _ERROR_NO_MORE_FILES = 18
    _LIST_BUFFER_BYTES = 64 * 1024

    def open_handle(path: str, *, directory: bool) -> int:
        """A C runtime descriptor for ``path`` (a file or a directory).

        Reparse points are followed, which is the point: the check that
        follows is on where the handle ended up, not on the name.
        """
        access = _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
        if directory:
            access |= _FILE_LIST_DIRECTORY
        handle = _CreateFileW(
            path,
            access,
            _FILE_SHARE_ALL,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle is None or handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            _CloseHandle(handle)
            raise

    def final_path(fd: int) -> str:
        """Where the object open on ``fd`` lives now, in ``realpath``'s spelling."""
        handle = msvcrt.get_osfhandle(fd)
        size = 512
        while True:
            buf = ctypes.create_unicode_buffer(size)
            needed = _GetFinalPathNameByHandleW(handle, buf, size, 0)
            if needed == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            if needed < size:
                return strip_extended_prefix(buf.value)
            size = needed + 1

    def list_dir(fd: int) -> list[tuple[str, bool]]:
        """``(name, is_dir)`` for the directory open on ``fd``, read through the handle."""
        handle = msvcrt.get_osfhandle(fd)
        buf = ctypes.create_string_buffer(_LIST_BUFFER_BYTES)
        info_class = _FILE_FULL_DIRECTORY_RESTART_INFO
        entries: list[tuple[str, bool]] = []
        while True:
            if not _GetFileInformationByHandleEx(handle, info_class, buf, len(buf)):
                error = ctypes.get_last_error()
                if error == _ERROR_NO_MORE_FILES:
                    return entries
                raise ctypes.WinError(error)
            entries.extend(parse_full_dir_info(buf.raw))
            info_class = _FILE_FULL_DIRECTORY_INFO

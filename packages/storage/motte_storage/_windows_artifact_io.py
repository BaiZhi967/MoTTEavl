"""Private Windows handle-bound ordinary artifact I/O (not archive durability).

All mutation uses an acquired handle. Names after the filesystem anchor contain
one component and are opened relative to a live no-follow directory handle.
"""
from __future__ import annotations

import ctypes as C
import os
from contextlib import contextmanager
from pathlib import PureWindowsPath

U8, U16, U32, I32 = C.c_uint8, C.c_uint16, C.c_uint32, C.c_int32
HANDLE = C.c_void_p
SYNCHRONIZE = 0x00100000
DELETE = 0x00010000
READ_ATTRIBUTES = 0x80
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
DIRECTORY_ACCESS = 0x1 | 0x20 | READ_ATTRIBUTES | SYNCHRONIZE
OPEN_REPARSE_POINT, SYNCHRONOUS_IO = 0x00200000, 0x20
REPARSE_POINT = 0x400
MISSING = {0xC0000034, 0xC000003A}  # OBJECT_NAME/PATH_NOT_FOUND only
UNSUPPORTED = {0xC0000002, 0xC0000003, 0xC00000BB}


class UNICODE_STRING(C.Structure):
    _fields_ = [("Length", U16), ("MaximumLength", U16), ("Buffer", HANDLE)]


class OBJECT_ATTRIBUTES(C.Structure):
    _fields_ = [("Length", U32), ("RootDirectory", HANDLE),
                ("ObjectName", C.POINTER(UNICODE_STRING)), ("Attributes", U32),
                ("SecurityDescriptor", HANDLE), ("SecurityQualityOfService", HANDLE)]


class _IO_STATUS(C.Union):
    _fields_ = [("Status", I32), ("Pointer", HANDLE)]


class IO_STATUS_BLOCK(C.Structure):
    _anonymous_ = ("Result",)
    _fields_ = [("Result", _IO_STATUS), ("Information", C.c_size_t)]


class FILE_ID_INFO(C.Structure):
    _fields_ = [("VolumeSerialNumber", C.c_uint64), ("FileId", U8 * 16)]


class FILE_ATTRIBUTE_TAG_INFO(C.Structure):
    _fields_ = [("FileAttributes", U32), ("ReparseTag", U32)]


class FILE_STANDARD_INFO(C.Structure):
    _fields_ = [("AllocationSize", C.c_int64), ("EndOfFile", C.c_int64),
                ("NumberOfLinks", U32), ("DeletePending", U8), ("Directory", U8)]


class FILE_DISPOSITION_INFO(C.Structure):
    _fields_ = [("DeleteFile", U8)]


def _unsupported(message="safe artifact mutation primitives are unsupported"):
    from .artifacts import ArtifactMutationUnsupported
    return ArtifactMutationUnsupported(message)


def _components(artifact_id):
    path = PureWindowsPath(artifact_id)
    if (not isinstance(artifact_id, str) or "\0" in artifact_id or path.drive or path.root
            or not path.parts or ".." in path.parts):
        raise ValueError("artifact path must name a file within the root")
    devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    devices.update(f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³")
    for part in path.parts:
        if (":" in part or part.endswith((".", " "))
                or part.split(".", 1)[0].rstrip(" ").upper() in devices):
            raise ValueError("unsupported Windows artifact name")
    if path.parts[0].casefold() == "trace-archives":
        raise ValueError("trace-archives is a reserved immutable namespace")
    return path.parts


def _unicode_string(name):
    encoded = name.encode("utf-16-le")
    if len(encoded) + 2 > 0xFFFF:
        raise ValueError("Windows artifact component exceeds UNICODE_STRING bounds")
    buffer = C.create_string_buffer(encoded + b"\0\0", len(encoded) + 2)
    return UNICODE_STRING(len(encoded), len(encoded) + 2, C.cast(buffer, HANDLE)), buffer


class _Handle:
    def __init__(self, api, value):
        self.api, self.value = api, value

    def close(self):
        if self.value is not None:
            value, self.value = self.value, None  # Never retry a possibly recycled handle.
            self.api.close(value)


class _Handles:
    def __init__(self, api):
        self.api, self.handles = api, []

    def own(self, value):
        handle = _Handle(self.api, value)
        self.handles.append(handle)
        return handle

    def __enter__(self):
        return self

    def __exit__(self, kind, primary, traceback):
        errors = []
        for handle in reversed(self.handles):
            try:
                handle.close()
            except OSError as error:
                errors.append(error)
        if primary is not None:
            for error in errors:
                primary.add_note(f"handle cleanup also failed: {error}")
        elif errors:
            for error in errors[1:]:
                errors[0].add_note(f"additional handle cleanup failure: {error}")
            raise errors[0]
        return False


class _NativeAPI:
    def __init__(self):
        self.kernel = C.WinDLL("kernel32.dll", use_last_error=True)
        self.nt = C.WinDLL("ntdll.dll")
        bindings = [
            (self.nt, "NtCreateFile", I32,
             [C.POINTER(HANDLE), U32, C.POINTER(OBJECT_ATTRIBUTES), C.POINTER(IO_STATUS_BLOCK),
              HANDLE, U32, U32, U32, U32, HANDLE, U32]),
            (self.nt, "RtlNtStatusToDosError", U32, [I32]),
            (self.kernel, "GetFileType", U32, [HANDLE]),
            (self.kernel, "GetFileInformationByHandleEx", I32, [HANDLE, I32, HANDLE, U32]),
            (self.kernel, "SetFileInformationByHandle", I32, [HANDLE, I32, HANDLE, U32]),
            (self.kernel, "GetFinalPathNameByHandleW", U32, [HANDLE, C.c_wchar_p, U32, U32]),
            (self.kernel, "ReadFile", I32, [HANDLE, HANDLE, U32, C.POINTER(U32), HANDLE]),
            (self.kernel, "WriteFile", I32, [HANDLE, HANDLE, U32, C.POINTER(U32), HANDLE]),
            (self.kernel, "SetFilePointerEx", I32, [HANDLE, C.c_int64, HANDLE, U32]),
            (self.kernel, "SetEndOfFile", I32, [HANDLE]),
            (self.kernel, "CloseHandle", I32, [HANDLE]),
        ]
        for library, name, result, args in bindings:
            function = getattr(library, name)
            function.argtypes, function.restype = args, result

    def _check(self, result, *, capability=False):
        if not result:
            code = C.get_last_error()
            if capability and code in {1, 50, 87, 120}:  # required info/operation unavailable
                raise _unsupported(f"Windows artifact capability is unavailable (error {code})")
            raise C.WinError(code)
        return result

    def close(self, value):
        self._check(self.kernel.CloseHandle(value))

    def open(self, handles, parent, name, *, directory=None, create=False, access=READ_ATTRIBUTES,
             share=7):
        value, buffer = _unicode_string(name)
        attrs = OBJECT_ATTRIBUTES(C.sizeof(OBJECT_ATTRIBUTES), parent, C.pointer(value),
                                  0x40, None, None)  # OBJ_CASE_INSENSITIVE, no inheritance
        status_block, result = IO_STATUS_BLOCK(), HANDLE()
        options = OPEN_REPARSE_POINT | SYNCHRONOUS_IO
        options |= 0x1 if directory is True else 0x40 if directory is False else 0
        status = self.nt.NtCreateFile(C.byref(result), access | SYNCHRONIZE, C.byref(attrs),
                                    C.byref(status_block), None, 0x80, share,
                                    3 if create else 1, options, None, 0)
        owned = None
        if result.value not in (None, 0, C.c_void_p(-1).value):
            owned = handles.own(result.value)
        if status != 0:
            code = status & 0xFFFFFFFF
            # Keep all call buffers alive through any exceptional handle cleanup.
            error = (_unsupported(f"unsupported NtCreateFile status 0x{code:08x}")
                     if code in UNSUPPORTED or status > 0 else
                     FileNotFoundError(2, "artifact component is absent", name)
                     if code in MISSING else C.WinError(self.nt.RtlNtStatusToDosError(status)))
            error.ntstatus = code
            if owned is not None:
                try:
                    owned.close()
                except OSError as cleanup:
                    error.add_note(f"native open cleanup failed: {cleanup}")
            raise error
        if owned is None:
            raise _unsupported("NtCreateFile returned success without a handle")
        return owned

    def info(self, handle):
        if handle.value is None:
            raise OSError("artifact handle is closed")
        if self.kernel.GetFileType(handle.value) != 1:  # FILE_TYPE_DISK
            raise ValueError("artifact must be a disk file")
        values = []
        for kind, structure in ((18, FILE_ID_INFO), (9, FILE_ATTRIBUTE_TAG_INFO),
                                (1, FILE_STANDARD_INFO)):
            value = structure()
            self._check(self.kernel.GetFileInformationByHandleEx(
                handle.value, kind, C.byref(value), C.sizeof(value)), capability=True)
            values.append(value)
        identity, attrs, standard = values
        return ((identity.VolumeSerialNumber, bytes(identity.FileId)), attrs, standard)

    def basename(self, handle):
        capacity = 512
        while capacity <= 65536:
            buffer = C.create_unicode_buffer(capacity)
            count = self.kernel.GetFinalPathNameByHandleW(handle.value, buffer, capacity, 2)
            self._check(count, capability=True)
            if count < capacity:
                name = buffer.value.rpartition("\\")[2]
                if not name:
                    raise _unsupported("ambiguous normalized artifact directory name")
                return name
            capacity = count + 1
        raise _unsupported("normalized artifact directory name is too long")

    def seek_zero(self, handle):
        self._check(self.kernel.SetFilePointerEx(handle.value, 0, None, 0))

    def truncate(self, handle):
        self._check(self.kernel.SetEndOfFile(handle.value))

    def read(self, handle, count):
        buffer, transferred = C.create_string_buffer(count), U32()
        self._check(self.kernel.ReadFile(handle.value, buffer, count, C.byref(transferred), None))
        if transferred.value > count:
            raise OSError("invalid native artifact read length")
        return buffer.raw[:transferred.value]

    def write(self, handle, data):
        buffer, transferred = C.create_string_buffer(data, len(data)), U32()
        self._check(self.kernel.WriteFile(handle.value, buffer, len(data),
                                         C.byref(transferred), None))
        if not 0 < transferred.value <= len(data):
            raise OSError("short artifact write made no valid progress")
        return transferred.value

    def disposition(self, handle):
        value = FILE_DISPOSITION_INFO(1)
        self._check(self.kernel.SetFileInformationByHandle(
            handle.value, 4, C.byref(value), C.sizeof(value)))


_API = None


def _api():
    global _API
    if os.name != "nt":
        raise _unsupported()
    if _API is None:
        try:
            _API = _NativeAPI()
        except (AttributeError, OSError) as error:
            raise _unsupported() from error
    return _API


def _validate(api, handle, *, directory, identity=None, single_link=False):
    current, attrs, standard = api.info(handle)
    if (attrs.FileAttributes & REPARSE_POINT or attrs.ReparseTag
            or bool(standard.Directory) != directory or standard.DeletePending):
        raise ValueError("unsafe artifact type, reparse point, or deletion state")
    if identity is not None and current != identity:
        raise ValueError("artifact namespace identity changed")
    if single_link and standard.NumberOfLinks != 1:
        raise ValueError("hard-linked evidence may alias an immutable Trace archive")
    return current


def _directory(api, handles, parent, name, *, create=False):
    handle = api.open(handles, parent, name, directory=True, create=create,
                      access=DIRECTORY_ACCESS, share=1)
    identity = _validate(api, handle, directory=True)
    return handle, identity


def _root(api, handles, root):
    path = PureWindowsPath(str(root))
    if not path.is_absolute():
        raise ValueError("Windows artifact root must be absolute")
    anchor = path.anchor
    if anchor.startswith("\\\\") and not anchor.startswith(("\\\\?\\", "\\\\.\\")):
        anchor = "\\??\\UNC\\" + anchor[2:]
    elif len(anchor) == 3 and anchor[1:] == ":\\":
        anchor = "\\??\\" + anchor
    else:
        raise _unsupported("unsupported Windows artifact root anchor")
    chain = [_directory(api, handles, None, anchor)]
    for part in path.parts[1:]:
        _validate(api, chain[-1][0], directory=True, identity=chain[-1][1])
        chain.append(_directory(api, handles, chain[-1][0].value, part))
    return chain


def root_identity(root):
    api = _api()
    with _Handles(api) as handles:
        return _root(api, handles, root)[-1][1]


class _PinnedWindowsArtifact:
    def __init__(self, api, path, chain, leaf, name, identity, *, write, read, guard=None):
        self.api, self.path, self.chain, self.leaf = api, path, chain, leaf
        self.name, self.identity, self.write, self.read = name, identity, write, read
        self.parent, self.guard = chain[-1][0], guard

    def validate_parent(self):
        for handle, identity in [*self.chain, *([self.guard] if self.guard else [])]:
            _validate(self.api, handle, directory=True, identity=identity)

    def validate(self):
        self.validate_parent()
        _validate(self.api, self.leaf, directory=False, identity=self.identity,
                  single_link=self.write)

    def write_bytes(self, data):
        if not self.write:
            raise OSError("artifact handle was not opened for writing")
        self.validate()
        self.api.seek_zero(self.leaf)
        self.api.truncate(self.leaf)
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + 1024 * 1024]
            offset += self.api.write(self.leaf, chunk)
        self.validate()

    def read_bytes(self):
        if not self.read:
            raise OSError("artifact handle was not opened for reading")
        self.validate()
        self.api.seek_zero(self.leaf)
        chunks = []
        while chunk := self.api.read(self.leaf, 1024 * 1024):
            chunks.append(chunk)
        self.validate()
        return b"".join(chunks)

    def unlink(self):
        self.validate()
        self.api.disposition(self.leaf)
        self.leaf.close()
        # Generic delete also requires observed absence; GC additionally invokes
        # exists inside its audit boundary before committing completion.
        if self.exists():
            raise OSError("artifact still exists after disposition and close")

    def exists(self):
        self.validate_parent()
        with _Handles(self.api) as probes:
            try:
                self.api.open(probes, self.parent.value, self.name)
            except FileNotFoundError:
                return False
        return True


@contextmanager
def pinned_target(root, artifact_id, expected_root, *, write=False, read=False):
    parts = _components(artifact_id)
    api = _api()
    with _Handles(api) as handles:
        chain = _root(api, handles, root)
        if chain[-1][1] != expected_root:
            raise ValueError("artifact root changed since store construction")
        root_handle = chain[-1][0]
        guard = None
        for index, part in enumerate(parts[:-1]):
            for handle, identity in chain:
                _validate(api, handle, directory=True, identity=identity)
            child = _directory(api, handles, chain[-1][0].value, part, create=write)
            chain.append(child)
            if index == 0:
                # The authoritative lookup follows pinning, including when an
                # earlier caller observed the reserved namespace absent.
                try:
                    guard = _directory(api, handles, root_handle.value, "trace-archives")
                except FileNotFoundError:
                    pass
                if (guard is not None and child[1] == guard[1]
                        or api.basename(child[0]).casefold() == "trace-archives"):
                    raise ValueError("trace-archives is a reserved immutable namespace")
            if guard is not None and child[1] == guard[1]:
                raise ValueError("trace-archives is a reserved immutable namespace")
        # The guard is retained and revalidated without changing the target parent.
        access = (GENERIC_WRITE | READ_ATTRIBUTES if write else
                  GENERIC_READ | DELETE if read else DELETE | READ_ATTRIBUTES)
        leaf = api.open(handles, chain[-1][0].value, parts[-1], directory=False,
                        create=write, access=access, share=0)
        identity = _validate(api, leaf, directory=False, single_link=write)
        target = _PinnedWindowsArtifact(api, root.joinpath(*parts), chain, leaf, parts[-1],
                                        identity, write=write, read=read, guard=guard)
        target.validate()
        yield target

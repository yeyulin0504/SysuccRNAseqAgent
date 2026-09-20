from __future__ import annotations

import ctypes
import os
import stat
import tempfile
from pathlib import Path


def _windows_sid() -> str:
    adv = ctypes.windll.advapi32
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    adv.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
    adv.OpenProcessToken.restype = ctypes.c_int
    token = ctypes.c_void_p()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError("OpenProcessToken failed")
    try:
        size = ctypes.c_ulong(0)
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
            raise OSError("GetTokenInformation failed")
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_ptr = ctypes.c_void_p(sid_ptr)
        out = ctypes.c_wchar_p()
        adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        adv.ConvertSidToStringSidW.restype = ctypes.c_int
        if not adv.ConvertSidToStringSidW(sid_ptr, ctypes.byref(out)):
            raise OSError("ConvertSidToStringSid failed")
        try:
            return out.value
        finally:
            kernel.LocalFree(out)
    finally:
        kernel.CloseHandle(token)


def _windows_descriptor():
    adv = ctypes.windll.advapi32
    sid = _windows_sid()
    sddl = f"D:P(A;;GA;;;{sid})(A;;GA;;;SY)"
    descriptor = ctypes.c_void_p()
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_ulong)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise OSError("security descriptor construction failed")
    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", ctypes.c_ulong), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", ctypes.c_int)]
    attrs = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, 0)
    return attrs, descriptor


def _windows_dacl(descriptor):
    adv = ctypes.windll.advapi32
    adv.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int),
    ]
    adv.GetSecurityDescriptorDacl.restype = ctypes.c_int
    present = ctypes.c_int()
    dacl = ctypes.c_void_p()
    defaulted = ctypes.c_int()
    if not adv.GetSecurityDescriptorDacl(
        descriptor,
        ctypes.byref(present),
        ctypes.byref(dacl),
        ctypes.byref(defaulted),
    ):
        raise OSError("GetSecurityDescriptorDacl failed")
    if not present.value or not dacl.value:
        raise PermissionError("private path has no explicit DACL")
    return dacl


def _read_windows_acl(path: Path) -> tuple[bool, tuple[str, ...]]:
    adv = ctypes.windll.advapi32
    adv.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    adv.GetNamedSecurityInfoW.restype = ctypes.c_ulong
    descriptor = ctypes.c_void_p()
    result = adv.GetNamedSecurityInfoW(
        str(path),
        1,  # SE_FILE_OBJECT
        0x00000004,  # DACL_SECURITY_INFORMATION
        None,
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise OSError(int(result), "GetNamedSecurityInfoW failed")
    try:
        adv.GetSecurityDescriptorControl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ushort),
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        adv.GetSecurityDescriptorControl.restype = ctypes.c_int
        control = ctypes.c_ushort()
        revision = ctypes.c_ubyte()
        if not adv.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise OSError("GetSecurityDescriptorControl failed")
        protected = bool(control.value & 0x1000)  # SE_DACL_PROTECTED

        adv.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_bool),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_bool),
        ]
        adv.GetSecurityDescriptorDacl.restype = ctypes.c_int
        present = ctypes.c_bool()
        dacl = ctypes.c_void_p()
        defaulted = ctypes.c_bool()
        if not adv.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ) or not present.value or not dacl.value:
            raise PermissionError("private path has no explicit DACL")

        class ACL_SIZE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("AceCount", ctypes.c_ulong),
                ("AclBytesInUse", ctypes.c_ulong),
                ("AclBytesFree", ctypes.c_ulong),
            ]

        class ACE_HEADER(ctypes.Structure):
            _fields_ = [
                ("AceType", ctypes.c_ubyte),
                ("AceFlags", ctypes.c_ubyte),
                ("AceSize", ctypes.c_ushort),
            ]

        adv.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_uint]
        adv.GetAclInformation.restype = ctypes.c_int
        adv.GetAce.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
        adv.GetAce.restype = ctypes.c_int
        info = ACL_SIZE_INFORMATION()
        if not adv.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            raise OSError("GetAclInformation failed")
        trustees: list[str] = []
        for index in range(info.AceCount):
            ace = ctypes.c_void_p()
            if not adv.GetAce(dacl, index, ctypes.byref(ace)):
                raise OSError("GetAce failed")
            header = ctypes.cast(ace, ctypes.POINTER(ACE_HEADER)).contents
            if header.AceType != 0 or header.AceFlags & 0x10:  # ACCESS_ALLOWED_ACE / INHERITED_ACE
                raise PermissionError("private path contains a non-explicit allow ACE")
            sid = ctypes.c_void_p(ace.value + 8)  # ACCESS_ALLOWED_ACE.SidStart
            trustee = ctypes.c_wchar_p()
            if not adv.ConvertSidToStringSidW(sid, ctypes.byref(trustee)):
                raise OSError("ConvertSidToStringSidW failed")
            try:
                trustees.append(trustee.value or "")
            finally:
                ctypes.windll.kernel32.LocalFree(trustee)
    finally:
        ctypes.windll.kernel32.LocalFree(descriptor)

    current_sid = _windows_sid()
    allowed = {current_sid, "SY", "S-1-5-18"}
    if not trustees or any(trustee not in allowed for trustee in trustees):
        raise PermissionError("private path DACL contains an unauthorized trustee")
    if set(trustees) not in ({current_sid, "SY"}, {current_sid, "S-1-5-18"}):
        raise PermissionError("private path DACL must allow only current user and LocalSystem")
    return protected, tuple(trustees)


def _apply_windows_private_dacl(path: Path) -> None:
    adv = ctypes.windll.advapi32
    attrs, descriptor = _windows_descriptor()
    try:
        dacl = _windows_dacl(descriptor)
        adv.SetNamedSecurityInfoW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        adv.SetNamedSecurityInfoW.restype = ctypes.c_ulong
        result = adv.SetNamedSecurityInfoW(
            str(path),
            1,
            0x80000004,  # PROTECTED_DACL_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise OSError(int(result), "SetNamedSecurityInfoW failed")
    finally:
        ctypes.windll.kernel32.LocalFree(descriptor)


def _absolute_without_resolving(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _reject_symlink_or_reparse_components(path: Path) -> Path:
    """Check existing components without resolving or following them."""
    path = _absolute_without_resolving(path)
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            # Nothing below a missing component exists yet.
            break
        except NotADirectoryError as exc:
            raise OSError(f"private path component is not a directory: {current}") from exc
        if stat.S_ISLNK(info.st_mode) or (
            os.name == "nt"
            and bool(getattr(info, "st_file_attributes", 0) & 0x400)
        ):
            raise OSError(f"private path contains a symlink or reparse point: {current}")
    return path


def _chmod_no_follow(path: Path, mode: int) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _mkdir_posix_private(path: Path) -> None:
    """Create missing components without following an existing component."""
    path = _reject_symlink_or_reparse_components(path)
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current, 0o700)
            except FileExistsError:
                pass
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise OSError(f"private path component is unsafe: {current}")
            _chmod_no_follow(current, 0o700)
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError(f"private path component is unsafe: {current}")


def _ensure_directory_exists_no_follow(path: Path) -> Path:
    """Ensure a non-private parent exists without following links."""
    path = _reject_symlink_or_reparse_components(path)
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current)
            except FileExistsError:
                pass
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or (
            os.name == "nt" and bool(getattr(info, "st_file_attributes", 0) & 0x400)
        ) or not stat.S_ISDIR(info.st_mode):
            raise OSError(f"directory path is unsafe: {current}")
    return path


def _create_windows_directory(path: Path) -> None:
    """Create missing components with a protected DACL at creation time."""
    path = _reject_symlink_or_reparse_components(path)
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    kernel = ctypes.windll.kernel32
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            attrs, descriptor = _windows_descriptor()
            try:
                if not kernel.CreateDirectoryW(str(current), ctypes.byref(attrs)):
                    error = kernel.GetLastError()
                    if error != 183:  # ERROR_ALREADY_EXISTS
                        raise OSError(f"CreateDirectoryW failed: {error}")
            finally:
                kernel.LocalFree(descriptor)
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            raise OSError(f"private path component is a reparse point: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f"private path component is not a directory: {current}")


def ensure_private_directory(path: Path) -> Path:
    path = _reject_symlink_or_reparse_components(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if os.name == "nt":
            _create_windows_directory(path)
        else:
            _mkdir_posix_private(path)
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError(f"private directory is not a directory: {path}")
    verify_private_path(path)
    return path


def create_private_temp(directory: Path, prefix: str = ".tmp-") -> tuple[int, Path]:
    directory = _ensure_directory_exists_no_follow(directory)
    if os.name != "nt":
        fd, name = tempfile.mkstemp(prefix=prefix, dir=directory)
        os.fchmod(fd, 0o600)
        return fd, Path(name)
    attrs, descriptor = _windows_descriptor()
    try:
        kernel = ctypes.windll.kernel32
        kernel.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel.CreateFileW.restype = ctypes.c_void_p
        for _ in range(100):
            candidate = directory / f"{prefix}{next(tempfile._get_candidate_names())}"
            handle = kernel.CreateFileW(str(candidate), 0xC0000000, 0, ctypes.byref(attrs), 1, 0x80, None)
            if handle != ctypes.c_void_p(-1).value:
                import msvcrt
                return msvcrt.open_osfhandle(handle, os.O_RDWR), candidate
        raise FileExistsError("unable to create private temporary")
    finally:
        ctypes.windll.kernel32.LocalFree(descriptor)


def ensure_private_file(path: Path) -> Path:
    path = _reject_symlink_or_reparse_components(path)
    _ensure_directory_exists_no_follow(path.parent)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or (
            os.name == "nt" and bool(getattr(info, "st_file_attributes", 0) & 0x400)
        ) or not stat.S_ISREG(info.st_mode):
            raise OSError(f"private path is not a regular file: {path}")
        verify_private_path(path)
        return path
    if os.name != "nt":
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        os.write(fd, b"\0")
        os.close(fd)
    else:
        attrs, descriptor = _windows_descriptor()
        try:
            kernel = ctypes.windll.kernel32
            handle = kernel.CreateFileW(
                str(path), 0xC0000000, 0, ctypes.byref(attrs), 1, 0x80, None
            )
            if handle == ctypes.c_void_p(-1).value:
                error = kernel.GetLastError()
                if error != 80:  # ERROR_FILE_EXISTS
                    raise OSError(f"CreateFileW failed: {error}")
            else:
                import msvcrt

                fd = msvcrt.open_osfhandle(handle, os.O_RDWR)
                try:
                    os.write(fd, b"\0")
                finally:
                    os.close(fd)
        finally:
            kernel.LocalFree(descriptor)
    verify_private_path(path)
    return path


def ensure_private_lock_file(path: Path) -> Path:
    """Create or repair only the lock file; never rewrite its parent ACL."""
    path = _reject_symlink_or_reparse_components(path)
    _ensure_directory_exists_no_follow(path.parent)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or (
            os.name == "nt" and bool(getattr(info, "st_file_attributes", 0) & 0x400)
        ) or not stat.S_ISREG(info.st_mode):
            raise OSError(f"private lock path is not a regular file: {path}")
        verify_private_path(path)
        return path
    if os.name != "nt":
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        os.close(fd)
    else:
        attrs, descriptor = _windows_descriptor()
        try:
            kernel = ctypes.windll.kernel32
            handle = kernel.CreateFileW(
                str(path), 0xC0000000, 0, ctypes.byref(attrs), 1, 0x80, None
            )
            if handle == ctypes.c_void_p(-1).value:
                error = kernel.GetLastError()
                if error != 80:  # ERROR_FILE_EXISTS
                    raise OSError(f"CreateFileW failed: {error}")
            else:
                kernel.CloseHandle(handle)
        finally:
            kernel.LocalFree(descriptor)
    verify_private_path(path)
    return path


def _verify_or_apply_windows(path: Path) -> None:
    path = _reject_symlink_or_reparse_components(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise OSError(f"unsafe private path: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
        raise OSError(f"unsafe private path: {path}")
    attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    if attributes == 0xFFFFFFFF or attributes & 0x400:
        raise OSError(f"private path is missing or reparse-point: {path}")
    protected, _ = _read_windows_acl(path)
    if not protected:
        raise PermissionError(f"private path DACL is inheritable: {path}")


def verify_private_path(path: Path) -> None:
    path = _reject_symlink_or_reparse_components(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise OSError(f"unsafe private path: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise OSError(f"unsafe private path: {path}")
    if os.name != "nt":
        mode = info.st_mode
        if stat.S_ISDIR(mode):
            if mode & 0o077:
                raise PermissionError(f"private directory is too permissive: {path}")
        elif stat.S_ISREG(mode) and mode & 0o077:
            raise PermissionError(f"private file is too permissive: {path}")
        elif not stat.S_ISREG(mode):
            raise OSError(f"unsafe private path: {path}")
        return
    _verify_or_apply_windows(path)

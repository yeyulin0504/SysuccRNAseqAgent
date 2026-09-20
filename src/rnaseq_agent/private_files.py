from __future__ import annotations

import ctypes
import os
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


def ensure_private_directory(path: Path) -> Path:
    path = Path(path)
    if os.name == "nt" and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        attrs, descriptor = _windows_descriptor()
        try:
            if not ctypes.windll.kernel32.CreateDirectoryW(str(path), ctypes.byref(attrs)):
                err = ctypes.windll.kernel32.GetLastError()
                if err != 183:
                    raise OSError(f"CreateDirectoryW failed: {err}")
        finally:
            ctypes.windll.kernel32.LocalFree(descriptor)
    else:
        path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        if not path.is_dir():
            raise OSError(f"private directory is not a directory: {path}")
        _apply_windows_private_dacl(path)
        _read_windows_acl(path)
    else:
        os.chmod(path, 0o700)
    return path


def create_private_temp(directory: Path, prefix: str = ".tmp-") -> tuple[int, Path]:
    directory = ensure_private_directory(directory)
    if os.name != "nt":
        fd, name = tempfile.mkstemp(prefix=prefix, dir=directory)
        os.chmod(name, 0o600)
        return fd, Path(name)
    attrs, descriptor = _windows_descriptor()
    try:
        kernel = ctypes.windll.kernel32
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
    path = Path(path)
    ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        if os.name == "nt" and not path.is_symlink():
            _apply_windows_private_dacl(path)
        elif os.name != "nt" and not path.is_symlink():
            os.chmod(path, 0o600)
        verify_private_path(path)
        return path
    if os.name != "nt":
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
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
    if not path.exists() or path.is_symlink():
        raise OSError(f"unsafe private path: {path}")
    attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    if attributes == 0xFFFFFFFF or attributes & 0x400:
        raise OSError(f"private path is missing or reparse-point: {path}")
    protected, _ = _read_windows_acl(path)
    if not protected:
        raise PermissionError(f"private path DACL is inheritable: {path}")


def verify_private_path(path: Path) -> None:
    path = Path(path)
    if not path.exists() or path.is_symlink():
        raise OSError(f"unsafe private path: {path}")
    if os.name != "nt":
        mode = path.stat().st_mode
        if path.is_dir():
            if mode & 0o077:
                raise PermissionError(f"private directory is too permissive: {path}")
        elif mode & 0o077:
            raise PermissionError(f"private file is too permissive: {path}")
        return
    _verify_or_apply_windows(path)

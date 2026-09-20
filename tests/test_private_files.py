from __future__ import annotations

import os
import stat
import ctypes
from pathlib import Path

import pytest

from rnaseq_agent.private_files import (
    create_private_temp,
    ensure_private_directory,
    verify_private_path,
)


def test_private_directory_and_file_modes_on_posix(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX mode bits are not applicable on Windows")
    directory = tmp_path / "private"
    ensure_private_directory(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    handle, path = create_private_temp(directory, prefix="test-")
    try:
        os.write(handle, b"{}")
        os.close(handle)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        verify_private_path(path)
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration test")
def test_windows_private_paths_have_protected_current_user_dacl(tmp_path: Path) -> None:
    adv = ctypes.windll.advapi32
    kernel = ctypes.windll.kernel32

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [("AceCount", ctypes.c_ulong), ("AclBytesInUse", ctypes.c_ulong), ("AclBytesFree", ctypes.c_ulong)]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", ctypes.c_ushort)]

    adv.GetNamedSecurityInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetNamedSecurityInfoW.restype = ctypes.c_ulong
    adv.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ubyte)]
    adv.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_bool), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_bool)]
    adv.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_uint]
    adv.GetAce.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    adv.ConvertSidToStringSidW.restype = ctypes.c_int

    def descriptor(path: Path) -> tuple[bool, list[str]]:
        sd = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        owner = ctypes.c_void_p()
        err = adv.GetNamedSecurityInfoW(str(path), 1, 4, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sd))
        assert err == 0
        try:
            control = ctypes.c_ushort()
            revision = ctypes.c_ubyte()
            assert adv.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision))
            present = ctypes.c_bool()
            defaulted = ctypes.c_bool()
            assert adv.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted))
            info = ACL_SIZE_INFORMATION()
            assert adv.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2)
            sids: list[str] = []
            for index in range(info.AceCount):
                ace = ctypes.c_void_p()
                assert adv.GetAce(dacl, index, ctypes.byref(ace))
                header = ctypes.cast(ace, ctypes.POINTER(ACE_HEADER)).contents
                assert header.AceType == 0
                sid = ctypes.c_void_p(ace.value + 8)
                text = ctypes.c_wchar_p()
                assert adv.ConvertSidToStringSidW(sid, ctypes.byref(text))
                try:
                    sids.append(text.value)
                finally:
                    kernel.LocalFree(text)
            return bool(control.value & 0x1000), sids
        finally:
            kernel.LocalFree(sd)

    directory = tmp_path / "private"
    ensure_private_directory(directory)
    handle, path = create_private_temp(directory, prefix="test-")
    os.close(handle)
    try:
        verify_private_path(directory)
        verify_private_path(path)
        protected, sids = descriptor(path)
        assert protected
        from rnaseq_agent.private_files import _windows_sid
        assert set(sids) == {_windows_sid(), "S-1-5-18"}
        assert not {"S-1-1-0", "S-1-5-32-545", "S-1-5-11"}.intersection(sids)

        # Broaden the DACL after creation; verification must fail closed.
        broad = ctypes.c_void_p()
        assert adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:(A;;GA;;;WD)", 1, ctypes.byref(broad), None
        )
        try:
            broad_dacl = ctypes.c_void_p()
            present = ctypes.c_bool()
            defaulted = ctypes.c_bool()
            assert adv.GetSecurityDescriptorDacl(broad, ctypes.byref(present), ctypes.byref(broad_dacl), ctypes.byref(defaulted))
            adv.SetNamedSecurityInfoW.restype = ctypes.c_ulong
            assert adv.SetNamedSecurityInfoW(str(path), 1, 4, None, None, broad_dacl, None) == 0
        finally:
            kernel.LocalFree(broad)
        with pytest.raises((OSError, PermissionError)):
            verify_private_path(path)
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration test")
def test_windows_rejects_unprotected_inherited_directory(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        verify_private_path(tmp_path)

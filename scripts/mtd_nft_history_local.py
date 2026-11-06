"""Owned holder-lease deletion, never stale-lease reclamation.

Windows deletion uses the validated, exclusively protected HANDLE, not a second
pathname lookup. No PID/age heuristics. Other platforms fail closed for durable
recovery; initial same-call cleanup still requires a reviewed native primitive.
"""
import os
import stat


def identity(info):
    return [info.st_dev,info.st_ino]


def delete_owned(path, expected, token, *, before_delete=None):
    """Delete exactly a previously recorded lease; reject replacements/reparse links.

    Exclusive read/delete access prevents concurrent writers/renamers between the
    handle identity/token check and the delete disposition on that SAME handle.
    Missing means our lease is gone, not permission to remove any replacement.
    """
    if os.name!='nt':
        raise OSError('Owned lease recovery unsupported on this platform')
    import ctypes
    from ctypes import wintypes
    import json
    import msvcrt
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateFileW
    create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    create.restype=wintypes.HANDLE
    close=kernel.CloseHandle
    close.argtypes=[wintypes.HANDLE]; close.restype=wintypes.BOOL
    disposition=kernel.SetFileInformationByHandle
    disposition.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD]
    disposition.restype=wintypes.BOOL
    # GENERIC_READ | DELETE; no sharing. The same handle fences a pacing
    # repair and delete disposition against writes, renames and replacement.
    handle=create(str(path),0x80010000,0,None,3,0x00200000,None)
    if handle==wintypes.HANDLE(-1).value:
        code=ctypes.get_last_error()
        if code in (2,3): return False
        raise ctypes.WinError(code)
    try:
        fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except BaseException:
        close(handle); raise
    try:
        stream=os.fdopen(fd,'rb')
    except BaseException:
        # CRT owns the HANDLE after open_osfhandle; close ONLY its descriptor.
        os.close(fd)
        raise
    with stream:
        info=os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1
                or getattr(info,'st_file_attributes',0)&0x400
                or identity(info)!=expected):
            raise ValueError('Owned lease identity changed; review required')
        raw=stream.read(1025)
        if len(raw)>1024: raise ValueError('Owned lease invalid; review required')
        data=json.loads(raw)
        if not isinstance(data,dict) or data.get('recovery_token')!=token:
            raise ValueError('Owned lease token changed; review required')
        if before_delete is not None: before_delete()
        class Disposition(ctypes.Structure):
            _fields_=[('DeleteFile',ctypes.c_ubyte)]
        value=Disposition(1)
        if not disposition(handle,4,ctypes.byref(value),ctypes.sizeof(value)):
            raise ctypes.WinError(ctypes.get_last_error())
    return True

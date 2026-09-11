"""Read-only process absence checks; never signal Windows processes."""
import os


def process_absent(pid):
    if type(pid) is not int or pid<=0:return None
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.WaitForSingleObject.restype=wintypes.DWORD
        handle=kernel.OpenProcess(0x00100000,False,pid)  # SYNCHRONIZE only
        if not handle:return True if ctypes.get_last_error()==87 else None
        try:
            value=kernel.WaitForSingleObject(handle,0)
            return True if value==0 else False if value==258 else None
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0)
    except ProcessLookupError:return True
    except PermissionError:return None
    except OSError:return None
    return False

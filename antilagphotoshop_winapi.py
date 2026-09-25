"""Explicit Win32 signatures: HANDLE/HWND/HICON must never default to c_int."""
import ctypes as c
from ctypes import wintypes as w
import os


def configure_winapi() -> None:
    if os.name != 'nt':
        return

    def bind(library, name, result, *arguments):
        function = getattr(library, name)
        function.argtypes = list(arguments)
        function.restype = result

    kernel = c.windll.kernel32
    user = c.windll.user32
    bind(kernel, 'CreateMutexW', w.HANDLE, c.c_void_p, w.BOOL, w.LPCWSTR)
    bind(kernel, 'CloseHandle', w.BOOL, w.HANDLE)
    bind(kernel, 'GetModuleHandleW', w.HMODULE, w.LPCWSTR)
    bind(kernel, 'GlobalMemoryStatusEx', w.BOOL, c.c_void_p)
    bind(kernel, 'GetDiskFreeSpaceExW', w.BOOL, w.LPCWSTR, c.POINTER(c.c_ulonglong), c.POINTER(c.c_ulonglong), c.POINTER(c.c_ulonglong))
    bind(user, 'DefWindowProcW', c.c_ssize_t, w.HWND, w.UINT, c.c_size_t, c.c_ssize_t)
    bind(user, 'LoadImageW', w.HANDLE, w.HINSTANCE, w.LPCWSTR, w.UINT, c.c_int, c.c_int, w.UINT)
    bind(user, 'LoadIconW', w.HICON, w.HINSTANCE, c.c_void_p)
    bind(user, 'DestroyIcon', w.BOOL, w.HICON)
    bind(user, 'CreatePopupMenu', w.HMENU)
    bind(user, 'AppendMenuW', w.BOOL, w.HMENU, w.UINT, c.c_size_t, w.LPCWSTR)
    bind(user, 'TrackPopupMenu', w.UINT, w.HMENU, w.UINT, c.c_int, c.c_int, c.c_int, w.HWND, c.POINTER(w.RECT))
    bind(user, 'DestroyMenu', w.BOOL, w.HMENU)
    bind(user, 'GetCursorPos', w.BOOL, c.POINTER(w.POINT))
    bind(user, 'SetForegroundWindow', w.BOOL, w.HWND)
    bind(user, 'MessageBoxW', c.c_int, w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT)
    enum_proc = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    bind(user, 'EnumWindows', w.BOOL, enum_proc, w.LPARAM)
    bind(user, 'GetWindowThreadProcessId', w.DWORD, w.HWND, c.POINTER(w.DWORD))
    bind(user, 'IsWindowVisible', w.BOOL, w.HWND)
    bind(user, 'IsHungAppWindow', w.BOOL, w.HWND)
    bind(user, 'GetClassNameW', c.c_int, w.HWND, w.LPWSTR, c.c_int)
    bind(user, 'ShowWindowAsync', w.BOOL, w.HWND, c.c_int)
    bind(user, 'RedrawWindow', w.BOOL, w.HWND, c.POINTER(w.RECT), w.HRGN, w.UINT)
    bind(user, 'PostMessageW', w.BOOL, w.HWND, w.UINT, c.c_size_t, c.c_ssize_t)
    bind(user, 'DestroyWindow', w.BOOL, w.HWND)
    bind(user, 'UnregisterClassW', w.BOOL, w.LPCWSTR, w.HINSTANCE)
    bind(user, 'PostQuitMessage', None, c.c_int)
    bind(user, 'GetMessageW', w.BOOL, c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
    bind(user, 'TranslateMessage', w.BOOL, c.POINTER(w.MSG))
    bind(user, 'DispatchMessageW', c.c_ssize_t, c.POINTER(w.MSG))
    bind(user, 'RegisterWindowMessageW', w.UINT, w.LPCWSTR)
    bind(user, 'SetTimer', c.c_size_t, w.HWND, c.c_size_t, w.UINT, c.c_void_p)
    bind(user, 'KillTimer', w.BOOL, w.HWND, c.c_size_t)
    bind(c.windll.shell32, 'Shell_NotifyIconW', w.BOOL, w.DWORD, c.c_void_p)

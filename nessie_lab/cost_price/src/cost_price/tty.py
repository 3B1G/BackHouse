"""Reading the owner passphrase from a person, not a pipe. sys.stdin.isatty() alone isn't enough on Windows: the NUL
device is a character device, so `< NUL` passes it. A real console answers GetConsoleMode; NUL and pipes don't.

This only raises the bar for scripts that try to approve on the owner's behalf: the passphrase itself is the gate."""
import getpass
import os
import sys


class NotATerminal(PermissionError):
    pass


def is_console() -> bool:
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    if not os.isatty(fd):
        return False
    if os.name == "nt":
        import ctypes
        import msvcrt
        mode = ctypes.c_uint32()
        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError:
            return False
        return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
    return True


def read_passphrase(prompt="Owner passphrase: ") -> str:
    if not is_console():
        raise NotATerminal("the owner passphrase must be typed at a terminal, not piped or redirected")
    return getpass.getpass(prompt)

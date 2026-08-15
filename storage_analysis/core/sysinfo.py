"""Interrogation du support de stockage : taille de cluster, type de disque.

Tout est en « meilleur effort » : la moindre erreur renvoie None et l'appelant
retombe sur une valeur par défaut raisonnable. Aucun de ces appels n'exige de
droits administrateur.
"""

from __future__ import annotations

import os
from pathlib import Path

_WINDOWS = os.name == "nt"


def _volume_root(path: str | os.PathLike[str]) -> str:
    """Racine du volume contenant `path` (« D:\\ » sous Windows)."""
    drive, _ = os.path.splitdrive(os.path.abspath(os.fspath(path)))
    if drive:
        return drive + os.sep
    return os.sep


def cluster_size(path: str | os.PathLike[str]) -> int | None:
    """Taille d'allocation du système de fichiers, en octets."""
    if _WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            sectors_per_cluster = wintypes.DWORD()
            bytes_per_sector = wintypes.DWORD()
            free_clusters = wintypes.DWORD()
            total_clusters = wintypes.DWORD()

            ok = ctypes.windll.kernel32.GetDiskFreeSpaceW(
                ctypes.c_wchar_p(_volume_root(path)),
                ctypes.byref(sectors_per_cluster),
                ctypes.byref(bytes_per_sector),
                ctypes.byref(free_clusters),
                ctypes.byref(total_clusters),
            )
            if not ok:
                return None
            size = sectors_per_cluster.value * bytes_per_sector.value
            return size or None
        except Exception:
            return None

    try:
        st = os.statvfs(os.fspath(path))
        return int(st.f_frsize) or None
    except Exception:
        return None


def has_seek_penalty(path: str | os.PathLike[str]) -> bool | None:
    """True pour un disque rotatif, False pour un SSD/NVMe, None si indéterminé.

    Sous Windows : IOCTL_STORAGE_QUERY_PROPERTY / StorageDeviceSeekPenaltyProperty.
    Sous Linux : /sys/block/<dev>/queue/rotational.
    """
    if _WINDOWS:
        return _has_seek_penalty_windows(path)
    return _has_seek_penalty_linux(path)


def _has_seek_penalty_windows(path: str | os.PathLike[str]) -> bool | None:
    try:
        import ctypes
        from ctypes import wintypes

        drive, _ = os.path.splitdrive(os.path.abspath(os.fspath(path)))
        if not drive:
            return None

        GENERIC_NONE = 0
        FILE_SHARE_READ_WRITE = 0x00000001 | 0x00000002
        OPEN_EXISTING = 3
        INVALID_HANDLE = ctypes.c_void_p(-1).value
        IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
        STORAGE_DEVICE_SEEK_PENALTY_PROPERTY = 7
        PROPERTY_STANDARD_QUERY = 0

        class STORAGE_PROPERTY_QUERY(ctypes.Structure):
            _fields_ = [
                ("PropertyId", wintypes.DWORD),
                ("QueryType", wintypes.DWORD),
                ("AdditionalParameters", ctypes.c_ubyte * 1),
            ]

        class DEVICE_SEEK_PENALTY_DESCRIPTOR(ctypes.Structure):
            _fields_ = [
                ("Version", wintypes.DWORD),
                ("Size", wintypes.DWORD),
                ("IncursSeekPenalty", ctypes.c_ubyte),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = ctypes.c_void_p

        handle = kernel32.CreateFileW(
            ctypes.c_wchar_p(rf"\\.\{drive}"),
            GENERIC_NONE,  # aucun droit d'accès demandé : pas besoin d'être admin
            FILE_SHARE_READ_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE or handle is None:
            return None

        try:
            query = STORAGE_PROPERTY_QUERY(
                PropertyId=STORAGE_DEVICE_SEEK_PENALTY_PROPERTY,
                QueryType=PROPERTY_STANDARD_QUERY,
            )
            descriptor = DEVICE_SEEK_PENALTY_DESCRIPTOR()
            returned = wintypes.DWORD()

            ok = kernel32.DeviceIoControl(
                ctypes.c_void_p(handle),
                IOCTL_STORAGE_QUERY_PROPERTY,
                ctypes.byref(query),
                ctypes.sizeof(query),
                ctypes.byref(descriptor),
                ctypes.sizeof(descriptor),
                ctypes.byref(returned),
                None,
            )
            if not ok:
                return None
            return bool(descriptor.IncursSeekPenalty)
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        return None


def _has_seek_penalty_linux(path: str | os.PathLike[str]) -> bool | None:
    try:
        dev = os.stat(os.fspath(path)).st_dev
        major, minor = os.major(dev), os.minor(dev)
        link = Path(f"/sys/dev/block/{major}:{minor}")
        if not link.exists():
            return None
        # Remonter du device de partition au device parent.
        block = link.resolve()
        for candidate in (block, block.parent):
            rotational = candidate / "queue" / "rotational"
            if rotational.is_file():
                return rotational.read_text().strip() == "1"
        return None
    except Exception:
        return None


def default_workers(path: str | os.PathLike[str]) -> int:
    """Nombre de threads de scan adapté au support.

    Le scan est I/O-bound et le GIL est relâché pendant les appels système, donc
    paralléliser aide sur SSD/NVMe (2-4x). Sur disque rotatif c'est l'inverse :
    les seeks concurrents effondrent le débit, on reste donc à 1.
    """
    penalty = has_seek_penalty(path)
    if penalty is True:
        return 1
    cpu = os.cpu_count() or 4
    return max(2, min(8, cpu))

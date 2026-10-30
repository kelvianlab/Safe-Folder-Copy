"""Copy one folder into another, verifying every file and never touching the source.

The rules this module keeps, whatever the caller asks for:

* The source is only ever opened for reading.
* Nothing that already exists at the destination is overwritten.
* A file is written under a temporary name and only takes its real name after
  its bytes have been read back and found identical to the source.
* One unreadable file or folder never stops the run, and nothing that could
  not be read or verified is ever counted as copied.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

CHUNK_SIZE = 1024 * 1024
TMP_SUFFIX = ".sfc-tmp"
READ_ATTEMPTS = 3
RETRY_DELAY = 1.0
REPORT_PREFIX = "Laporan Safe-Folder-Copy"

# Windows error codes worth explaining to someone who has never seen one.
_WINDOWS_REASONS = {
    1392: "rusak, Windows sendiri tidak bisa membacanya",
    23: "bagian disk tempat berkas ini rusak (galat CRC)",
    5: "akses ditolak",
    32: "sedang dipakai program lain",
    33: "sebagian berkas dikunci program lain",
    121: "perangkat tidak merespons (kabel USB atau disk bermasalah)",
    1117: "galat perangkat (kabel USB atau disk bermasalah)",
    1167: "perangkat terputus",
    2: "hilang saat proses berjalan",
    3: "hilang saat proses berjalan",
}
_DISK_FULL_WINERRORS = {39, 112}


class ReadFailure(Exception):
    """The source could not be read."""


class WriteFailure(Exception):
    """The destination could not be written."""


class DiskFull(Exception):
    """The destination has no space left."""


@dataclass
class Problem:
    path: str
    reason: str


@dataclass
class Progress:
    phase: str  # "scan" or "copy"
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    current: str = ""


@dataclass
class Result:
    source: Path
    destination: Path
    copied: int = 0
    copied_bytes: int = 0
    already_same: int = 0
    files_total: int = 0
    conflicts: list[Problem] = field(default_factory=list)
    unreadable: list[Problem] = field(default_factory=list)
    failed: list[Problem] = field(default_factory=list)
    skipped_links: list[Problem] = field(default_factory=list)
    cancelled: bool = False
    stopped_reason: str | None = None
    report_path: Path | None = None

    @property
    def complete(self) -> bool:
        """True only when every file in the source is now safely at the destination."""
        return (
            not self.cancelled
            and self.stopped_reason is None
            and not self.unreadable
            and not self.failed
            and not self.conflicts
        )


def friendly_reason(exc: BaseException) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror in _WINDOWS_REASONS:
        return _WINDOWS_REASONS[winerror]
    if isinstance(exc, FileNotFoundError):
        return "hilang saat proses berjalan"
    if isinstance(exc, PermissionError):
        return "akses ditolak"
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__


def _is_disk_full(exc: OSError) -> bool:
    return exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in _DISK_FULL_WINERRORS


def _long(path: Path | str) -> str:
    """Give Windows a path form that is not limited to 260 characters."""
    text = os.path.abspath(str(path))
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _scandir(path: str):
    return os.scandir(path)


def _free_space(path: Path) -> int:
    return shutil.disk_usage(_long(path)).free


def _read_chunks(path: str) -> Iterator[bytes]:
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                return
            yield chunk


def _hash_file(path: str) -> str:
    digest = hashlib.blake2b(digest_size=32)
    for chunk in _read_chunks(path):
        digest.update(chunk)
    return digest.hexdigest()


class _Cancelled(Exception):
    pass


def _write_failure(exc: OSError) -> Exception:
    return DiskFull(exc) if _is_disk_full(exc) else WriteFailure(exc)


class _VerifyFailure(Exception):
    """The copy could not be read back to be checked."""


def _read_back(handle) -> str:
    """Hash what the destination now holds, through the handle that wrote it.

    Closing a freshly written file and opening it again makes antivirus scan
    it first, which measured about 15 ms per file here: half an hour on a
    100,000-file folder. Reading back through the same handle returns the
    same bytes from Windows without that wait.
    """
    handle.seek(0)
    digest = hashlib.blake2b(digest_size=32)
    while True:
        chunk = handle.read(CHUNK_SIZE)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _copy_to_temp(source: str, temp: str, cancel: threading.Event) -> tuple[str, str]:
    """Stream source into temp. Returns (digest of what was read, digest read back).

    Reading problems become ReadFailure and writing problems WriteFailure or
    DiskFull, so the caller can tell a bad source from a bad destination.
    """
    digest = hashlib.blake2b(digest_size=32)
    try:
        out = open(temp, "w+b")
    except OSError as exc:
        raise _write_failure(exc) from exc
    with out:
        try:
            chunks = iter(_read_chunks(source))
        except OSError as exc:
            raise ReadFailure(exc) from exc
        while True:
            if cancel.is_set():
                raise _Cancelled()
            try:
                chunk = next(chunks)
            except StopIteration:
                break
            except OSError as exc:
                raise ReadFailure(exc) from exc
            digest.update(chunk)
            try:
                out.write(chunk)
            except OSError as exc:
                raise _write_failure(exc) from exc
        try:
            out.flush()
            os.fsync(out.fileno())
        except OSError as exc:
            raise _write_failure(exc) from exc
        try:
            written = _read_back(out)
        except OSError as exc:
            raise _VerifyFailure(exc) from exc
    return digest.hexdigest(), written


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def target_for(source: Path, destination: Path) -> Path:
    """Where a copy of *source* lands when the user picks *destination*.

    Same as copy-paste in Explorer: copying D:\\Foto into E:\\ gives E:\\Foto.
    """
    src = Path(os.path.abspath(source))
    name = src.name or "Drive " + src.drive.rstrip(":\\/")
    return Path(os.path.abspath(destination)) / name


def check_paths(source: Path, destination: Path) -> None:
    """Raise ValueError, in words a non-technical user can act on, if the pair is unsafe."""
    source = Path(source)
    destination = Path(destination)
    if not str(source).strip() or not str(destination).strip():
        raise ValueError("Pilih folder sumber dan folder tujuan terlebih dahulu.")
    if not os.path.isdir(_long(source)):
        raise ValueError(f"Folder sumber tidak ditemukan:\n{source}")
    src = Path(os.path.abspath(source))
    dst = Path(os.path.abspath(destination))
    if os.path.normcase(str(src)) == os.path.normcase(str(dst)):
        raise ValueError("Folder sumber dan folder tujuan tidak boleh sama.")
    src_norm = os.path.normcase(str(src)).rstrip("\\/") + os.sep
    if os.path.normcase(str(dst)).startswith(src_norm):
        raise ValueError(
            "Folder tujuan tidak boleh berada di dalam folder sumber, "
            "karena salinannya akan ikut tersalin berulang-ulang."
        )


def scan(
    source: Path,
    result: Result,
    cancel: threading.Event,
    report: Callable[[Progress], None],
) -> tuple[list[tuple[str, int]], list[str]]:
    """Walk the source once, reading only metadata. Returns (files, folders)."""
    files: list[tuple[str, int]] = []
    folders: list[str] = []
    progress = Progress(phase="scan")
    stack: list[tuple[str, str]] = [(_long(source), "")]
    last_report = 0.0

    while stack:
        if cancel.is_set():
            result.cancelled = True
            break
        absolute, relative = stack.pop()
        try:
            entries = list(_scandir(absolute))
        except OSError as exc:
            result.unreadable.append(Problem(relative or ".", "folder " + friendly_reason(exc)))
            continue
        for entry in entries:
            rel = os.path.join(relative, entry.name) if relative else entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                if entry.is_symlink() or (is_dir and _is_link_folder(entry)):
                    result.skipped_links.append(
                        Problem(rel, "pintasan (link), tidak diikuti agar tidak berputar")
                    )
                elif is_dir:
                    folders.append(rel)
                    stack.append((entry.path, rel))
                elif entry.is_file(follow_symlinks=False):
                    size = entry.stat(follow_symlinks=False).st_size
                    files.append((rel, size))
                    progress.files_total += 1
                    progress.bytes_total += size
            except OSError as exc:
                result.unreadable.append(Problem(rel, friendly_reason(exc)))
        now = time.monotonic()
        if now - last_report > 0.1:
            progress.current = relative
            report(progress)
            last_report = now

    report(progress)
    files.sort()
    folders.sort()
    return files, folders


_LINK_REPARSE_TAGS = {0xA0000003, 0xA000000C}  # junction (mount point), symlink


def _is_link_folder(entry: os.DirEntry) -> bool:
    """A junction looks like a plain folder to Python before 3.12; following one can loop.

    Only those two reparse tags count: OneDrive and other cloud folders are
    reparse points too, and they hold real files that must be copied.
    """
    is_junction = getattr(entry, "is_junction", None)
    if is_junction and is_junction():
        return True
    if os.name != "nt":
        return False
    return getattr(os.lstat(entry.path), "st_reparse_tag", 0) in _LINK_REPARSE_TAGS


def _copy_one(source_file: str, dest_file: str, cancel: threading.Event) -> str | None:
    """Copy and verify one file. Returns None on success, or why it was not copied.

    Raises ReadFailure when the source stays unreadable after every attempt and
    DiskFull when the destination runs out of space.
    """
    temp = dest_file + TMP_SUFFIX
    last_read_error: BaseException | None = None
    for attempt in range(READ_ATTEMPTS):
        if cancel.is_set():
            return "dibatalkan"
        try:
            expected, actual = _copy_to_temp(source_file, temp, cancel)
        except _Cancelled:
            _remove_quietly(temp)
            return "dibatalkan"
        except ReadFailure as exc:
            _remove_quietly(temp)
            last_read_error = exc.__cause__ or exc
            if isinstance(last_read_error, FileNotFoundError):
                break
            if attempt + 1 < READ_ATTEMPTS:
                cancel.wait(RETRY_DELAY)
            continue
        except DiskFull:
            _remove_quietly(temp)
            raise
        except WriteFailure as exc:
            _remove_quietly(temp)
            return "tidak bisa ditulis di tujuan: " + friendly_reason(exc.__cause__ or exc)
        except _VerifyFailure as exc:
            _remove_quietly(temp)
            return "salinan tidak bisa dibaca ulang untuk diperiksa: " + friendly_reason(
                exc.__cause__ or exc
            )
        except BaseException:
            _remove_quietly(temp)
            raise

        if actual != expected:
            _remove_quietly(temp)
            if attempt + 1 < READ_ATTEMPTS:
                continue
            return "isi salinan tidak cocok dengan aslinya, berkas tidak disimpan"

        if os.path.exists(dest_file):
            _remove_quietly(temp)
            return "muncul berkas lain dengan nama sama di tujuan saat proses berjalan"
        try:
            os.replace(temp, dest_file)
        except OSError as exc:
            _remove_quietly(temp)
            return "tidak bisa diberi nama akhir di tujuan: " + friendly_reason(exc)
        try:
            shutil.copystat(source_file, dest_file)
        except OSError:
            pass  # the bytes are verified; a lost timestamp is not worth failing the file for
        return None

    raise ReadFailure(last_read_error)


def _same_content(source_file: str, dest_file: str) -> bool:
    if os.path.getsize(source_file) != os.path.getsize(dest_file):
        return False
    return _hash_file(source_file) == _hash_file(dest_file)


def copy_folder(
    source: Path,
    destination: Path,
    cancel: threading.Event | None = None,
    report: Callable[[Progress], None] | None = None,
    write_report: bool = True,
) -> Result:
    """Copy everything readable from *source* into *destination*.

    The source folder's contents land directly inside *destination*, keeping
    their own sub-folders. Raises ValueError before touching anything if the
    two paths are an unsafe pair.
    """
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    check_paths(source, destination)
    cancel = cancel or threading.Event()
    report = report or (lambda progress: None)
    result = Result(source=source, destination=destination)

    files, folders = scan(source, result, cancel, report)
    result.files_total = len(files)

    if not result.cancelled:
        _copy_phase(source, destination, files, folders, result, cancel, report)

    if write_report:
        result.report_path = _write_report(result)
    return result


def _copy_phase(source, destination, files, folders, result, cancel, report) -> None:
    src_root = _long(source)
    dst_root = _long(destination)

    try:
        os.makedirs(dst_root, exist_ok=True)
    except OSError as exc:
        result.stopped_reason = "Folder tujuan tidak bisa dibuat: " + friendly_reason(exc)
        return

    needed = sum(
        size for rel, size in files if not os.path.exists(os.path.join(dst_root, rel))
    )
    try:
        free = _free_space(destination)
    except OSError:
        free = None
    if free is not None and needed > free:
        result.stopped_reason = (
            f"Ruang kosong di tujuan tidak cukup: butuh sekitar {format_size(needed)}, "
            f"tersedia {format_size(free)}. Tidak ada berkas yang disalin."
        )
        return

    for rel in folders:
        try:
            os.makedirs(os.path.join(dst_root, rel), exist_ok=True)
        except OSError as exc:
            result.failed.append(Problem(rel, "folder tidak bisa dibuat: " + friendly_reason(exc)))

    progress = Progress(
        phase="copy",
        files_total=len(files),
        bytes_total=sum(size for _, size in files),
    )
    for rel, size in files:
        if cancel.is_set():
            result.cancelled = True
            break
        progress.current = rel
        report(progress)
        source_file = os.path.join(src_root, rel)
        dest_file = os.path.join(dst_root, rel)

        try:
            os.makedirs(os.path.dirname(dest_file), exist_ok=True)
            if os.path.exists(dest_file):
                try:
                    same = _same_content(source_file, dest_file)
                except OSError as exc:
                    result.unreadable.append(Problem(rel, friendly_reason(exc)))
                else:
                    if same:
                        result.already_same += 1
                    else:
                        result.conflicts.append(
                            Problem(rel, "di tujuan sudah ada berkas bernama sama dengan isi berbeda, dibiarkan")
                        )
            else:
                problem = _copy_one(source_file, dest_file, cancel)
                if problem == "dibatalkan":
                    result.cancelled = True
                    break
                if problem is None:
                    result.copied += 1
                    result.copied_bytes += size
                else:
                    result.failed.append(Problem(rel, problem))
        except ReadFailure as exc:
            result.unreadable.append(Problem(rel, friendly_reason(exc.args[0] or exc)))
        except DiskFull:
            result.stopped_reason = "Ruang di tujuan habis di tengah proses."
            break
        except OSError as exc:
            result.failed.append(Problem(rel, friendly_reason(exc)))

        if not os.path.isdir(src_root):
            result.stopped_reason = "Folder sumber tidak bisa diakses lagi (drive tercabut?)."
            break
        if not os.path.isdir(dst_root):
            result.stopped_reason = "Folder tujuan tidak bisa diakses lagi (drive tercabut?)."
            break

        progress.files_done += 1
        progress.bytes_done += size

    report(progress)


def format_size(size: int) -> str:
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GB"
    return f"{size / 1024**2:.1f} MB"


def report_text(result: Result) -> str:
    if result.stopped_reason:
        status = "BERHENTI: " + result.stopped_reason
    elif result.cancelled:
        status = "DIBATALKAN oleh pengguna"
    elif result.complete:
        status = "SELESAI, semua berkas tersalin dan terverifikasi"
    else:
        status = "SELESAI, tetapi ada berkas yang tidak tersalin (lihat daftar di bawah)"

    lines = [
        REPORT_PREFIX,
        "=" * len(REPORT_PREFIX),
        f"Waktu  : {datetime.now():%d-%m-%Y %H:%M}",
        f"Dari   : {result.source}",
        f"Ke     : {result.destination}",
        f"Status : {status}",
        "",
        f"Disalin dan diverifikasi       : {result.copied} berkas ({format_size(result.copied_bytes)})",
        f"Sudah ada di tujuan, isi sama  : {result.already_same} berkas",
        f"Dibiarkan, di tujuan berbeda   : {len(result.conflicts)} berkas",
        f"Tidak bisa dibaca dari sumber  : {len(result.unreadable)}",
        f"Gagal disalin                  : {len(result.failed)}",
        f"Pintasan yang tidak diikuti    : {len(result.skipped_links)}",
        f"Total berkas di sumber         : {result.files_total}",
        "",
        "Isi folder sumber tidak diubah sama sekali.",
    ]
    for title, problems in (
        ("TIDAK BISA DIBACA (tidak tersalin)", result.unreadable),
        ("GAGAL DISALIN", result.failed),
        ("DIBIARKAN KARENA DI TUJUAN SUDAH ADA BERKAS BERBEDA", result.conflicts),
        ("PINTASAN YANG TIDAK DIIKUTI", result.skipped_links),
    ):
        if problems:
            lines += ["", f"== {title} ==", ""]
            lines += [f"{p.path}\n    -> {p.reason}" for p in problems]
    return "\n".join(lines) + "\n"


def _write_report(result: Result) -> Path | None:
    stamp = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
    target = Path(result.destination) / f"{REPORT_PREFIX} {stamp}.txt"
    try:
        with open(_long(target), "w", encoding="utf-8") as handle:
            handle.write(report_text(result))
    except OSError:
        return None
    return target

"""The rules a file-copying tool must never break, each pinned by a test."""

import errno
import hashlib
import os
import sys
import threading
from pathlib import Path

import pytest

from safe_folder_copy import core
from safe_folder_copy.__main__ import main as cli_main
from safe_folder_copy.core import (
    TMP_SUFFIX,
    copy_folder,
    friendly_reason,
    report_text,
    target_for,
)


def _tree(root: Path) -> dict:
    """File contents by relative path, leaving out the report the tool writes."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.startswith(core.REPORT_PREFIX)
    }


def _fingerprint(root: Path) -> dict[str, tuple]:
    """Everything about the source that copying must leave alone."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.stat()
        content = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        out[p.relative_to(root).as_posix()] = (p.is_dir(), st.st_size, st.st_mtime_ns, content)
    return out


def _temps(root: Path) -> list[Path]:
    return list(root.rglob("*" + TMP_SUFFIX))


@pytest.fixture
def source(tmp_path) -> Path:
    src = tmp_path / "Data Kantor"
    (src / "Laporan" / "2026").mkdir(parents=True)
    (src / "Kosong").mkdir()
    (src / "a.txt").write_text("halo")
    (src / "Laporan" / "b.xlsx").write_bytes(os.urandom(3 * 1024 * 1024 + 17))
    (src / "Laporan" / "2026" / "c.pdf").write_bytes(os.urandom(2048))
    (src / "nol.bin").write_bytes(b"")
    os.utime(src / "a.txt", (1_600_000_000, 1_600_000_000))
    return src


@pytest.fixture
def fast_retry(monkeypatch):
    monkeypatch.setattr(core, "RETRY_DELAY", 0)


def test_copies_everything_and_keeps_the_layout(source, tmp_path):
    dest = tmp_path / "salinan"
    result = copy_folder(source, dest)

    assert result.complete
    assert result.copied == 4
    assert result.files_total == 4
    assert _tree(dest) == _tree(source)
    assert (dest / "Kosong").is_dir(), "empty folders are part of the copy"
    assert not _temps(dest)


def test_report_is_written_and_is_not_mistaken_for_a_copied_file(source, tmp_path):
    dest = tmp_path / "salinan"
    result = copy_folder(source, dest)

    assert result.report_path is not None and result.report_path.exists()
    assert result.report_path.parent == dest
    text = result.report_path.read_text(encoding="utf-8")
    assert "SELESAI, semua berkas tersalin dan terverifikasi" in text


def test_modification_time_is_kept(source, tmp_path):
    dest = tmp_path / "salinan"
    copy_folder(source, dest)
    assert int((dest / "a.txt").stat().st_mtime) == 1_600_000_000


def test_source_is_never_changed(source, tmp_path):
    before = _fingerprint(source)
    copy_folder(source, tmp_path / "salinan")
    copy_folder(source, tmp_path / "salinan")
    assert _fingerprint(source) == before


def test_running_again_skips_what_is_already_there(source, tmp_path):
    dest = tmp_path / "salinan"
    copy_folder(source, dest)
    second = copy_folder(source, dest)

    assert second.copied == 0
    assert second.already_same == 4
    assert second.complete


def test_a_different_file_at_the_destination_is_never_overwritten(source, tmp_path):
    dest = tmp_path / "salinan"
    dest.mkdir()
    (dest / "a.txt").write_text("punya orang lain")

    result = copy_folder(source, dest)

    assert (dest / "a.txt").read_text() == "punya orang lain"
    assert [p.path for p in result.conflicts] == ["a.txt"]
    assert result.copied == 3
    assert not result.complete


def test_an_unreadable_file_is_reported_and_the_rest_still_copied(source, tmp_path, monkeypatch, fast_retry):
    (source / "rusak.bin").write_bytes(b"x" * 100)
    real = core._read_chunks

    def flaky(path):
        if path.endswith("rusak.bin"):
            raise PermissionError(errno.EACCES, "denied")
        return real(path)

    monkeypatch.setattr(core, "_read_chunks", flaky)
    dest = tmp_path / "salinan"
    result = copy_folder(source, dest)

    assert [p.path for p in result.unreadable] == ["rusak.bin"]
    assert result.unreadable[0].reason == "akses ditolak"
    assert not (dest / "rusak.bin").exists(), "a file that was not read must not appear copied"
    assert result.copied == 4
    assert not _temps(dest)
    assert "rusak.bin" in result.report_path.read_text(encoding="utf-8")


def test_an_unreadable_folder_is_reported_and_its_siblings_still_copied(source, tmp_path, monkeypatch):
    (source / "Rusak").mkdir()
    (source / "Rusak" / "x.txt").write_text("x")
    real = core._scandir

    def broken(path):
        if path.rstrip("\\/").endswith("Rusak"):
            raise PermissionError(errno.EACCES, "denied")
        return real(path)

    monkeypatch.setattr(core, "_scandir", broken)
    result = copy_folder(source, tmp_path / "salinan")

    assert [p.path for p in result.unreadable] == ["Rusak"]
    assert result.unreadable[0].reason.startswith("folder ")
    assert result.copied == 4


def test_a_passing_read_error_is_retried(source, tmp_path, monkeypatch, fast_retry):
    real = core._read_chunks
    calls = {"n": 0}

    def once(path):
        if path.endswith("a.txt") and not path.endswith(TMP_SUFFIX) and calls["n"] == 0:
            calls["n"] += 1
            raise OSError(errno.EIO, "usb blip")
        return real(path)

    monkeypatch.setattr(core, "_read_chunks", once)
    result = copy_folder(source, tmp_path / "salinan")

    assert result.complete
    assert calls["n"] == 1


def test_a_copy_that_does_not_verify_is_never_given_its_real_name(source, tmp_path, monkeypatch):
    real = core._hash_file

    def lying(path):
        return "0" * 64 if path.endswith(TMP_SUFFIX) else real(path)

    monkeypatch.setattr(core, "_hash_file", lying)
    dest = tmp_path / "salinan"
    result = copy_folder(source, dest)

    assert result.copied == 0
    assert len(result.failed) == 4
    assert "tidak cocok" in result.failed[0].reason
    assert not (dest / "a.txt").exists()
    assert not _temps(dest)


def test_disk_full_stops_cleanly(source, tmp_path, monkeypatch):
    def full(src, temp, cancel):
        Path(temp).write_bytes(b"partial")
        raise core.DiskFull(OSError(errno.ENOSPC, "No space left on device"))

    monkeypatch.setattr(core, "_copy_to_temp", full)
    dest = tmp_path / "salinan"
    result = copy_folder(source, dest)

    assert result.stopped_reason and "habis" in result.stopped_reason
    assert not _temps(dest)
    assert result.copied == 0


def test_not_enough_space_is_caught_before_anything_is_written(source, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_free_space", lambda path: 10)
    dest = tmp_path / "salinan"
    result = copy_folder(source, dest)

    assert result.stopped_reason and "tidak cukup" in result.stopped_reason
    assert _tree(dest) == {}


def test_cancel_leaves_no_half_written_file(source, tmp_path):
    cancel = threading.Event()

    def report(progress):
        if progress.phase == "copy" and progress.files_done >= 1:
            cancel.set()

    dest = tmp_path / "salinan"
    result = copy_folder(source, dest, cancel, report)

    assert result.cancelled
    assert 1 <= result.copied < 4
    assert not _temps(dest)
    for name, data in _tree(dest).items():
        assert data == (source / name).read_bytes(), "whatever was kept must be complete"


def test_refuses_a_destination_inside_the_source(source):
    with pytest.raises(ValueError, match="di dalam folder sumber"):
        copy_folder(source, source / "salinan")
    assert not (source / "salinan").exists()


def test_refuses_the_same_folder(source):
    with pytest.raises(ValueError, match="tidak boleh sama"):
        copy_folder(source, source)


def test_refuses_a_missing_source(tmp_path):
    with pytest.raises(ValueError, match="tidak ditemukan"):
        copy_folder(tmp_path / "tidak-ada", tmp_path / "salinan")


def test_target_follows_explorer_copy_paste(tmp_path):
    assert target_for(tmp_path / "Foto", tmp_path / "Backup") == tmp_path / "Backup" / "Foto"


@pytest.mark.skipif(os.name != "nt", reason="drive letters are a Windows idea")
def test_target_for_a_whole_drive_gets_a_readable_name():
    assert target_for(Path("E:\\"), Path("D:\\")).name == "Drive E"


def test_windows_corruption_error_is_explained_in_plain_words():
    class Corrupt(OSError):
        winerror = 1392

    assert "rusak" in friendly_reason(Corrupt())


@pytest.mark.skipif(os.name != "nt", reason="the 260-character limit is Windows-only")
def test_paths_longer_than_windows_limit_are_copied(tmp_path):
    src = tmp_path / "sumber"
    deep = src
    for i in range(12):
        deep = deep / f"folder_yang_namanya_cukup_panjang_{i:02d}"
    os.makedirs(core._long(deep))
    target = core._long(deep / "berkas.txt")
    with open(target, "w") as handle:
        handle.write("dalam")
    assert len(str(deep)) > 260

    result = copy_folder(src, tmp_path / "salinan")

    assert result.complete and result.copied == 1


def test_report_lists_what_was_not_copied():
    result = core.Result(source=Path("A"), destination=Path("B"))
    result.unreadable.append(core.Problem("Post/27. Ide godmod", "folder rusak"))
    text = report_text(result)
    assert "TIDAK BISA DIBACA" in text and "Post/27. Ide godmod" in text
    assert "Isi folder sumber tidak diubah sama sekali." in text


def test_windowless_mode_exit_codes(source, tmp_path):
    assert cli_main(["--copy", str(source), str(tmp_path / "salinan")]) == 0
    assert cli_main(["--copy", str(source), str(source / "di-dalam")]) == 2
    assert cli_main(["--copy", str(source)]) == 2

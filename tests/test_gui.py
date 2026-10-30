"""Drives the real window. Skipped where Tk or a display is unavailable."""

import time

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture
def app(monkeypatch):
    from tkinter import messagebox

    shown = []
    monkeypatch.setattr(messagebox, "showwarning", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: shown.append(a))
    try:
        from safe_folder_copy.gui import App

        instance = App()
    except tk.TclError as exc:
        pytest.skip(f"no display available: {exc}")
    instance.shown = shown
    yield instance
    try:
        instance.destroy()
    except tk.TclError:
        pass


def _pump(app, seconds=20.0):
    """Run the event loop until the window itself has shown the result.

    The worker finishing is not enough: the window only notices on its next
    100 ms poll, and the start button comes back once it has.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.update()
        finished = not app.busy and app._outcome is not None
        if finished and str(app.start_button["state"]) == "normal":
            return
        time.sleep(0.02)
    raise AssertionError("the copy never finished")


def test_copy_through_the_window_lands_in_a_folder_named_after_the_source(app, tmp_path):
    src = tmp_path / "Foto Liburan"
    src.mkdir()
    (src / "a.jpg").write_bytes(b"\xff\xd8" * 1000)
    backup = tmp_path / "Backup"
    backup.mkdir()

    app.source.set(str(src))
    app.destination.set(str(backup))
    assert "Foto Liburan" in app.target_text.get()
    app.start_button.invoke()
    _pump(app)

    assert (backup / "Foto Liburan" / "a.jpg").read_bytes() == (src / "a.jpg").read_bytes()
    assert "Selesai" in app.status.get()
    assert str(app.report_button["state"]) == "normal"
    assert str(app.start_button["state"]) == "normal", "the window must be usable again"


def test_picking_the_sources_own_parent_is_refused_before_anything_runs(app, tmp_path):
    src = tmp_path / "Foto"
    src.mkdir()
    app.source.set(str(src))
    app.destination.set(str(tmp_path))
    app.start_button.invoke()
    app.update()

    assert app.shown, "the user must be told why nothing happened"
    assert not app.busy


def test_empty_fields_get_a_hint_not_a_crash(app):
    app.start_button.invoke()
    app.update()
    assert app.shown

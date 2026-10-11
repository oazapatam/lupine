import time

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImageReader, QPalette  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from lupine_desktop import discovery, tray  # noqa: E402
from lupine_desktop.settings import Settings, native_env_path, session_env_path  # noqa: E402


@pytest.fixture(scope="module")
def app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _wait(app, condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        app.processEvents()
        time.sleep(0.01)


def _server(host, gpus=1, client=True):
    return discovery.Server(
        discovery.Endpoint(host),
        name=host.replace(".", "-"),
        gpus=[discovery.Gpu(i, "RTX 4090", 24 << 30, 1 << 30, 5) for i in range(gpus)],
        metrics=True,
        client_available=client,
    )


@pytest.fixture
def make_tray(app, xdg, monkeypatch):
    found = []
    probes = {}
    notes = []
    prepared = []
    monkeypatch.setattr(tray.discovery, "discover", lambda **kwargs: list(found))
    monkeypatch.setattr(tray, "_probe_address", lambda address: probes.get(address))
    monkeypatch.setattr(tray.Tray, "prepare_client", lambda self, purpose: prepared.append(purpose))
    # A session that already loads the client from the runtime directory.
    monkeypatch.setenv("LD_LIBRARY_PATH", str(xdg / "run" / "lupine"))

    def make():
        instance = tray.Tray("/usr/bin/lupine-tray")
        instance.notify = lambda title, text, error=False: notes.append((title, error))
        return instance

    make.found, make.probes, make.notes, make.prepared = found, probes, notes, prepared
    return make


def _menu(instance):
    return [action.text() for action in instance.menu.actions() if action.text()]


@pytest.mark.parametrize("state, die", [("idle", None), ("ok", "#27ae60"), ("error", "#da4453")])
def test_the_icon_outside_plasma_follows_the_palette(app, monkeypatch, state, die):
    if b"svg" not in QImageReader.supportedImageFormats():
        pytest.skip("Qt has no SVG image plugin")
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    image = tray._status_icon(state).pixmap(22, 22).toImage()

    text = app.palette().color(QPalette.ColorRole.WindowText)
    assert image.pixelColor(4, 11) == text  # the package's left edge
    if die:
        assert image.pixelColor(7, 10) == QColor(die)


def test_every_program_gets_the_client_while_the_selection_answers(app, make_tray, xdg):
    make_tray.found.append(_server("10.0.0.3"))
    make_tray.probes["10.0.0.3:14833"] = _server("10.0.0.3")
    instance = make_tray()
    instance.start()
    _wait(app, lambda: instance.scanned_once and not instance.health_running)
    assert ("Log out and back in once", False) not in make_tray.notes
    directory = xdg / "run" / "lupine"
    assert native_env_path().exists()
    assert not session_env_path().exists()
    assert not (directory / "servers").exists()  # no client fetched yet

    instance.native_libdir = "/src/build"
    instance.refresh()
    assert (directory / "servers").read_text() == "10.0.0.3:14833\n"
    assert (directory / "libcuda.so.1").is_symlink()

    instance.offline.add("10.0.0.3:14833")
    instance.refresh()
    assert not (directory / "servers").exists()

    instance.offline.clear()
    instance.settings.session_env = True
    instance.toggle_native(False)
    assert not (directory / "servers").exists()
    assert not native_env_path().exists()
    assert Settings.load().native_gpus is False
    assert session_env_path().exists()  # exported again, now that it is safe


def test_a_session_without_the_client_on_its_path_is_told_to_log_in_again(app, make_tray, monkeypatch):
    monkeypatch.delenv("LD_LIBRARY_PATH")
    instance = make_tray()
    instance.start()

    assert ("Log out and back in once", False) in make_tray.notes


def test_first_scan_picks_the_best_usable_server(app, make_tray):
    make_tray.found += [_server("10.0.0.2", gpus=4, client=False), _server("10.0.0.3", gpus=2)]
    make_tray.probes["10.0.0.3:14833"] = _server("10.0.0.3", gpus=2)
    instance = make_tray()
    instance.start()
    _wait(app, lambda: instance.scanned_once and not instance.health_running)

    assert instance.settings.servers == ["10.0.0.3:14833"]
    assert Settings.load().servers == ["10.0.0.3:14833"]
    assert make_tray.notes[0] == ("Using 10-0-0-3", False)
    assert make_tray.prepared == ["prefetch"]
    menu = _menu(instance)
    assert menu[0] == "Using 10-0-0-3 · 2 GPUs"
    assert "10-0-0-3 — 2× RTX 4090" in menu
    assert "10-0-0-2 — 4× RTX 4090 (no client)" in menu
    assert instance.state() == "ok"


def test_an_unanswering_selection_turns_the_icon_red(app, make_tray):
    Settings(servers=["10.0.0.9:14833"]).save()
    instance = make_tray()
    instance.start()
    _wait(app, lambda: instance.scanned_once and not instance.health_running)

    assert instance.state() == "error"
    assert _menu(instance)[0] == "10.0.0.9:14833 not answering"
    assert ("10.0.0.9:14833 is not answering", True) in make_tray.notes

    make_tray.probes["10.0.0.9:14833"] = _server("10.0.0.9")
    instance.check_health()
    _wait(app, lambda: not instance.health_running)
    assert instance.state() == "ok"
    assert ("10-0-0-9 is back", False) in make_tray.notes


def test_window_checkboxes_select_several_servers(app, make_tray):
    make_tray.found += [_server("10.0.0.2"), _server("10.0.0.3")]
    for host in ("10.0.0.2", "10.0.0.3"):
        make_tray.probes[f"{host}:14833"] = _server(host)
    instance = make_tray()
    instance.start()
    _wait(app, lambda: instance.scanned_once)
    instance.show_window()
    table = instance.window.table

    unchecked = next(
        row for row in range(table.rowCount())
        if table.item(row, 0).checkState() == Qt.CheckState.Unchecked
    )
    table.item(unchecked, 0).setCheckState(Qt.CheckState.Checked)
    _wait(app, lambda: len(instance.settings.servers) == 2 and not instance.health_running)

    assert set(instance.settings.servers) == {"10.0.0.2:14833", "10.0.0.3:14833"}
    assert instance.status_text() in (
        "Using 10-0-0-2, 10-0-0-3 · 2 GPUs",
        "Using 10-0-0-3, 10-0-0-2 · 2 GPUs",
    )


def test_servers_without_metrics_report_gpus_through_nvml(app, make_tray, monkeypatch):
    from test_gpuinfo import REPORT

    queries = []
    monkeypatch.setattr(tray, "client_environment", lambda servers, libdir="": {"LUPINE_SERVER": servers[0]})
    monkeypatch.setattr(
        tray.gpuinfo, "query", lambda env, cuda=False: queries.append((env["LUPINE_SERVER"], cuda)) or REPORT
    )
    Settings(servers=["10.0.0.9:14833"]).save()
    bare = discovery.Server(discovery.Endpoint("10.0.0.9"), name="squad", client_available=False)
    make_tray.probes["10.0.0.9:14833"] = bare
    instance = make_tray()
    instance.settings.libdir = "/src/build"  # the server publishes no client
    instance.start()
    _wait(app, lambda: "10.0.0.9:14833" in instance.inventory)

    assert queries == [("10.0.0.9:14833", True)]
    assert instance.status_text() == "Using squad · 2 GPUs (1 not visible to CUDA)"
    menu = _menu(instance)
    assert "      GPU 0: GeForce RTX 3050 Ti Laptop GPU · 0.5 / 4.0 GiB · 3% · 40 °C · not visible to CUDA" in menu
    assert "      GPU 1: GeForce RTX 5060 Ti · 0.0 / 16.0 GiB · 0% · 48 °C" in menu
    assert "Remote nvidia-smi…" in menu

    # Later health checks keep the inventory, and only the first query asks CUDA.
    instance.check_health()
    _wait(app, lambda: not instance.health_running)
    assert len(instance.known["10.0.0.9:14833"].gpus) == 2
    instance._refresh_inventory(force=True)
    _wait(app, lambda: not instance.inventory_running)
    assert queries[-1] == ("10.0.0.9:14833", False)
    assert instance.known["10.0.0.9:14833"].gpus[0].cuda is False

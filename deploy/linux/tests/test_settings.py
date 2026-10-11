import json
import os
import urllib.error

import pytest

from lupine_desktop import settings
from lupine_desktop.settings import ClientError, Settings, client_environment


def test_settings_round_trip(xdg):
    Settings(servers=["a:1"], manual=["b:2"], networks=["10.0.0.0/24"], session_env=True).save()
    loaded = Settings.load()

    assert loaded.servers == ["a:1"]
    assert loaded.manual == ["b:2"]
    assert loaded.networks == ["10.0.0.0/24"]
    assert loaded.session_env is True


def test_settings_tolerate_damaged_files(xdg):
    path = settings.settings_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"servers": "gpu", "port": 99999, "manual": ["x", 3, " "]}))
    loaded = Settings.load()

    assert loaded.servers == []
    assert loaded.port == 14833
    assert loaded.manual == ["x"]
    path.write_text("{not json")
    assert Settings.load() == Settings()


def test_client_environment_downloads_the_servers_client(xdg, monkeypatch, tmp_path):
    calls = []

    def resolve(servers, required):
        calls.append((servers, required))
        return tmp_path / "bundle", '"sha256:' + "0" * 64 + '"', "linux/amd64", ()

    monkeypatch.setattr(settings._bundles, "resolve", resolve)
    env = client_environment(["a:1", "b:2"], base={"LD_LIBRARY_PATH": "/opt/x"})

    assert calls == [(("a:1", "b:2"), ("libcuda.so.1", "libnvidia-ml.so.1"))]
    assert env["LUPINE_SERVER"] == "a:1,b:2"
    assert env["LUPINE_LIBDIR"] == str(tmp_path / "bundle")
    assert env["LD_LIBRARY_PATH"] == f"{tmp_path / 'bundle'}:/opt/x"
    assert env["TRITON_LIBCUDA_PATH"] == str(tmp_path / "bundle")
    assert env["LUPINE_CLIENT_PLATFORM"] == "linux/amd64"


def test_client_environment_replaces_a_previous_selection(xdg, monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings._bundles,
        "resolve",
        lambda servers, required: (tmp_path / "new", '"sha256:' + "1" * 64 + '"', "linux/amd64", ()),
    )
    old = str(tmp_path / "old")
    env = client_environment(
        ["b:2"],
        base={
            "LUPINE_LIBDIR": old,
            "LUPINE_CLIENT_ETAG": "x",
            "LD_LIBRARY_PATH": f"{old}:/opt/x",
            "TRITON_LIBCUDA_PATH": old,
        },
    )

    assert env["LD_LIBRARY_PATH"] == f"{tmp_path / 'new'}:/opt/x"
    assert env["TRITON_LIBCUDA_PATH"] == str(tmp_path / "new")


def test_client_environment_honours_a_local_build(xdg, monkeypatch):
    monkeypatch.setattr(settings._bundles, "resolve", pytest.fail)

    assert client_environment(["a:1"], base={}, libdir="/src/build")["LUPINE_LIBDIR"] == "/src/build"
    user = client_environment(["a:1"], base={"LUPINE_LIBDIR": "/mine"})
    assert user["LD_LIBRARY_PATH"] == "/mine"


def test_client_environment_explains_servers_without_a_client(xdg, monkeypatch):
    def resolve(servers, required):
        raise urllib.error.HTTPError("http://a:1/", 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(settings._bundles, "resolve", resolve)
    with pytest.raises(ClientError, match="publishes no client"):
        client_environment(["a:1"], base={})
    with pytest.raises(ClientError, match="No LUPINE server"):
        client_environment([], base={})


def test_autostart_user_entry(xdg):
    assert not settings.autostart_enabled()
    settings.set_autostart(True, "/opt/bin/lupine-tray")
    entry = xdg / "config" / "autostart" / "lupine-tray.desktop"

    assert 'Exec="/opt/bin/lupine-tray"' in entry.read_text()
    assert settings.autostart_enabled()
    settings.set_autostart(False, "/opt/bin/lupine-tray")
    assert not entry.exists()
    assert not settings.autostart_enabled()


def test_autostart_overrides_the_system_entry(xdg):
    system = xdg / "etc-xdg" / "autostart" / "lupine-tray.desktop"
    system.parent.mkdir(parents=True)
    system.write_text("[Desktop Entry]\nExec=/usr/bin/lupine-tray\n")
    user = xdg / "config" / "autostart" / "lupine-tray.desktop"

    assert settings.autostart_enabled()
    settings.set_autostart(False, "/usr/bin/lupine-tray")
    assert "Hidden=true" in user.read_text()
    assert not settings.autostart_enabled()
    settings.set_autostart(True, "/usr/bin/lupine-tray")
    assert not user.exists()
    assert settings.autostart_enabled()


def test_session_env_file(xdg, monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "_systemctl", lambda *args: calls.append(args))
    settings.set_session_env(["a:1", "b:2"])
    path = settings.session_env_path()

    assert "LUPINE_SERVER=a:1,b:2\n" in path.read_text()
    settings.set_session_env(None)
    assert not path.exists()
    assert calls == [("set-environment", "LUPINE_SERVER=a:1,b:2"), ("unset-environment", "LUPINE_SERVER")]


def test_native_gpus_is_on_unless_turned_off(xdg):
    assert Settings.load().native_gpus is True
    Settings(native_gpus=False).save()
    assert Settings.load().native_gpus is False


def test_native_environment_puts_the_runtime_directory_first(xdg):
    assert settings.set_native_environment(True)
    text = settings.native_env_path().read_text()

    assert f"LD_LIBRARY_PATH={xdg / 'run' / 'lupine'}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}" in text
    assert "LUPINE_LOG_LEVEL=error" in text
    assert settings.set_native_environment(False)
    assert not settings.native_env_path().exists()


def test_native_needs_a_runtime_directory(xdg, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR")

    assert not settings.set_native_environment(True)
    assert not settings.native_env_path().exists()
    settings.sync_native(["a:1"], "/src/build")  # nowhere to put it; no error


def test_sync_native_exposes_the_client_and_the_selection(xdg):
    directory = xdg / "run" / "lupine"
    settings.sync_native(["a:1", "b:2"], "/src/build")

    assert {p.name: os.readlink(p) for p in directory.iterdir() if p.is_symlink()} == {
        "libcuda.so.1": "/src/build/libcuda.so.1",
        "libnvidia-ml.so.1": "/src/build/libnvidia-ml.so.1",
    }
    assert (directory / "servers").read_text() == "a:1,b:2\n"

    settings.sync_native(["a:1"], "/other/build")
    assert os.readlink(directory / "libcuda.so.1") == "/other/build/libcuda.so.1"
    assert (directory / "servers").read_text() == "a:1\n"

    settings.sync_native([], None)
    assert list(directory.iterdir()) == []

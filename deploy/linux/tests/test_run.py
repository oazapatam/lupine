import shlex

import pytest

from lupine_desktop import discovery, run
from lupine_desktop.settings import Settings


@pytest.fixture
def local_build(xdg):
    Settings(libdir="/src/build").save()


def _exports(text):
    return dict(
        shlex.split(line.removeprefix("export "))[0].split("=", 1) for line in text.splitlines()
    )


def test_env_uses_the_server_option(local_build, capsys):
    assert run.main(["--server", "gpu-a,gpu-b:9000", "--env"]) == 0
    exports = _exports(capsys.readouterr().out)

    assert exports["LUPINE_SERVER"] == "gpu-a:14833,gpu-b:9000"
    assert exports["LD_LIBRARY_PATH"].split(":")[0] == "/src/build"


def test_env_falls_back_to_the_tray_selection(xdg, capsys):
    Settings(servers=["10.0.0.9:14833"], libdir="/src/build").save()

    assert run.main(["--env"]) == 0
    assert _exports(capsys.readouterr().out)["LUPINE_SERVER"] == "10.0.0.9:14833"


def test_without_a_selection_the_best_discovered_server_is_used(local_build, monkeypatch, capsys):
    servers = [
        discovery.Server(discovery.Endpoint("10.0.0.2"), name="small"),
        discovery.Server(
            discovery.Endpoint("10.0.0.3"),
            name="big",
            gpus=[discovery.Gpu(0, "GPU", 10, 1)],
        ),
    ]
    monkeypatch.setattr(run.discovery, "discover", lambda **kwargs: servers)

    assert run.main(["--env"]) == 0
    captured = capsys.readouterr()
    assert _exports(captured.out)["LUPINE_SERVER"] == "10.0.0.3:14833"
    assert "using big" in captured.err


def test_command_is_executed_with_the_client_environment(local_build, monkeypatch):
    calls = []
    monkeypatch.setattr(run.os, "execvpe", lambda *args: calls.append(args))
    monkeypatch.setattr(run.discovery, "unreachable", lambda servers: [])

    run.main(["-s", "gpu-a", "--", "nvidia-smi", "-L"])

    (file, argv, env), = calls
    assert (file, argv) == ("nvidia-smi", ["nvidia-smi", "-L"])
    assert env["LUPINE_SERVER"] == "gpu-a:14833"


def test_discover_prints_a_table(xdg, monkeypatch, capsys):
    server = discovery.Server(
        discovery.Endpoint("10.0.0.3"),
        name="big",
        gpus=[discovery.Gpu(0, "RTX 4090", 24 << 30, 2 << 30, 40)],
        metrics=True,
        clients=2,
    )
    monkeypatch.setattr(run.discovery, "discover", lambda **kwargs: [server])

    assert run.main(["--discover"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["SERVER", "ADDRESS", "GPUS", "MEMORY", "(GiB)", "LOAD", "CLIENTS"]
    assert lines[1].split() == ["big", "10.0.0.3:14833", "RTX", "4090", "2.0", "/", "24.0", "40%", "2"]


def test_an_unanswering_server_fails_at_once(local_build, monkeypatch, capsys):
    monkeypatch.setattr(run.discovery, "probe", lambda *args, **kwargs: None)
    monkeypatch.setattr(run.os, "execvpe", pytest.fail)

    assert run.main(["-s", "gpu-a", "nvidia-smi"]) == 1
    assert "gpu-a:14833 not answering" in capsys.readouterr().err

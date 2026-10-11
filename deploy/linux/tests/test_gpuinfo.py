import json
import subprocess

import pytest

from lupine_desktop import discovery, gpuinfo

REPORT = {
    "gpus": [
        {
            "index": 0,
            "name": "NVIDIA GeForce RTX 3050 Ti Laptop GPU",
            "uuid": "GPU-da642383-2886-0811-3064-301c83b0ff71",
            "memory_total": 4 << 30,
            "memory_used": 1 << 29,
            "utilization": 3,
            "temperature": 40,
        },
        {
            "index": 1,
            "name": "NVIDIA GeForce RTX 5060 Ti",
            "uuid": "GPU-ddc60789-2fde-a5cb-310e-3397ed24b5ba",
            "memory_total": 16 << 30,
            "memory_used": 0,
            "utilization": 0,
            "temperature": 48,
        },
    ],
    # CUDA lists a local GPU and only one of the server's two.
    "cuda": ["GPU-4a2c2b0c-5fa9-f2b7-bf40-af218b04fdc9", "GPU-ddc60789-2fde-a5cb-310e-3397ed24b5ba"],
}


def test_gpus_mark_what_cuda_cannot_see():
    gpus = gpuinfo.gpus(REPORT)

    assert [gpu.cuda for gpu in gpus] == [False, True]
    assert gpus[0].details() == "GeForce RTX 3050 Ti Laptop GPU · 0.5 / 4.0 GiB · 3% · 40 °C · not visible to CUDA"
    server = discovery.Server(discovery.Endpoint("10.0.0.1"), gpus=gpus)
    assert server.gpu_summary() == (
        "NVIDIA GeForce RTX 3050 Ti Laptop GPU (no CUDA), NVIDIA GeForce RTX 5060 Ti"
    )
    assert server.memory() == (1 << 29, 20 << 30)


def test_gpus_without_a_cuda_check_leave_visibility_unknown():
    assert [gpu.cuda for gpu in gpuinfo.gpus({**REPORT, "cuda": None})] == [None, None]


def test_name_suffix_from_the_nvml_shim_is_removed():
    # nvidia-smi's 64-byte buffer cuts the suffix short; both forms go.
    assert gpuinfo._SUFFIX.sub("", "NVIDIA GeForce RTX 3050 Ti Laptop GPU (via lupine 192.168.78.19") == (
        "NVIDIA GeForce RTX 3050 Ti Laptop GPU"
    )
    assert gpuinfo._SUFFIX.sub("", "NVIDIA GeForce RTX 5060 Ti (via lupine gpu:1)") == "NVIDIA GeForce RTX 5060 Ti"


def _completed(stdout, stderr=""):
    return lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=stderr)


def test_query_reads_the_prefixed_line_among_shim_logs(monkeypatch):
    stdout = "LUPINE server at x hosts no arenas\nlupine-gpuinfo: " + json.dumps(REPORT) + "\n"
    monkeypatch.setattr(gpuinfo.subprocess, "run", _completed(stdout))

    assert gpuinfo.query({"LUPINE_LIBDIR": "/x"}, cuda=True) == REPORT


def test_query_asks_for_the_servers_gpus_alone(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, stdout="lupine-gpuinfo: " + json.dumps(REPORT))

    monkeypatch.setattr(gpuinfo.subprocess, "run", run)
    gpuinfo.query({"LUPINE_LIBDIR": "/x"})

    assert calls == [{"LUPINE_LIBDIR": "/x", "LUPINE_LOCAL_GPUS": "0"}]


def test_query_raises_the_reported_error(monkeypatch):
    monkeypatch.setattr(
        gpuinfo.subprocess, "run", _completed('lupine-gpuinfo: {"error": "nvmlInit failed (999)"}\n')
    )
    with pytest.raises(RuntimeError, match="nvmlInit failed"):
        gpuinfo.query({})


def test_query_without_a_report_names_the_last_error_line(monkeypatch):
    monkeypatch.setattr(gpuinfo.subprocess, "run", _completed("", "Traceback\nOSError: no shim\n"))
    with pytest.raises(RuntimeError, match="OSError: no shim"):
        gpuinfo.query({})


def test_script_reports_a_missing_libdir(monkeypatch):
    monkeypatch.delenv("LUPINE_LIBDIR", raising=False)
    result = subprocess.run(
        [__import__("sys").executable, gpuinfo.__file__], capture_output=True, text=True, env={}
    )
    assert result.returncode == 1
    assert json.loads(result.stdout.removeprefix("lupine-gpuinfo: ")) == {"error": "LUPINE_LIBDIR is not set"}

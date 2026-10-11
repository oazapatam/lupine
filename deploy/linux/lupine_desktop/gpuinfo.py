"""Ask LUPINE servers for their GPUs through the client's own shims.

``/metrics`` is the cheap source, but servers built without NVML monitoring
(the Windows server, for one) do not serve it. The NVML shim answers for any
server, as ``nvidia-smi`` does through it, and the CUDA shim tells which of
those GPUs CUDA programs can use: a driver can hide a GPU from CUDA that NVML
still lists.

The shims hold connections to one server set for the life of the process, so
callers run this file as a script, in the client environment
(``settings.client_environment``), and read JSON from its stdout::

    {"gpus": [{"index": 0, "name": ..., "uuid": ..., "memory_total": ...,
               "memory_used": ..., "utilization": ..., "temperature": ...}],
     "cuda": ["GPU-...", ...] | null}

It uses the standard library only.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping

_SUFFIX = re.compile(r"\s*\(via lupine.*$")
# The shims log to the same streams; the report is the line with this prefix.
_PREFIX = "lupine-gpuinfo: "
NVML_TEMPERATURE_GPU = 0


class _Memory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class _Utilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


def _library(libdir: str, name: str) -> ctypes.CDLL:
    # By path, so a real driver library elsewhere on the system cannot answer.
    return ctypes.CDLL(os.path.join(libdir, name))


def nvml_gpus(libdir: str) -> list[dict]:
    nvml = _library(libdir, "libnvidia-ml.so.1")
    status = nvml.nvmlInit_v2()
    if status != 0:
        raise RuntimeError(f"nvmlInit failed ({status})")
    gpus = []
    try:
        count = ctypes.c_uint()
        status = nvml.nvmlDeviceGetCount_v2(ctypes.byref(count))
        if status != 0:
            raise RuntimeError(f"nvmlDeviceGetCount failed ({status})")
        for index in range(count.value):
            handle = ctypes.c_void_p()
            if nvml.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(handle)) != 0:
                continue
            name = ctypes.create_string_buffer(256)
            uuid = ctypes.create_string_buffer(96)
            memory = _Memory()
            utilization = _Utilization()
            temperature = ctypes.c_uint()
            gpu: dict = {"index": index, "name": "GPU", "uuid": ""}
            if nvml.nvmlDeviceGetName(handle, name, len(name)) == 0:
                gpu["name"] = _SUFFIX.sub("", name.value.decode(errors="replace"))
            if nvml.nvmlDeviceGetUUID(handle, uuid, len(uuid)) == 0:
                gpu["uuid"] = uuid.value.decode(errors="replace")
            if nvml.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(memory)) == 0:
                gpu["memory_total"] = memory.total
                gpu["memory_used"] = memory.used
            if nvml.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(utilization)) == 0:
                gpu["utilization"] = utilization.gpu
            if (
                nvml.nvmlDeviceGetTemperature(
                    handle, NVML_TEMPERATURE_GPU, ctypes.byref(temperature)
                )
                == 0
            ):
                gpu["temperature"] = temperature.value
            gpus.append(gpu)
    finally:
        nvml.nvmlShutdown()
    return gpus


def _uuid_text(raw: bytes) -> str:
    h = raw.hex()
    return f"GPU-{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def cuda_uuids(libdir: str) -> list[str]:
    """UUIDs of the GPUs CUDA enumerates, local ones included."""

    cuda = _library(libdir, "libcuda.so.1")
    status = cuda.cuInit(0)
    if status != 0:
        raise RuntimeError(f"cuInit failed ({status})")
    count = ctypes.c_int()
    status = cuda.cuDeviceGetCount(ctypes.byref(count))
    if status != 0:
        raise RuntimeError(f"cuDeviceGetCount failed ({status})")
    uuids = []
    for ordinal in range(count.value):
        device = ctypes.c_int()
        raw = (ctypes.c_ubyte * 16)()
        if cuda.cuDeviceGet(ctypes.byref(device), ordinal) != 0:
            continue
        if cuda.cuDeviceGetUuid_v2(raw, device) == 0:
            uuids.append(_uuid_text(bytes(raw)))
    return uuids


def gpus(report: dict) -> list:
    """``discovery.Gpu`` objects for a report; CUDA visibility when it was asked."""

    # Imported here: this file also runs as a plain script, outside the package.
    from lupine_desktop.discovery import Gpu

    visible = report.get("cuda")
    return [
        Gpu(
            index=item.get("index", position),
            name=item.get("name", "GPU"),
            memory_total=item.get("memory_total"),
            memory_used=item.get("memory_used"),
            utilization=item.get("utilization"),
            uuid=item.get("uuid", ""),
            temperature=item.get("temperature"),
            cuda=None if visible is None else item.get("uuid") in visible,
        )
        for position, item in enumerate(report.get("gpus", []))
    ]


def query(env: Mapping[str, str], cuda: bool = False, timeout: float = 90) -> dict:
    """Run this file in ``env`` and return its report; raises ``RuntimeError``."""

    command = [sys.executable, os.path.abspath(__file__)]
    if cuda:
        command.append("--cuda")
    # The servers' GPUs alone: the client would list this machine's too.
    env = {**env, "LUPINE_LOCAL_GPUS": "0"}
    try:
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"GPU query failed: {exc}") from exc
    lines = [line for line in result.stdout.splitlines() if line.startswith(_PREFIX)]
    try:
        report = json.loads(lines[-1][len(_PREFIX) :])
    except (IndexError, ValueError):
        detail = (result.stderr.strip().splitlines() or ["no output"])[-1]
        raise RuntimeError(f"GPU query failed: {detail}") from None
    if "error" in report:
        raise RuntimeError(report["error"])
    return report


def main(argv: list[str]) -> int:
    libdir = os.environ.get("LUPINE_LIBDIR", "")
    report: dict = {}
    try:
        if not libdir:
            raise RuntimeError("LUPINE_LIBDIR is not set")
        report["gpus"] = nvml_gpus(libdir)
        report["cuda"] = None
        if "--cuda" in argv:
            try:
                report["cuda"] = cuda_uuids(libdir)
            except (OSError, RuntimeError):
                pass  # NVML still answered; CUDA visibility stays unknown
    except (OSError, RuntimeError) as exc:
        report = {"error": str(exc)}
    print(_PREFIX + json.dumps(report), flush=True)
    return 0 if "error" not in report else 1


if __name__ == "__main__":
    # A normal exit lets the shims close their connections in order, as any
    # CUDA program would; a Windows server serves them all in one process.
    sys.exit(main(sys.argv[1:]))

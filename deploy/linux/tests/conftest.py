import socketserver
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parents[2] / "python")]

METRICS = """\
# HELP lupine_monitor_nvml_up Whether NVML is available.
# TYPE lupine_monitor_nvml_up gauge
lupine_monitor_nvml_up 1
# TYPE lupine_server_connection_info gauge
lupine_server_connection_info{client_id="10.0.0.5:laptop",client_address="10.0.0.5",client_hostname="laptop",client_name="python3",client_pid="41",connection_kind="main",host_pid="900",server_pid="12"} 1
lupine_server_connection_info{client_id="10.0.0.5:laptop",client_address="10.0.0.5",client_hostname="laptop",client_name="python3",client_pid="41",connection_kind="bulk",host_pid="901",server_pid="13"} 1
# TYPE lupine_host_gpu_memory_total_bytes gauge
lupine_host_gpu_memory_total_bytes{device_index="0",device_type="NVIDIA GeForce RTX 4090",device_uuid="GPU-a"} 25769803776
lupine_host_gpu_memory_used_bytes{device_index="0",device_type="NVIDIA GeForce RTX 4090",device_uuid="GPU-a"} 1073741824
lupine_host_gpu_utilization_percent{device_index="0",device_type="NVIDIA GeForce RTX 4090",device_uuid="GPU-a"} 30
lupine_host_gpu_memory_total_bytes{device_index="1",device_type="NVIDIA \\"Quoted\\" GPU",device_uuid="GPU-b"} 25769803776
lupine_host_gpu_memory_used_bytes{device_index="1",device_type="NVIDIA \\"Quoted\\" GPU",device_uuid="GPU-b"} 0
lupine_host_gpu_utilization_percent{device_index="1",device_type="NVIDIA \\"Quoted\\" GPU",device_uuid="GPU-b"} 10
"""


class FakeLupine(socketserver.ThreadingTCPServer):
    """Answers HTTP/1.1 the way dispatch.cpp does."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, body=b"lupine\n", metrics=METRICS, client_status=200):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.body = body
        self.metrics = metrics
        self.client_status = client_status
        self.requests = []


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk
        method, path, _ = data.split(b"\r\n", 1)[0].decode().split(" ", 2)
        server = self.server
        server.requests.append((method, path))
        if path == "/":
            status, body = 200, server.body
        elif path == "/metrics" and server.metrics is not None:
            status, body = 200, server.metrics.encode()
        elif path.startswith("/.well-known/lupine/client/v1/"):
            status, body = server.client_status, b""
        else:
            status, body = 404, b"not found\n"
        head = (
            f"HTTP/1.1 {status} X\r\nx-lupine-cuda-version: 13.3.1\r\n"
            f"content-length: {len(body)}\r\nconnection: close\r\n\r\n"
        ).encode()
        self.request.sendall(head + (b"" if method == "HEAD" else body))


@pytest.fixture
def fake_server():
    servers = []

    def start(**kwargs):
        server = FakeLupine(**kwargs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "run").mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    # Never the real user manager's environment.
    from lupine_desktop import settings

    monkeypatch.setattr(settings, "_systemctl", lambda *args: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path / "etc-xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("LUPINE_SERVER", raising=False)
    monkeypatch.delenv("LUPINE_LIBDIR", raising=False)
    monkeypatch.delenv("LUPINE_CLIENT_ETAG", raising=False)
    return tmp_path

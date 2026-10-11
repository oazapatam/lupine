"""Find LUPINE servers on the network without knowing their addresses.

A server answers plain HTTP/1.1 on its RPC port (see dispatch.cpp): ``GET /``
returns ``lupine`` with an ``x-lupine-cuda-version`` header, and ``GET
/metrics`` lists the host GPUs when the server was built with NVML. Discovery
therefore needs nothing on the server side. Candidates come from:

* every host of each local IPv4 network (container and virtual bridges are
  skipped; a network larger than ``MAX_SWEEP_HOSTS`` is reduced to the /24
  around this machine's address), plus networks named explicitly;
* the kernel neighbour table, which reaches beyond that /24;
* ``_lupine._tcp`` mDNS services, when ``avahi-browse`` is installed;
* online Tailscale peers, when ``tailscale`` is installed;
* endpoints the user added by hand.

Each candidate is probed on the LUPINE port, and only hosts that answer like a
LUPINE server are reported. The module uses the standard library only.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import re
import shutil
import socket
import ssl
import subprocess
import platform
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PORT = 14833
# Networks with more hosts than this are swept only around our own address.
MAX_SWEEP_HOSTS = 1024
MDNS_SERVICE = "_lupine._tcp"
CLIENT_ENDPOINT = "/.well-known/lupine/client/v1/linux/"
_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}

# Interfaces that only reach containers, VMs on this host, or nothing at all.
_SKIPPED_INTERFACES = (
    "lo",
    "docker",
    "br-",
    "veth",
    "virbr",
    "vnet",
    "cni",
    "flannel",
    "cali",
    "podman",
    "lxc",
    "kube",
    "tailscale",
)

_SAMPLE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')
_GPU_METRICS = {
    "lupine_host_gpu_memory_total_bytes": "memory_total",
    "lupine_host_gpu_memory_used_bytes": "memory_used",
    "lupine_host_gpu_utilization_percent": "utilization",
}


@dataclass(frozen=True)
class Endpoint:
    """A server address in the form ``LUPINE_SERVER`` accepts."""

    host: str
    port: int = DEFAULT_PORT
    tls: bool = False

    @classmethod
    def parse(cls, value: str, default_port: int = DEFAULT_PORT) -> Endpoint:
        text = value.strip()
        if not text:
            raise ValueError("empty server address")
        if "://" in text:
            parts = urlsplit(text)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"invalid server address: {value!r}")
            tls = parts.scheme == "https"
            return cls(parts.hostname, parts.port or (443 if tls else default_port), tls)
        if text.count(":") > 1 and not text.startswith("["):
            text = f"[{text}]"  # bare IPv6 address
        parts = urlsplit("//" + text)
        if not parts.hostname:
            raise ValueError(f"invalid server address: {value!r}")
        return cls(parts.hostname, parts.port or default_port)

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"https://{host}:{self.port}" if self.tls else f"{host}:{self.port}"


@dataclass
class Gpu:
    index: int
    name: str
    memory_total: int | None = None
    memory_used: int | None = None
    utilization: int | None = None
    uuid: str = ""
    temperature: int | None = None
    # False when the server's CUDA driver does not enumerate a GPU that NVML
    # lists, so CUDA programs cannot use it. None when not checked.
    cuda: bool | None = None

    @property
    def short_name(self) -> str:
        return self.name.removeprefix("NVIDIA ")

    def details(self) -> str:
        parts = [self.short_name]
        if self.memory_total is not None:
            used = (self.memory_used or 0) / (1 << 30)
            parts.append(f"{used:.1f} / {self.memory_total / (1 << 30):.1f} GiB")
        if self.utilization is not None:
            parts.append(f"{self.utilization}%")
        if self.temperature is not None:
            parts.append(f"{self.temperature} °C")
        if self.cuda is False:
            parts.append("not visible to CUDA")
        return " · ".join(parts)


@dataclass
class Server:
    endpoint: Endpoint
    name: str = ""
    cuda_version: str = ""
    gpus: list[Gpu] = field(default_factory=list)
    clients: int = 0
    # Whether /metrics answered; servers built without NVML do not serve it.
    metrics: bool = False
    # Whether the server publishes a client for this machine; development
    # builds without embedded clients answer 503. None when not asked.
    client_available: bool | None = None
    latency_ms: float = 0.0

    @property
    def address(self) -> str:
        return str(self.endpoint)

    @property
    def label(self) -> str:
        return self.name or self.endpoint.host

    def gpu_summary(self) -> str:
        if not self.gpus:
            return "no GPUs reported" if self.metrics else "GPU details unavailable"
        counts = Counter(
            gpu.name + (" (no CUDA)" if gpu.cuda is False else "") for gpu in self.gpus
        )
        return ", ".join(
            name if count == 1 else f"{count}× {name}" for name, count in counts.items()
        )

    def memory(self) -> tuple[int, int] | None:
        """Used and total bytes over every GPU that reports memory."""

        known = [gpu for gpu in self.gpus if gpu.memory_total is not None]
        if not known:
            return None
        return (
            sum(gpu.memory_used or 0 for gpu in known),
            sum(gpu.memory_total or 0 for gpu in known),
        )

    def utilization(self) -> int | None:
        values = [gpu.utilization for gpu in self.gpus if gpu.utilization is not None]
        return round(sum(values) / len(values)) if values else None


def best(servers: Sequence[Server]) -> Server:
    """The server to pick when the user has not chosen one: most free memory."""

    def free(server: Server) -> int:
        memory = server.memory()
        return 0 if memory is None else memory[1] - memory[0]

    return max(servers, key=lambda s: (len(s.gpus), free(s), -s.latency_ms))


def _dechunk(body: bytes) -> bytes:
    out = bytearray()
    while body:
        size_line, _, rest = body.partition(b"\r\n")
        size = int(size_line.split(b";", 1)[0], 16)
        if size == 0:
            break
        out += rest[:size]
        body = rest[size + 2 :]
    return bytes(out)


def http_get(
    endpoint: Endpoint,
    path: str,
    timeout: float = 2.0,
    connect_timeout: float | None = None,
    limit: int = 1 << 20,
    method: str = "GET",
) -> tuple[int, dict[str, str], bytes]:
    """One HTTP/1.1 request with ``Connection: close``; raises ``OSError``."""

    sock: socket.socket = socket.create_connection(
        (endpoint.host, endpoint.port), timeout=connect_timeout or timeout
    )
    data = bytearray()
    try:
        sock.settimeout(timeout)
        if endpoint.tls:
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=endpoint.host
            )
        host = f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
        sock.sendall(
            f"{method} {path} HTTP/1.1\r\nHost: {host}:{endpoint.port}\r\n"
            "User-Agent: lupine-desktop\r\nAccept: */*\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        while len(data) <= limit:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()

    head, separator, body = bytes(data).partition(b"\r\n\r\n")
    if not separator:
        raise OSError("incomplete HTTP response")
    lines = head.decode("latin-1").split("\r\n")
    status = lines[0].split(" ", 2)
    if len(status) < 2 or not status[0].startswith("HTTP/") or not status[1].isdigit():
        raise OSError("not an HTTP response")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _dechunk(body)
    elif headers.get("content-length", "").isdigit():
        body = body[: int(headers["content-length"])]
    return int(status[1]), headers, body


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), value)


def parse_metrics(text: str) -> tuple[list[Gpu], int]:
    """GPUs and the number of distinct connected clients from ``/metrics``."""

    gpus: dict[str, Gpu] = {}
    clients: set[str] = set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if match is None:
            continue
        name, label_text, value = match.groups()
        labels = {key: _unescape(raw) for key, raw in _LABEL.findall(label_text or "")}
        if name == "lupine_server_connection_info":
            clients.add(labels.get("client_id") or labels.get("server_pid", ""))
            continue
        attribute = _GPU_METRICS.get(name)
        if attribute is None:
            continue
        try:
            number = int(float(value))
            index = int(labels.get("device_index", len(gpus)))
        except ValueError:
            continue
        key = labels.get("device_uuid") or str(index)
        gpu = gpus.setdefault(key, Gpu(index, labels.get("device_type", "GPU")))
        setattr(gpu, attribute, number)
    return sorted(gpus.values(), key=lambda gpu: gpu.index), len(clients)


def probe(
    endpoint: Endpoint,
    timeout: float = 2.0,
    connect_timeout: float = 0.8,
    details: bool = True,
    details_timeout: float = 6.0,
) -> Server | None:
    """Return the server at ``endpoint`` if it answers like LUPINE.

    ``details`` also reads ``/metrics`` and asks whether a client is
    published for this machine. The first NVML query after a GPU wakes from
    a low-power state can take seconds, hence the longer ``details_timeout``.
    """

    start = time.monotonic()
    try:
        status, headers, body = http_get(endpoint, "/", timeout, connect_timeout)
    except (OSError, ValueError):
        return None
    if status != 200 or body.strip() != b"lupine":
        return None
    server = Server(
        endpoint,
        cuda_version=headers.get("x-lupine-cuda-version", ""),
        latency_ms=(time.monotonic() - start) * 1000,
    )
    if not details:
        return server
    try:
        status, _, body = http_get(endpoint, "/metrics", details_timeout)
        if status == 200:
            server.gpus, server.clients = parse_metrics(body.decode("utf-8", "replace"))
            server.metrics = True
    except (OSError, ValueError):
        pass
    arch = _ARCH.get(platform.machine().lower())
    if arch is not None:
        try:
            status, _, _ = http_get(
                endpoint, CLIENT_ENDPOINT + arch, timeout, method="HEAD"
            )
            server.client_available = status == 200
        except (OSError, ValueError):
            pass
    return server


def unreachable(addresses: Sequence[str], timeout: float = 4.0) -> list[str]:
    """The addresses that do not answer like a LUPINE server, in order.

    The shims retry a dead server for a long while before failing, so callers
    check first and say so at once.
    """

    def answers(address: str) -> bool:
        try:
            endpoint = Endpoint.parse(address)
        except ValueError:
            return False
        return probe(endpoint, timeout, connect_timeout=timeout, details=False) is not None

    if not addresses:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(addresses))) as pool:
        results = list(pool.map(answers, addresses))
    return [address for address, ok in zip(addresses, results) if not ok]


def _primary_address() -> str | None:
    # Connecting a UDP socket sends nothing; it only selects a route.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 9))
            return sock.getsockname()[0]
        except OSError:
            return None


def local_interfaces() -> list[tuple[str, ipaddress.IPv4Interface]]:
    """IPv4 addresses of the interfaces that reach other machines."""

    result: list[tuple[str, ipaddress.IPv4Interface]] = []
    ip = shutil.which("ip")
    if ip:
        try:
            output = subprocess.run(
                [ip, "-j", "-4", "addr", "show", "up"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout
            for link in json.loads(output):
                name = link.get("ifname", "")
                flags = link.get("flags", [])
                if (
                    "LOOPBACK" in flags
                    or "NO-CARRIER" in flags
                    or name.startswith(_SKIPPED_INTERFACES)
                ):
                    continue
                for address in link.get("addr_info", []):
                    if address.get("family") == "inet" and address.get("scope") != "host":
                        result.append(
                            (
                                name,
                                ipaddress.IPv4Interface(
                                    f"{address['local']}/{address['prefixlen']}"
                                ),
                            )
                        )
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            result = []
    if not result:
        primary = _primary_address()
        if primary and not primary.startswith("127."):
            result.append(("default", ipaddress.IPv4Interface(f"{primary}/24")))
    return result


def sweep_hosts(
    interfaces: Iterable[tuple[str, ipaddress.IPv4Interface]],
    networks: Iterable[ipaddress.IPv4Network] = (),
) -> list[str]:
    """Every host address to probe, in order and without duplicates."""

    selected: list[ipaddress.IPv4Network] = []
    for _, interface in interfaces:
        network = interface.network
        if network.num_addresses - 2 > MAX_SWEEP_HOSTS:
            network = ipaddress.IPv4Network(f"{interface.ip}/24", strict=False)
        selected.append(network)
    selected.extend(networks)

    hosts: dict[str, None] = {}
    for network in selected:
        if network.prefixlen >= 31:
            continue  # point-to-point; the peer is not ours to sweep
        for host in network.hosts():
            hosts.setdefault(str(host))
    return list(hosts)


def neighbor_hosts(arp_table: Path = Path("/proc/net/arp")) -> list[str]:
    """Addresses the kernel has resolved recently on physical interfaces."""

    try:
        lines = arp_table.read_text().splitlines()[1:]
    except OSError:
        return []
    hosts = []
    for line in lines:
        fields = line.split()
        # IP, HW type, flags, HW address, mask, device; flag 0x2 = complete.
        if len(fields) >= 6 and int(fields[2], 16) & 0x2:
            if not fields[5].startswith(_SKIPPED_INTERFACES):
                hosts.append(fields[0])
    return hosts


def _avahi_unescape(value: str) -> str:
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), value)


def mdns_services(timeout: float = 4.0) -> list[tuple[Endpoint, str]]:
    """``_lupine._tcp`` services announced over mDNS, with their host names."""

    browse = shutil.which("avahi-browse")
    if not browse:
        return []
    command = [browse, "--resolve", "--parsable", "--terminate", "--no-db-lookup"]
    try:
        output = subprocess.run(
            [*command, MDNS_SERVICE], capture_output=True, timeout=timeout
        ).stdout
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
    except OSError:
        return []
    found = []
    for line in output.decode("utf-8", "replace").splitlines():
        fields = line.split(";")
        # =;iface;IPv4;name;type;domain;hostname;address;port;txt
        if len(fields) < 9 or fields[0] != "=" or fields[2] != "IPv4":
            continue
        try:
            endpoint = Endpoint(fields[7], int(fields[8]))
        except ValueError:
            continue
        hostname = fields[6].removesuffix(".local") or _avahi_unescape(fields[3])
        found.append((endpoint, hostname))
    return found


def tailscale_peers(timeout: float = 4.0) -> list[tuple[str, str]]:
    """IPv4 addresses and host names of online Tailscale peers."""

    tailscale = shutil.which("tailscale")
    if not tailscale:
        return []
    try:
        output = subprocess.run(
            [tailscale, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        ).stdout
        peers = (json.loads(output or "{}").get("Peer") or {}).values()
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return []
    found = []
    for peer in peers:
        if not isinstance(peer, dict) or not peer.get("Online"):
            continue
        for address in peer.get("TailscaleIPs") or []:
            if "." in address:
                found.append((address, peer.get("HostName", "")))
    return found


def _reverse_name(host: str) -> str:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host  # already a name
    try:
        name = socket.gethostbyaddr(host)[0]
    except OSError:
        return ""
    return name.split(".", 1)[0]


def discover(
    port: int = DEFAULT_PORT,
    extra: Iterable[str] = (),
    networks: Iterable[str] = (),
    sweep: bool = True,
    timeout: float = 2.0,
    connect_timeout: float = 0.8,
    workers: int = 128,
    progress: Callable[[int, int], None] | None = None,
) -> list[Server]:
    """Probe every candidate and return the LUPINE servers, best first.

    ``extra`` endpoints are always probed; ``networks`` (CIDR strings) are
    swept in addition to the local ones. ``sweep=False`` probes only
    ``extra``. ``progress`` receives (probed, total) as probes finish.
    """

    names: dict[Endpoint, str] = {}
    candidates: dict[Endpoint, None] = {}
    for value in extra:
        try:
            candidates.setdefault(Endpoint.parse(value, port))
        except ValueError:
            continue

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        lookups = []
        if sweep:
            lookups = [pool.submit(mdns_services), pool.submit(tailscale_peers)]
            extra_networks = []
            for value in networks:
                try:
                    extra_networks.append(ipaddress.IPv4Network(value, strict=False))
                except ValueError:
                    continue
            for host in sweep_hosts(local_interfaces(), extra_networks):
                candidates.setdefault(Endpoint(host, port))
            for host in neighbor_hosts():
                candidates.setdefault(Endpoint(host, port))

        found: dict[Endpoint, Server] = {}
        done = 0
        total = len(candidates)
        pending: dict[concurrent.futures.Future, Endpoint] = {}

        def submit(endpoint: Endpoint) -> None:
            pending[pool.submit(probe, endpoint, timeout, connect_timeout)] = endpoint

        for endpoint in candidates:
            submit(endpoint)
        while pending or lookups:
            ready, _ = concurrent.futures.wait(
                [*pending, *lookups], return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in ready:
                if future in lookups:
                    lookups.remove(future)
                    for item in future.result():
                        if isinstance(item[0], Endpoint):
                            endpoint, name = item
                        else:
                            endpoint, name = Endpoint(item[0], port), item[1]
                        if name:
                            names.setdefault(endpoint, name)
                        if endpoint not in candidates:
                            candidates[endpoint] = None
                            total += 1
                            submit(endpoint)
                    continue
                endpoint = pending.pop(future)
                server = future.result()
                if server is not None:
                    found[endpoint] = server
                done += 1
                if progress is not None:
                    progress(done, total)

        unnamed = [e for e in found if e not in names]
        for endpoint, name in zip(unnamed, pool.map(_reverse_name, (e.host for e in unnamed))):
            names[endpoint] = name

    servers = list(found.values())
    for server in servers:
        server.name = names.get(server.endpoint, "")
    servers.sort(key=lambda s: (-len(s.gpus), s.label, s.address))
    return servers

import ipaddress
import subprocess

import pytest

from lupine_desktop import discovery
from lupine_desktop.discovery import Endpoint


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gpu-host", "gpu-host:14833"),
        ("10.0.0.7:9000", "10.0.0.7:9000"),
        ("http://gpu-host", "gpu-host:14833"),
        ("https://gpu.example.com", "https://gpu.example.com:443"),
        ("fe80::1", "[fe80::1]:14833"),
        ("[fe80::1]:99", "[fe80::1]:99"),
    ],
)
def test_endpoint_round_trips_to_lupine_server_form(text, expected):
    assert str(Endpoint.parse(text)) == expected


@pytest.mark.parametrize("text", ["", "  ", "ftp://host", "host:notaport"])
def test_endpoint_rejects_invalid_addresses(text):
    with pytest.raises(ValueError):
        Endpoint.parse(text)


def test_parse_metrics_reads_gpus_and_distinct_clients():
    from conftest import METRICS

    gpus, clients = discovery.parse_metrics(METRICS)

    assert [gpu.name for gpu in gpus] == ["NVIDIA GeForce RTX 4090", 'NVIDIA "Quoted" GPU']
    assert gpus[0].memory_total == 24 << 30
    assert gpus[0].memory_used == 1 << 30
    assert gpus[0].utilization == 30
    # Two connections of one client process count once.
    assert clients == 1


def test_probe_reports_a_lupine_server(fake_server):
    server = fake_server()
    found = discovery.probe(Endpoint("127.0.0.1", server.server_address[1]))

    assert found is not None
    assert found.cuda_version == "13.3.1"
    assert found.gpu_summary() == 'NVIDIA GeForce RTX 4090, NVIDIA "Quoted" GPU'
    assert found.memory() == (1 << 30, 48 << 30)
    assert found.utilization() == 20
    assert found.client_available is True
    assert ("HEAD", "/.well-known/lupine/client/v1/linux/amd64") in server.requests or (
        "HEAD",
        "/.well-known/lupine/client/v1/linux/arm64",
    ) in server.requests


def test_probe_without_metrics_or_published_client(fake_server):
    server = fake_server(metrics=None, client_status=503)
    found = discovery.probe(Endpoint("127.0.0.1", server.server_address[1]))

    assert found is not None
    assert not found.metrics
    assert found.gpu_summary() == "GPU details unavailable"
    assert found.client_available is False


def test_probe_ignores_other_http_services(fake_server):
    server = fake_server(body=b"hello\n")
    assert discovery.probe(Endpoint("127.0.0.1", server.server_address[1])) is None


def test_probe_ignores_closed_ports():
    assert discovery.probe(Endpoint("127.0.0.1", 1), connect_timeout=0.2) is None


def test_sweep_limits_large_networks_to_the_local_slash_24():
    interfaces = [
        ("wlan0", ipaddress.IPv4Interface("192.168.1.20/24")),
        ("eth0", ipaddress.IPv4Interface("10.4.7.9/16")),
        ("wg0", ipaddress.IPv4Interface("100.64.0.1/32")),
    ]
    hosts = discovery.sweep_hosts(interfaces, [ipaddress.IPv4Network("172.20.0.0/30")])

    assert len(hosts) == 254 + 254 + 2
    assert "192.168.1.1" in hosts and "192.168.1.254" in hosts
    assert "10.4.7.1" in hosts and "10.4.8.1" not in hosts
    assert hosts[-2:] == ["172.20.0.1", "172.20.0.2"]


def test_neighbor_hosts_skips_incomplete_and_container_entries(tmp_path):
    table = tmp_path / "arp"
    table.write_text(
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.1.1      0x1         0x2         aa:bb:cc:dd:ee:ff     *        wlan0\n"
        "192.168.1.77     0x1         0x0         00:00:00:00:00:00     *        wlan0\n"
        "172.17.0.2       0x1         0x2         02:42:ac:11:00:02     *        docker0\n"
    )
    assert discovery.neighbor_hosts(table) == ["192.168.1.1"]


def test_mdns_services_parses_avahi_output(monkeypatch):
    output = (
        "+;wlan0;IPv4;LUPINE\\032on\\032gpu-a;_lupine._tcp;local\n"
        "=;wlan0;IPv4;LUPINE\\032on\\032gpu-a;_lupine._tcp;local;gpu-a.local;192.168.1.30;14833;\n"
        "=;wlan0;IPv6;LUPINE\\032on\\032gpu-a;_lupine._tcp;local;gpu-a.local;fe80::1;14833;\n"
    ).encode()
    monkeypatch.setattr(discovery.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        discovery.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=output),
    )
    assert discovery.mdns_services() == [(Endpoint("192.168.1.30", 14833), "gpu-a")]


def test_discover_probes_extra_endpoints_and_sorts_by_gpu_count(fake_server):
    rich = fake_server()
    bare = fake_server(metrics=None)
    progress = []

    servers = discovery.discover(
        extra=[f"127.0.0.1:{bare.server_address[1]}", f"127.0.0.1:{rich.server_address[1]}"],
        sweep=False,
        progress=lambda done, total: progress.append((done, total)),
    )

    assert [s.endpoint.port for s in servers] == [
        rich.server_address[1],
        bare.server_address[1],
    ]
    assert progress[-1] == (2, 2)
    assert discovery.best(servers).endpoint.port == rich.server_address[1]


def test_unreachable_lists_only_the_dead_servers(fake_server):
    alive = fake_server()
    addresses = [f"127.0.0.1:{alive.server_address[1]}", "127.0.0.1:1", "not a host:x"]

    assert discovery.unreachable(addresses, timeout=0.5) == addresses[1:]

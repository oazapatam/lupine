"""``lupine-run``: run a program against remote LUPINE GPUs.

The server comes from ``--server``, then ``LUPINE_SERVER``, then the tray's
selection, and otherwise from discovery: the best server found on the
network is used and named on stderr.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys

from . import discovery, gpuinfo
from .settings import ClientError, Settings, client_environment

_EPILOG = """\
examples:
  lupine-run nvidia-smi
  lupine-run python3 train.py
  lupine-run                      # a shell whose programs use the remote GPUs
  lupine-run --server gpu-host -- ./my_cuda_app --flag
  eval "$(lupine-run --env)"      # use them in the current shell
  lupine-run --discover
"""


def _gib(value: int) -> str:
    return f"{value / (1 << 30):.1f}"


def print_servers(servers: list[discovery.Server], selected: list[str]) -> None:
    rows = [("", "SERVER", "ADDRESS", "GPUS", "MEMORY (GiB)", "LOAD", "CLIENTS")]
    for server in servers:
        memory = server.memory()
        load = server.utilization()
        rows.append(
            (
                "*" if server.address in selected else "",
                server.label,
                server.address,
                server.gpu_summary(),
                f"{_gib(memory[0])} / {_gib(memory[1])}" if memory else "-",
                "-" if load is None else f"{load}%",
                str(server.clients) if server.metrics else "-",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def _split(values: list[str]) -> list[str]:
    return [item.strip() for value in values for item in value.split(",") if item.strip()]


def _normalize(values: list[str], port: int) -> list[str]:
    try:
        return [str(discovery.Endpoint.parse(value, port)) for value in values]
    except ValueError as exc:
        raise SystemExit(f"lupine-run: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lupine-run",
        description="Run a command on remote LUPINE GPUs.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-s",
        "--server",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help="server to use; repeat or separate with commas for several",
    )
    parser.add_argument(
        "--discover", action="store_true", help="list LUPINE servers on the network"
    )
    parser.add_argument(
        "--network",
        action="append",
        default=[],
        metavar="CIDR",
        help="also sweep this network while discovering",
    )
    parser.add_argument(
        "--env", action="store_true", help="print shell exports instead of running"
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command

    settings = Settings.load()
    networks = settings.networks + args.network
    if args.discover:
        servers = discovery.discover(
            port=settings.port, extra=settings.manual + settings.servers, networks=networks
        )
        if not servers:
            print("No LUPINE servers found.", file=sys.stderr)
            return 1
        for server in servers:
            # Servers without /metrics answer through the client's NVML; only
            # the selected ones, whose client is known to fit.
            if server.address in settings.servers and not server.metrics:
                try:
                    env = client_environment([server.address], libdir=settings.libdir)
                    server.gpus = gpuinfo.gpus(gpuinfo.query(env, cuda=True))
                except (ClientError, RuntimeError):
                    pass
        print_servers(servers, settings.servers)
        return 0

    if args.server:
        selected = _normalize(_split(args.server), settings.port)
    elif os.environ.get("LUPINE_SERVER", "").strip():
        selected = _split([os.environ["LUPINE_SERVER"]])
    elif settings.servers:
        selected = settings.servers
    else:
        print("lupine-run: no server selected; searching the network...", file=sys.stderr)
        found = discovery.discover(
            port=settings.port, extra=settings.manual, networks=networks
        )
        if not found:
            print(
                "lupine-run: no LUPINE server found; pass --server HOST[:PORT]",
                file=sys.stderr,
            )
            return 1
        chosen = discovery.best(found)
        selected = [chosen.address]
        print(
            f"lupine-run: using {chosen.label} ({chosen.address}, {chosen.gpu_summary()})",
            file=sys.stderr,
        )

    try:
        env = client_environment(selected, libdir=settings.libdir)
    except ClientError as exc:
        print(f"lupine-run: {exc}", file=sys.stderr)
        return 1

    if args.env:
        for name in (
            "LUPINE_SERVER",
            "LUPINE_LIBDIR",
            "LUPINE_CLIENT_ETAG",
            "LUPINE_CLIENT_PLATFORM",
            "LD_LIBRARY_PATH",
            "TRITON_LIBCUDA_PATH",
        ):
            if name in env:
                print(f"export {name}={shlex.quote(env[name])}")
        return 0

    down = discovery.unreachable(selected)
    if down:
        print(
            f"lupine-run: {', '.join(down)} not answering; is the LUPINE server "
            "running and reachable from this machine?",
            file=sys.stderr,
        )
        return 1

    if not command:
        shell = os.environ.get("SHELL") or "/bin/sh"
        print(
            f"LUPINE shell on {env['LUPINE_SERVER']}; type 'exit' to leave.",
            file=sys.stderr,
        )
        command = [shell]
    try:
        os.execvpe(command[0], command, env)
    except OSError as exc:
        print(f"lupine-run: {command[0]}: {exc.strerror}", file=sys.stderr)
        return 127

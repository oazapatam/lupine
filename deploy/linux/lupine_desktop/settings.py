"""Settings shared by the tray and ``lupine-run``, and the client environment.

``~/.config/lupine/client.json``::

    {
      "servers":  ["192.168.1.20:14833"],   // selected; LUPINE_SERVER order
      "manual":   ["gpu.example.com"],      // always probed
      "networks": ["10.4.0.0/24"],          // swept besides the local ones
      "port":     14833,                    // port probed during sweeps
      "libdir":   "",                       // local client build, if any
      "session_env": false,                 // export LUPINE_SERVER at login
      "native_gpus": true                   // every program sees the GPUs
    }
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from lupine import _bundles, _native

from .discovery import DEFAULT_PORT

AUTOSTART_NAME = "lupine-tray.desktop"
# The client libraries every program of the session loads while the tray runs:
# with them, CUDA and NVML list this machine's GPUs and the selected servers'.
NATIVE_LIBRARIES = ("libcuda.so.1", "libnvidia-ml.so.1")


class ClientError(RuntimeError):
    """The native client for the selected servers could not be prepared."""


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def settings_path() -> Path:
    return config_home() / "lupine" / "client.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


@dataclass
class Settings:
    servers: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    port: int = DEFAULT_PORT
    libdir: str = ""
    session_env: bool = False
    native_gpus: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        try:
            data = json.loads((path or settings_path()).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        port = data.get("port")
        return cls(
            servers=_strings(data.get("servers")),
            manual=_strings(data.get("manual")),
            networks=_strings(data.get("networks")),
            port=port if isinstance(port, int) and 0 < port < 65536 else DEFAULT_PORT,
            libdir=data.get("libdir") if isinstance(data.get("libdir"), str) else "",
            session_env=data.get("session_env") is True,
            native_gpus=data.get("native_gpus") is not False,
        )

    def save(self, path: Path | None = None) -> None:
        _atomic_write(path or settings_path(), json.dumps(asdict(self), indent=2) + "\n")


def client_environment(
    servers: Sequence[str],
    base: Mapping[str, str] | None = None,
    libdir: str = "",
) -> dict[str, str]:
    """The environment a program needs to use ``servers``.

    The client bundle each server publishes is downloaded (or revalidated in
    the cache shared with the ``lupine`` package) and put first on
    ``LD_LIBRARY_PATH``, the layout the client container image uses. A
    ``libdir``, or a ``LUPINE_LIBDIR`` the user set, replaces the download.
    """

    if not servers:
        raise ClientError("No LUPINE server is selected.")
    env = dict(os.environ if base is None else base)
    previous = env.get("LUPINE_LIBDIR", "")
    # LUPINE_CLIENT_ETAG marks a LUPINE_LIBDIR this code chose for another
    # selection (a shell started by the tray, say), not a user override.
    ours = "LUPINE_CLIENT_ETAG" in env
    env["LUPINE_SERVER"] = ",".join(servers)
    directory = libdir or ("" if ours else previous)
    if directory:
        for name in ("LUPINE_CLIENT_ETAG", "LUPINE_CLIENT_PLATFORM"):
            env.pop(name, None)
    else:
        try:
            path, etag, platform_key, _ = _bundles.resolve(
                tuple(servers), _native._REQUIRED[sys.platform]
            )
        except Exception as exc:
            reason = str(exc)
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 503:
                reason = (
                    "the server publishes no client (a development build?); "
                    'set "libdir" in ' + str(settings_path()) + " to a local client build"
                )
            raise ClientError(
                f"Could not get the LUPINE client from {', '.join(servers)}: {reason}"
            ) from exc
        directory = str(path)
        env["LUPINE_CLIENT_ETAG"] = etag
        env["LUPINE_CLIENT_PLATFORM"] = platform_key
    env["LUPINE_LIBDIR"] = directory

    paths = [p for p in env.get("LD_LIBRARY_PATH", "").split(":") if p]
    if ours and previous:
        paths = [p for p in paths if p != previous]
    env["LD_LIBRARY_PATH"] = ":".join([directory, *(p for p in paths if p != directory)])
    if ours and env.get("TRITON_LIBCUDA_PATH") == previous:
        del env["TRITON_LIBCUDA_PATH"]
    # Triton links its launcher against a libcuda it finds on disk.
    env.setdefault("TRITON_LIBCUDA_PATH", directory)
    return env


def instance_path() -> str:
    """The socket a running tray listens on."""

    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(runtime, f"lupine-tray-{os.getuid()}.sock")


def native_dir() -> Path | None:
    """Where the tray puts the client while it runs: ``$XDG_RUNTIME_DIR/lupine``.

    The session's ``LD_LIBRARY_PATH`` names it, so every program loads the
    client libraries from there, and the clients read the selected servers
    from its ``servers`` file. With the tray stopped it is empty and programs
    load the system's driver libraries as before.
    """

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) / "lupine" if runtime else None


def native_env_path() -> Path:
    return config_home() / "environment.d" / "61-lupine-gpus.conf"


def native_active() -> bool:
    """Whether this session's programs already look in :func:`native_dir`."""

    directory = native_dir()
    return directory is not None and str(directory) in os.environ.get(
        "LD_LIBRARY_PATH", ""
    ).split(":")


def set_native_environment(enabled: bool) -> bool:
    """Put :func:`native_dir` on the session's ``LD_LIBRARY_PATH`` from the next login.

    False when the session has no ``XDG_RUNTIME_DIR`` to put it in.
    """

    path = native_env_path()
    directory = native_dir()
    if not enabled or directory is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return not enabled
    _atomic_write(
        path,
        "# Written by the LUPINE tray: while it runs, programs load the LUPINE\n"
        "# client from here and see the remote GPUs beside this machine's.\n"
        f"LD_LIBRARY_PATH={directory}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}\n"
        "# The client's connection diagnostics would reach every program.\n"
        "LUPINE_LOG_LEVEL=error\n",
    )
    return True


def sync_native(servers: Sequence[str], libdir: str | None) -> None:
    """Point :func:`native_dir` at ``libdir``'s client for ``servers``, or empty it."""

    directory = native_dir()
    if directory is None:
        return
    wanted = {name: str(Path(libdir) / name) for name in NATIVE_LIBRARIES} if libdir and servers else {}
    directory.mkdir(mode=0o700, exist_ok=True)
    for name in NATIVE_LIBRARIES:
        link = directory / name
        target = wanted.get(name)
        current = os.readlink(link) if link.is_symlink() else None
        if target == current:
            continue
        if target is None:
            link.unlink(missing_ok=True)
            continue
        temporary = directory / f".{name}.new"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        os.replace(temporary, link)
    selection = directory / "servers"
    text = ",".join(servers) + "\n" if wanted else None
    try:
        current_text = selection.read_text(encoding="utf-8")
    except OSError:
        current_text = None
    if text is None:
        selection.unlink(missing_ok=True)
    elif text != current_text:
        _atomic_write(selection, text)


def session_env_path() -> Path:
    return config_home() / "environment.d" / "60-lupine.conf"


def _systemctl(*args: str) -> None:
    systemctl = shutil.which("systemctl")
    if systemctl:
        try:
            subprocess.run([systemctl, "--user", *args], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def set_session_env(servers: Sequence[str] | None) -> None:
    """Export ``LUPINE_SERVER`` to graphical sessions, or stop exporting it.

    systemd reads ``environment.d`` at login; the running user manager is
    updated too, so services and apps it starts from now on see the change.
    """

    path = session_env_path()
    if servers:
        value = ",".join(servers)
        _atomic_write(
            path,
            "# Written by the LUPINE tray; change the server from its menu.\n"
            f"LUPINE_SERVER={value}\n",
        )
        _systemctl("set-environment", f"LUPINE_SERVER={value}")
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        _systemctl("unset-environment", "LUPINE_SERVER")


def _autostart_paths() -> tuple[Path, list[Path]]:
    user = config_home() / "autostart" / AUTOSTART_NAME
    dirs = os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg"
    system = [Path(d) / "autostart" / AUTOSTART_NAME for d in dirs.split(":") if d]
    return user, system


def _hidden(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return True
    return any(line.strip().lower() in ("hidden=true", "x-gnome-autostart-enabled=false") for line in lines)


def autostart_enabled() -> bool:
    user, system = _autostart_paths()
    if user.exists():
        return not _hidden(user)
    return any(path.exists() and not _hidden(path) for path in system)


def set_autostart(enabled: bool, executable: str) -> None:
    """Follow the XDG autostart rules: a user entry overrides a system one."""

    user, system = _autostart_paths()
    system_entry = any(path.exists() for path in system)
    if enabled and system_entry:
        try:
            user.unlink()
        except FileNotFoundError:
            pass
        if autostart_enabled():
            return
    if not enabled and not system_entry:
        try:
            user.unlink()
        except FileNotFoundError:
            pass
        return
    entry = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=LUPINE Client",
        "Comment=Remote GPU servers in the system tray",
        f'Exec="{executable}"',
        "Icon=lupine",
        "Terminal=false",
    ]
    entry.append("X-GNOME-Autostart-enabled=true" if enabled else "Hidden=true")
    _atomic_write(user, "\n".join(entry) + "\n")

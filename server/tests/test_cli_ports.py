"""Default-port discovery and explicit-port behaviour of ``tracedown up``."""
import socket
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
import uvicorn
from tracedown_server import api as api_mod
from tracedown_server import cli


@pytest.fixture()
def startup(tmp_path, monkeypatch):
    monkeypatch.delenv("TDN_PORT", raising=False)
    monkeypatch.setenv("TDN_HOST", "127.0.0.1")
    monkeypatch.setenv("TDN_DB", str(tmp_path / "tracedown.db"))
    app = Mock()
    create_app = Mock(return_value=app)
    server = Mock()
    thread = Mock()
    thread.is_alive.return_value = False
    ready = Mock(return_value=True)
    browser = Mock()
    monkeypatch.setattr(api_mod, "create_app", create_app)
    monkeypatch.setattr(api_mod, "web_available", lambda: True)
    monkeypatch.setattr(uvicorn, "Server", server)
    monkeypatch.setattr(cli, "threading", SimpleNamespace(Thread=Mock(return_value=thread)))
    monkeypatch.setattr(cli, "_wait_until_up", ready)
    monkeypatch.setattr(cli.webbrowser, "open", browser)
    return SimpleNamespace(server=server, ready=ready, browser=browser, create_app=create_app)


@pytest.fixture()
def occupied_port():
    # Let the OS choose a port, then find one with a free neighbour. Keeping
    # the listener open exercises the real bind check without reserving 7070.
    for _ in range(20):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            if port < 65526 and cli._port_free("127.0.0.1", port + 1):
                yield port
                return
    pytest.fail("could not find an occupied port with a free neighbour")


def assert_started(startup, capsys, port, page="/runs/", host="127.0.0.1"):
    config = startup.server.call_args.args[0]
    assert (config.host, config.port) == (host, port)
    url = f"http://127.0.0.1:{port}"
    startup.ready.assert_called_once_with(url, startup.server.return_value)
    startup.browser.assert_called_once_with(url + page)
    assert f"Tracedown is running at {url}" in capsys.readouterr().out


@pytest.mark.parametrize("bundled_web", [True, False])
def test_default_port_falls_back_to_next_free_port(
    startup, occupied_port, monkeypatch, capsys, bundled_web,
):
    monkeypatch.setattr(cli, "_DEFAULT_PORT", occupied_port)
    monkeypatch.setattr(api_mod, "web_available", lambda: bundled_web)
    assert cli.main(["up", "--no-demo"]) == 0
    assert_started(startup, capsys, occupied_port + 1, "/runs/" if bundled_web else "/")


def test_free_default_port_is_kept(startup, monkeypatch, capsys):
    probe = Mock(return_value=True)
    monkeypatch.setattr(cli, "_port_free", probe)
    assert cli.main(["up", "--no-demo"]) == 0
    probe.assert_called_once_with("127.0.0.1", 7070)
    assert_started(startup, capsys, 7070)


def test_search_includes_last_port_and_uses_requested_host(startup, monkeypatch, capsys):
    probe = Mock(side_effect=lambda host, port: port == 7079)
    monkeypatch.setattr(cli, "_port_free", probe)
    assert cli.main(["up", "--host", "0.0.0.0", "--no-demo"]) == 0
    assert probe.call_args_list == [call("0.0.0.0", port) for port in range(7070, 7080)]
    assert_started(startup, capsys, 7079, host="0.0.0.0")


def test_search_exhaustion_does_not_start_server(startup, monkeypatch, capsys):
    probe = Mock(return_value=False)
    monkeypatch.setattr(cli, "_port_free", probe)
    assert cli.main(["up", "--no-demo"]) == 1
    assert probe.call_args_list == [call("127.0.0.1", port) for port in range(7070, 7080)]
    assert "7070-7079" in capsys.readouterr().err
    startup.create_app.assert_not_called()
    startup.server.assert_not_called()
    startup.browser.assert_not_called()


@pytest.mark.parametrize("setting", ["flag", "environment"])
def test_explicit_busy_port_does_not_fall_back(
    startup, occupied_port, monkeypatch, capsys, setting,
):
    monkeypatch.setattr(cli, "_DEFAULT_PORT", occupied_port)
    argv = ["up", "--no-demo"]
    if setting == "flag":
        argv += ["--port", str(occupied_port)]
    else:
        monkeypatch.setenv("TDN_PORT", str(occupied_port))
    assert cli.main(argv) == 1
    assert f"Port {occupied_port} is in use" in capsys.readouterr().err
    startup.create_app.assert_not_called()
    startup.server.assert_not_called()
    startup.browser.assert_not_called()


@pytest.mark.parametrize("explicit_flag", [False, True])
def test_configured_port_is_used_and_flag_overrides_environment(
    startup, monkeypatch, capsys, explicit_flag,
):
    monkeypatch.setenv("TDN_PORT", "8080")
    probe = Mock(return_value=True)
    monkeypatch.setattr(cli, "_port_free", probe)
    argv = ["up", "--no-demo"] + (["--port", "9090"] if explicit_flag else [])
    port = 9090 if explicit_flag else 8080
    assert cli.main(argv) == 0
    probe.assert_called_once_with("127.0.0.1", port)
    assert_started(startup, capsys, port)

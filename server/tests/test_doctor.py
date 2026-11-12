"""``tracedown doctor``: each setup check, plus the command's exit code."""
import socket
from types import SimpleNamespace

from tracedown_server import cli


def test_python_check_accepts_310_and_newer():
    assert cli._doctor_python((3, 10, 0))[0]
    assert cli._doctor_python((3, 12, 4))[0]


def test_python_check_rejects_39_with_a_fix():
    ok, line = cli._doctor_python((3, 9, 18))
    assert not ok
    assert "3.9.18" in line and "3.10" in line


def test_packages_check_reports_both_installed(monkeypatch):
    from importlib import metadata
    monkeypatch.setattr(metadata, "version", lambda dist: "1.2.3")
    checks = cli._doctor_packages()
    assert [ok for ok, _ in checks] == [True, True]
    assert all("1.2.3" in line for _, line in checks)


def test_packages_check_names_the_missing_one(monkeypatch):
    from importlib import metadata

    def version(dist):
        if dist == "agent-tracedown-server":
            raise metadata.PackageNotFoundError(dist)
        return "0.1.0"

    monkeypatch.setattr(metadata, "version", version)
    checks = dict((line.split(" ")[0], ok) for ok, line in cli._doctor_packages())
    assert checks["agent-tracedown"] is True
    assert checks["agent-tracedown-server"] is False


def test_db_check_existing_writable_file(tmp_path):
    db = tmp_path / "tracedown.db"
    db.write_bytes(b"")
    ok, line = cli._doctor_db(str(db))
    assert ok and "writable" in line


def test_db_check_missing_file_in_writable_folder(tmp_path):
    ok, line = cli._doctor_db(str(tmp_path / "new.db"))
    assert ok and "will create it" in line


def test_db_check_synced_folder_is_a_problem(tmp_path):
    path = str(tmp_path / "OneDrive" / "tracedown.db")
    ok, line = cli._doctor_db(path)
    assert not ok
    assert "synced" in line and "readonly database" in line


def test_db_check_directory_is_not_a_database(tmp_path):
    ok, line = cli._doctor_db(str(tmp_path))
    assert not ok and "not a file" in line


def test_db_check_memory_database():
    assert cli._doctor_db(":memory:")[0]


def test_port_check_busy_and_free():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        ok, line = cli._doctor_port("127.0.0.1", port)
        assert not ok and "in use" in line
    ok, line = cli._doctor_port("127.0.0.1", port)
    assert ok and "free" in line


def test_judge_check_unset_is_fine():
    ok, line = cli._doctor_judge({})
    assert ok and "not set" in line


def test_judge_check_set_with_key():
    ok, line = cli._doctor_judge({"TDN_JUDGE": "anthropic:claude-sonnet-5-5",
                                  "ANTHROPIC_API_KEY": "sk-test"})
    assert ok and "ANTHROPIC_API_KEY is set" in line


def test_judge_check_set_without_key_names_the_variable_but_not_a_key():
    ok, line = cli._doctor_judge({"TDN_JUDGE": "openai:gpt-5.4-mini"})
    assert not ok
    assert "OPENAI_API_KEY" in line


def test_judge_check_unknown_provider():
    ok, line = cli._doctor_judge({"TDN_JUDGE": "ollama:llama3"})
    assert not ok and "openai:<model>" in line


def _doctor_args(tmp_path):
    return SimpleNamespace(host="127.0.0.1", port=7070, db=str(tmp_path / "tracedown.db"))


def test_doctor_command_all_green(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TDN_JUDGE", raising=False)
    monkeypatch.setattr(cli, "_doctor_web", lambda: (True, "web app present"))
    assert cli.doctor(_doctor_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert out.count("✓") >= 6
    assert "✗" not in out


def test_doctor_command_reports_problems_and_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TDN_JUDGE", "openai:gpt-5.4-mini")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_doctor_web", lambda: (True, "web app present"))
    assert cli.doctor(_doctor_args(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "✗" in out and "OPENAI_API_KEY" in out
    assert "1 problem(s)" in out


def test_doctor_is_a_subcommand(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "doctor", lambda args: 7)
    assert cli.main(["doctor"]) == 7

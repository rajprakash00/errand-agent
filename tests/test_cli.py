from __future__ import annotations

from errand_agent.cli import main


def test_demo_command_completes_and_writes_a_trace(tmp_path, capsys, read_trace) -> None:
    data_dir = tmp_path / "data"
    code = main(["demo", "--data-dir", str(data_dir)])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "Final state: completed" in captured.out
    assert "Trace:" in captured.out

    traces = list((data_dir / "traces").glob("*.jsonl"))
    assert len(traces) == 1
    events = read_trace(traces[0])
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "completed"

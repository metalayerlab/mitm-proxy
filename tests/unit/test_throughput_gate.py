"""The throughput gate's pure parts (design 0013, section 10): its statistics and its pass mark.

The script itself runs curl against a test deployment, by hand; it is never run here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "benchmarks" / "throughput_gate.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("throughput_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["throughput_gate"] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


gate = _load()

STEADY = [100.0] * 60  # Mbit/s each second
QUICK = [0.020] * 50  # first byte, seconds


def test_percentile_interpolates_between_ranks() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert gate.percentile(values, 0) == 10.0
    assert gate.percentile(values, 50) == 30.0
    assert gate.percentile(values, 100) == 50.0
    assert gate.percentile(values, 10) == pytest.approx(14.0)
    assert gate.percentile(values, 95) == pytest.approx(48.0)
    assert gate.percentile([7.0], 95) == 7.0
    with pytest.raises(ValueError, match="no values"):
        gate.percentile([], 50)
    with pytest.raises(ValueError, match="0-100"):
        gate.percentile(values, 101)


def test_per_second_counts_stalls_as_zero_and_drops_the_warmup() -> None:
    start = 1000.0
    events = [(1000.1, 125_000), (1000.9, 125_000), (1002.5, 250_000), (1009.0, 999)]
    assert gate.per_second_mbit(events, start, 3) == [2.0, 0.0, 2.0]
    assert gate.per_second_mbit(events, start, 3, warmup=1) == [0.0, 2.0]
    assert gate.per_second_mbit([(999.0, 10)], start, 2) == [0.0, 0.0]  # before the start


def test_a_fast_cheap_proxy_passes() -> None:
    verdict = gate.decide([97.0] * 60, STEADY, [[60.0] * 30, [55.0] * 30], [0.023] * 50, QUICK, 0.2)
    assert verdict.result == "pass"
    assert all(reason.startswith("ok ") for reason in verdict.reasons)
    assert verdict.numbers["rate_ratio"] == pytest.approx(0.97)


@pytest.mark.parametrize(
    ("proxy", "parallel", "ttfb", "cpu", "failure"),
    [
        ([90.0] * 60, [[60.0], [60.0]], [0.020] * 50, 0.2, "median rate 90.0%"),
        ([100.0] * 50 + [5.0] * 10, [[60.0], [60.0]], [0.020] * 50, 0.2, "10th percentile"),
        (STEADY, [[60.0], [20.0]], [0.020] * 50, 0.2, "two at once"),
        (STEADY, [[60.0], []], [0.020] * 50, 0.2, "two downloads at once: no samples"),
        (STEADY, [[60.0], [60.0]], [0.026] * 50, 0.2, "6.0 ms at the median"),
        (STEADY, [[60.0], [60.0]], [0.020] * 45 + [0.050] * 5, 0.2, "at the 95th percentile"),
        (STEADY, [[60.0], [60.0]], [0.020] * 50, 0.5, "CPU 0.50 cores"),
        (STEADY, [[60.0], [60.0]], [], 0.2, "latency: no samples"),
    ],
)
def test_each_part_of_the_pass_mark_can_fail(
    proxy: list[float], parallel: list[list[float]], ttfb: list[float], cpu: float, failure: str
) -> None:
    verdict = gate.decide(proxy, STEADY, parallel, ttfb, QUICK, cpu)
    assert verdict.result == "fail"
    failed = [r for r in verdict.reasons if r.startswith("FAIL ")]
    assert len(failed) == 1 and failure in failed[0]


def test_a_slow_exit_is_inconclusive() -> None:
    verdict = gate.decide([20.0] * 60, [24.0] * 60, [[20.0], [20.0]], QUICK, QUICK, 0.1)
    assert verdict.result == "inconclusive"
    assert "the exit is the bottleneck" in verdict.reasons[0]


def test_without_the_cpu_cost_it_can_only_fail_or_be_inconclusive() -> None:
    good = gate.decide(STEADY, STEADY, [[60.0], [60.0]], QUICK, QUICK, None)
    assert good.result == "inconclusive"
    assert "--proxy-pid" in good.reasons[0]
    bad = gate.decide([50.0] * 60, STEADY, [[60.0], [60.0]], QUICK, QUICK, None)
    assert bad.result == "fail"


def test_no_samples_fails() -> None:
    assert gate.decide([], STEADY, [], QUICK, QUICK, 0.1).result == "fail"


def test_every_endpoint_is_an_argument_and_only_the_probe_has_a_default(tmp_path: Path) -> None:
    args = gate.parse_args(
        [
            "--proxy",
            "http://127.0.0.1:3128",
            "--exit",
            "http://exit-a.exits:8001",
            "--download-url",
            "https://download.example/big",
            "--out",
            str(tmp_path),
            "--curl",
            sys.executable,  # anything on PATH: nothing is run here
        ]
    )
    assert (args.rounds, args.duration, args.latency_requests) == (5, 120, 200)
    assert args.probe_url == "https://www.gstatic.com/generate_204"
    assert args.video_url is None
    with pytest.raises(SystemExit):
        gate.parse_args(["--proxy", "http://127.0.0.1:3128", "--out", str(tmp_path)])


def test_a_chatty_tool_never_stalls_on_its_stderr(tmp_path: Path) -> None:
    # 1 MiB on stderr before any stdout: far more than a pipe holds. Read only at the end, a
    # piped stderr would block the tool until the deadline, and its download would read as zero.
    script = (
        "import sys; sys.stderr.write('x' * (1 << 20) + 'the real error'); sys.stderr.flush(); "
        "sys.stdout.write('y' * 1000); sys.stdout.flush(); sys.exit(3)"
    )
    events: list[tuple[float, int]] = []
    started = gate.time.monotonic()
    error = gate._count([sys.executable, "-c", script], started + 20, events, tmp_path)
    assert gate.time.monotonic() - started < 10
    assert sum(size for _, size in events) == 1000
    assert error is not None and error.endswith("the real error")
    assert " exited 3: " in error
    assert list(tmp_path.iterdir()) == []  # the temporary file is gone


def test_a_clean_download_has_no_error(tmp_path: Path) -> None:
    events: list[tuple[float, int]] = []
    command = [sys.executable, "-c", "print('z' * 99)"]
    until = gate.time.monotonic() + 20
    assert gate._count(command, until, events, tmp_path) is None
    assert sum(size for _, size in events) == 100

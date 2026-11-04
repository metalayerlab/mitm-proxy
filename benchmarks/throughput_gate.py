#!/usr/bin/env python3
"""The streaming-throughput gate of design 0013 (section 10): can mitm-proxy carry 4K video
through an exit?

It runs beside a mitm-proxy whose only exit is the exit under test, and compares two paths to the
same exit, alternating, round after round:

- **through mitm-proxy**: ``curl -x <proxy>``, a large HTTPS download, bytes counted each second;
- **baseline**: the same download with ``curl -x <exit>``, straight to the exit;
- **two at once**: two downloads through mitm-proxy together, once;
- **latency**: small requests to the probe URL through each path, timing the first byte;
- **a real stream** (if ``yt-dlp`` is installed and ``--video-url`` is given): one 4K format through
  each path, for its rate only (it is not part of the pass mark).

It writes the per-second samples, the latencies and a summary to ``--out``, and prints PASS, FAIL
or INCONCLUSIVE against the pass mark (0013, section 10):

- through mitm-proxy, the median per-second rate is at least 95% of the baseline's, and the 10th
  percentile at least 25 Mbit/s; with two downloads at once, each at least 25 Mbit/s (its median);
- first byte through mitm-proxy minus the baseline: at most 5 ms at the median, 15 ms at the 95th
  percentile;
- mitm-proxy under half a CPU core while it carries the download (``--proxy-pid``, read from
  ``/proc``; without it the gate can't pass, only fail or be inconclusive);
- inconclusive if the baseline's median is under 25 Mbit/s: the exit is the bottleneck, so run the
  gate again on another exit.

The download must be tunnelled, not decrypted: mitm-proxy's ``intercept.hosts`` must not list its
host. Standard library only; it needs ``curl`` (and optionally ``yt-dlp``) on PATH. It is never run
by the tests or CI: it is for a person, against a test deployment.

Exit codes: 0 pass, 1 fail, 3 inconclusive, 2 a usage error.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

# The record's own probe URL (0013, section 9): the only real host this script names.
DEFAULT_PROBE_URL = "https://www.gstatic.com/generate_204"
PROXY = "proxy"  # the path through mitm-proxy
BASELINE = "baseline"  # the path straight to the exit
READ_SIZE = 1 << 16

EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_INCONCLUSIVE = 0, 1, 2, 3


# The pass mark and the statistics: pure, and unit-tested.


@dataclass(frozen=True)
class PassMark:
    """0013, section 10 (decided as recommended, open question 3)."""

    rate_ratio: float = 0.95  # proxy median / baseline median, at least
    p10_floor_mbit: float = 25.0  # proxy 10th percentile, at least
    parallel_floor_mbit: float = 25.0  # each of two downloads at once, at least (its median)
    added_median_ms: float = 5.0  # proxy first byte minus baseline, at the median, at most
    added_p95_ms: float = 15.0  # ... and at the 95th percentile, at most
    cpu_max_cores: float = 0.5  # mitm-proxy's CPU while carrying the download, under
    baseline_floor_mbit: float = 25.0  # a baseline median under this is inconclusive


@dataclass
class Verdict:
    result: str  # "pass", "fail" or "inconclusive"
    reasons: list[str] = field(default_factory=list)
    numbers: dict[str, float | None] = field(default_factory=dict)


def percentile(values: Sequence[float], p: float) -> float:
    """The p-th percentile (0-100), interpolating linearly between the closest ranks."""
    if not values:
        raise ValueError("no values")
    if not 0 <= p <= 100:
        raise ValueError("p must be 0-100")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p / 100
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def per_second_mbit(
    events: Sequence[tuple[float, int]], start: float, seconds: int, warmup: int = 0
) -> list[float]:
    """Mbit/s for each whole second from start: events are (monotonic time, bytes read).

    Seconds with no bytes count as zero (a stall is a stall); the first ``warmup`` are dropped.
    """
    counts = [0] * seconds
    for when, size in events:
        index = int(when - start)
        if 0 <= index < seconds:
            counts[index] += size
    return [n * 8 / 1_000_000 for n in counts[warmup:]]


def decide(
    proxy_mbit: Sequence[float],
    baseline_mbit: Sequence[float],
    parallel_mbit: Sequence[Sequence[float]],
    proxy_ttfb_s: Sequence[float],
    baseline_ttfb_s: Sequence[float],
    cpu_cores: float | None,
    mark: PassMark = PassMark(),  # noqa: B008 - frozen, so one shared default is safe
) -> Verdict:
    """Pass, fail or inconclusive against the pass mark, with the numbers and every reason."""
    numbers: dict[str, float | None] = {}
    if not baseline_mbit or not proxy_mbit:
        return Verdict("fail", ["no download samples: did curl run?"], numbers)
    base_median = statistics.median(baseline_mbit)
    numbers["baseline_median_mbit"] = base_median
    numbers["proxy_median_mbit"] = statistics.median(proxy_mbit)
    numbers["proxy_p10_mbit"] = percentile(proxy_mbit, 10)
    if base_median < mark.baseline_floor_mbit:
        return Verdict(
            "inconclusive",
            [
                f"the baseline's median is {base_median:.1f} Mbit/s, under "
                f"{mark.baseline_floor_mbit:g}: the exit is the bottleneck; run the gate on "
                "another exit"
            ],
            numbers,
        )
    failed: list[str] = []
    passed: list[str] = []

    def check(ok: bool, text: str) -> None:
        (passed if ok else failed).append(text)

    ratio = statistics.median(proxy_mbit) / base_median
    numbers["rate_ratio"] = ratio
    check(
        ratio >= mark.rate_ratio,
        f"median rate {ratio:.1%} of the baseline's (at least {mark.rate_ratio:.0%})",
    )
    p10 = percentile(proxy_mbit, 10)
    check(
        p10 >= mark.p10_floor_mbit,
        f"10th percentile {p10:.1f} Mbit/s (at least {mark.p10_floor_mbit:g})",
    )
    if not parallel_mbit or any(not stream for stream in parallel_mbit):
        failed.append("two downloads at once: no samples")
    else:
        medians = [statistics.median(stream) for stream in parallel_mbit]
        for i, median in enumerate(medians):
            numbers[f"parallel_{i}_median_mbit"] = median
        check(
            min(medians) >= mark.parallel_floor_mbit,
            "two at once: medians "
            + ", ".join(f"{m:.1f}" for m in medians)
            + f" Mbit/s (each at least {mark.parallel_floor_mbit:g})",
        )
    if not proxy_ttfb_s or not baseline_ttfb_s:
        failed.append("latency: no samples")
    else:
        added_median = (statistics.median(proxy_ttfb_s) - statistics.median(baseline_ttfb_s)) * 1000
        added_p95 = (percentile(proxy_ttfb_s, 95) - percentile(baseline_ttfb_s, 95)) * 1000
        numbers["added_median_ms"] = added_median
        numbers["added_p95_ms"] = added_p95
        check(
            added_median <= mark.added_median_ms,
            f"added first-byte latency {added_median:.1f} ms at the median "
            f"(at most {mark.added_median_ms:g})",
        )
        check(
            added_p95 <= mark.added_p95_ms,
            f"added first-byte latency {added_p95:.1f} ms at the 95th percentile "
            f"(at most {mark.added_p95_ms:g})",
        )
    numbers["cpu_cores"] = cpu_cores
    if cpu_cores is not None:
        check(
            cpu_cores < mark.cpu_max_cores,
            f"mitm-proxy CPU {cpu_cores:.2f} cores (under {mark.cpu_max_cores:g})",
        )
    if failed:
        return Verdict("fail", [f"FAIL {t}" for t in failed] + [f"ok {t}" for t in passed], numbers)
    if cpu_cores is None:
        return Verdict(
            "inconclusive",
            ["the CPU cost wasn't measured: give --proxy-pid"] + [f"ok {t}" for t in passed],
            numbers,
        )
    return Verdict("pass", [f"ok {t}" for t in passed], numbers)


# Measuring: curl, yt-dlp and /proc.


@dataclass
class Download:
    path: str
    round: int
    stream: int
    mbit: list[float]
    errors: list[str]


def _count(
    command: list[str], until: float, events: list[tuple[float, int]], scratch: Path
) -> str | None:
    """Run a command that writes a download to stdout, counting bytes as they arrive, until
    `until` (monotonic) or its end. Returns its error, if it failed.

    Its stderr goes to an unnamed temporary file in `scratch`, never a pipe: a pipe nobody reads
    until the end fills up when a tool is chatty (yt-dlp's progress), and the tool then stalls,
    which would read as a slow download. The file is gone once it is closed.
    """
    with tempfile.TemporaryFile(dir=scratch) as stderr:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr)
        if process.stdout is None:  # pragma: no cover - stdout=PIPE always gives one
            raise RuntimeError("no stdout")
        fd = process.stdout.fileno()
        timer = threading.Timer(max(0.0, until - time.monotonic()), process.kill)
        timer.start()
        try:
            while data := os.read(fd, READ_SIZE):
                events.append((time.monotonic(), len(data)))
        finally:
            timer.cancel()
            process.wait()
            process.stdout.close()
        if process.returncode not in (0, -9) and time.monotonic() < until:
            stderr.seek(0)
            text = stderr.read().decode(errors="replace").strip()
            return f"{command[0]} exited {process.returncode}: {text[-200:]}"
    return None


def download(
    command: list[str], seconds: int, warmup: int, scratch: Path
) -> tuple[list[float], list[str]]:
    """Per-second Mbit/s of a download repeated until `seconds` have passed. A failing tool's
    stderr is kept in a temporary file in `scratch` while it runs (see _count)."""
    events: list[tuple[float, int]] = []
    errors: list[str] = []
    start = time.monotonic()
    until = start + seconds
    while time.monotonic() < until:
        error = _count(command, until, events, scratch)
        if error is not None:
            errors.append(error)
            break  # a path that fails counts as zero for the rest of the time
    return per_second_mbit(events, start, seconds, warmup), errors


def curl_download(curl: str, via: str, url: str) -> list[str]:
    return [curl, "-sS", "--fail", "-o", "-", "-x", via, url]


def latency(curl: str, via: str, url: str, count: int) -> tuple[list[float], list[str]]:
    """Time to first byte (seconds) of `count` small requests through `via`."""
    out: list[float] = []
    errors: list[str] = []
    for _ in range(count):
        result = subprocess.run(
            [curl, "-sS", "-o", os.devnull, "-w", "%{time_starttransfer}", "-x", via, url],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            errors.append(f"curl exited {result.returncode}: {result.stderr.strip()[:200]}")
            continue
        out.append(float(result.stdout.strip()))
    return out, errors


def cpu_seconds(pid: int) -> float:
    """utime + stime of a process, from /proc/<pid>/stat."""
    text = Path(f"/proc/{pid}/stat").read_text()
    fields = text[text.rindex(")") + 2 :].split()
    ticks = int(fields[11]) + int(fields[12])  # utime, stime: fields 14 and 15 of the line
    return ticks / os.sysconf("SC_CLK_TCK")


class CpuMeter:
    """mitm-proxy's CPU in cores over each measured stretch; the gate reports the worst."""

    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        self.readings: list[float] = []
        self._started: tuple[float, float] | None = None

    def start(self) -> None:
        if self.pid is not None:
            self._started = (cpu_seconds(self.pid), time.monotonic())

    def stop(self) -> None:
        if self.pid is None or self._started is None:
            return
        cpu_before, wall_before = self._started
        wall = time.monotonic() - wall_before
        if wall > 0:
            self.readings.append((cpu_seconds(self.pid) - cpu_before) / wall)
        self._started = None

    @property
    def worst(self) -> float | None:
        return max(self.readings) if self.readings else None


# The run


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Design 0013's streaming-throughput gate: mitm-proxy against its exit.",
    )
    parser.add_argument(
        "--proxy", required=True, help="mitm-proxy's proxy URL, as curl -x takes it"
    )
    parser.add_argument(
        "--exit", required=True, dest="exit_url", help="the exit's own proxy URL (the baseline)"
    )
    parser.add_argument(
        "--download-url", required=True, help="a large file over HTTPS (a speed-test endpoint)"
    )
    parser.add_argument(
        "--probe-url", default=DEFAULT_PROBE_URL, help="a small URL for the latency requests"
    )
    parser.add_argument("--video-url", help="a video page for yt-dlp (optional)")
    parser.add_argument(
        "--video-format", default="bv*[height>=2160]", help="the yt-dlp format to fetch"
    )
    parser.add_argument("--out", required=True, type=Path, help="directory for the results")
    parser.add_argument("--rounds", type=int, default=5, help="rounds of each path")
    parser.add_argument("--duration", type=int, default=120, help="seconds per download")
    parser.add_argument("--latency-requests", type=int, default=200, help="per path")
    parser.add_argument(
        "--warmup", type=int, default=0, help="seconds dropped from the start of each download"
    )
    parser.add_argument(
        "--proxy-pid", type=int, help="mitm-proxy's process ID, to measure its CPU from /proc"
    )
    parser.add_argument("--curl", default="curl", help="the curl to run")
    parser.add_argument("--yt-dlp", default="yt-dlp", dest="yt_dlp", help="the yt-dlp to run")
    args = parser.parse_args(argv)
    if args.rounds < 1 or args.duration < 1 or args.latency_requests < 1:
        parser.error("--rounds, --duration and --latency-requests must be at least 1")
    if not 0 <= args.warmup < args.duration:
        parser.error("--warmup must be at least 0 and less than --duration")
    if shutil.which(args.curl) is None:
        parser.error(f"{args.curl!r} is not on PATH")
    return args


def _say(text: str) -> None:
    print(text, flush=True)


def run(args: argparse.Namespace) -> int:
    paths = {PROXY: args.proxy, BASELINE: args.exit_url}
    # The tools' stderr goes to temporary files here while they run (see _count).
    scratch = args.out / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    downloads: list[Download] = []
    cpu = CpuMeter(args.proxy_pid)
    for number in range(1, args.rounds + 1):
        # Alternate which path goes first, so a drift in the exit's rate hits both alike.
        order = (PROXY, BASELINE) if number % 2 else (BASELINE, PROXY)
        for path in order:
            _say(f"round {number}/{args.rounds}: {path}, {args.duration} s")
            command = curl_download(args.curl, paths[path], args.download_url)
            if path == PROXY:
                cpu.start()
            mbit, errors = download(command, args.duration, args.warmup, scratch)
            if path == PROXY:
                cpu.stop()
            downloads.append(Download(path, number, 0, mbit, errors))

    _say(f"two downloads at once through mitm-proxy, {args.duration} s")
    parallel: list[tuple[list[float], list[str]]] = [([], []), ([], [])]

    def stream(index: int) -> None:
        parallel[index] = download(
            curl_download(args.curl, args.proxy, args.download_url),
            args.duration,
            args.warmup,
            scratch,
        )

    threads = [threading.Thread(target=stream, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for i, (mbit, errors) in enumerate(parallel):
        downloads.append(Download("proxy-parallel", 0, i, mbit, errors))

    latencies: dict[str, tuple[list[float], list[str]]] = {}
    for path in (PROXY, BASELINE):
        _say(f"latency: {args.latency_requests} requests, {path}")
        latencies[path] = latency(args.curl, paths[path], args.probe_url, args.latency_requests)

    video: dict[str, dict[str, object]] = {}
    if args.video_url and shutil.which(args.yt_dlp) is not None:
        for path in (PROXY, BASELINE):
            _say(f"video: {args.video_format}, {path}")
            command = [
                args.yt_dlp, "--quiet", "--proxy", paths[path],
                "-f", args.video_format, "-o", "-", args.video_url,
            ]  # fmt: skip
            mbit, errors = download(command, args.duration, args.warmup, scratch)
            video[path] = {
                "median_mbit": statistics.median(mbit) if mbit else None,
                "errors": errors,
            }
            downloads.append(Download(f"video-{path}", 0, 0, mbit, errors))
    elif args.video_url:
        _say(f"video: skipped, {args.yt_dlp!r} is not on PATH")

    verdict = decide(
        [x for d in downloads if d.path == PROXY for x in d.mbit],
        [x for d in downloads if d.path == BASELINE for x in d.mbit],
        [mbit for mbit, _ in parallel],
        latencies[PROXY][0],
        latencies[BASELINE][0],
        cpu.worst,
    )
    write_results(args.out, downloads, latencies, video, verdict, args)
    for reason in verdict.reasons:
        _say(reason)
    _say(f"RESULT: {verdict.result.upper()}")
    return {"pass": EXIT_PASS, "fail": EXIT_FAIL}.get(verdict.result, EXIT_INCONCLUSIVE)


def write_results(
    out: Path,
    downloads: list[Download],
    latencies: dict[str, tuple[list[float], list[str]]],
    video: dict[str, dict[str, object]],
    verdict: Verdict,
    args: argparse.Namespace,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "samples.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["path", "round", "stream", "second", "mbit_per_s"])
        for d in downloads:
            for second, mbit in enumerate(d.mbit, start=args.warmup):
                writer.writerow([d.path, d.round, d.stream, second, f"{mbit:.3f}"])
    with (out / "latency.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["path", "request", "first_byte_s"])
        for path, (times, _) in latencies.items():
            for i, seconds in enumerate(times):
                writer.writerow([path, i, f"{seconds:.6f}"])
    summary = {
        "verdict": asdict(verdict),
        "pass_mark": asdict(PassMark()),
        "settings": {
            "rounds": args.rounds,
            "duration_s": args.duration,
            "warmup_s": args.warmup,
            "latency_requests": args.latency_requests,
            "video_format": args.video_format if args.video_url else None,
        },
        "errors": {
            "downloads": [e for d in downloads for e in d.errors],
            "latency": {path: errors for path, (_, errors) in latencies.items()},
        },
        "video": video,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

from motte_harness.supervisor import SupervisedLimits, SupervisedProcess, SupervisedProcessError


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job ownership')
def test_target_cannot_execute_before_job_assignment(tmp_path, monkeypatch):
    import motte_harness.supervisor as module
    marker = tmp_path / 'executed'
    original = module._assign_process_to_job
    observed = []
    def delayed_assignment(job, pid):
        time.sleep(0.3)
        observed.append(marker.exists())
        return original(job, pid)
    monkeypatch.setattr(module, '_assign_process_to_job', delayed_assignment)
    process = SupervisedProcess([sys.executable, '-c',
        f'from pathlib import Path; Path({str(marker)!r}).touch()'])
    process.start()
    assert process.wait().exit_code == 0
    assert observed == [False]
    assert marker.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job ownership')
def test_assignment_failure_never_executes_target(tmp_path, monkeypatch):
    import motte_harness.supervisor as module
    marker = tmp_path / 'executed'
    monkeypatch.setattr(module, '_assign_process_to_job', lambda job, pid: False)
    process = SupervisedProcess([sys.executable, '-c',
        f'from pathlib import Path; Path({str(marker)!r}).touch()'])
    try:
        with pytest.raises(SupervisedProcessError, match='ownership'):
            process.start()
    finally:
        if process.pid:
            process.interrupt()
    assert not marker.exists()


def test_drain_timeout_freezes_callbacks_and_buffers():
    seen = []
    entered = threading.Event()
    def consumer(line):
        entered.set()
        seen.append(line)
        time.sleep(0.08)
    process = SupervisedProcess([sys.executable, '-c',
        "for i in range(100): print(i, flush=True)"], on_stdout=consumer,
        limits=SupervisedLimits(drain_timeout=0.01))
    process.start()
    assert entered.wait(5)
    result = process.wait()
    count = len(seen)
    size = len(process._stdout)
    time.sleep(0.3)
    assert len(seen) == count
    assert len(process._stdout) == size
    assert result.truncated


def test_send_line_roundtrip_and_lifecycle():
    process = SupervisedProcess([sys.executable, '-c',
        'import sys; print(sys.stdin.readline().strip(), flush=True)'],
        limits=SupervisedLimits(max_line_bytes=64), interactive_stdin=True)
    with pytest.raises(SupervisedProcessError):
        process.send_line('before')
    process.start()
    with pytest.raises(SupervisedProcessError):
        process.send_line('x' * 64)
    with pytest.raises(SupervisedProcessError):
        process.send_line('one\ntwo')
    process.send_line(json.dumps({'id': 1}))
    result = process.wait()
    assert json.loads(result.stdout) == {'id': 1}
    with pytest.raises(SupervisedProcessError):
        process.send_line('after')

def test_blocked_callback_cannot_block_worker_forever():
    entered = threading.Event()
    release = threading.Event()
    def blocked(line):
        entered.set()
        release.wait(5)
    process = SupervisedProcess([sys.executable, '-c', 'print("done", flush=True)'],
        on_stdout=blocked, limits=SupervisedLimits(drain_timeout=0.02))
    process.start()
    assert entered.wait(5)
    started = time.monotonic()
    try:
        result = process.wait()
    finally:
        release.set()
    assert time.monotonic() - started < 0.5
    assert result.truncated
    assert 'callback_unconfirmed' in result.detail

def test_blocked_stdin_send_is_bounded():
    process = SupervisedProcess([sys.executable, '-c', 'import time; time.sleep(30)'],
        limits=SupervisedLimits(max_line_bytes=1_000_000, idle_timeout=0.1), interactive_stdin=True)
    process.start()
    started = time.monotonic()
    with pytest.raises(SupervisedProcessError) as raised:
        process.send_line('x' * 900_000)
    assert raised.value.code == 'STDIN_TIMEOUT'
    assert time.monotonic() - started < 2
    assert process.wait().status == 'timeout'


def test_batch_stdin_is_eof_without_an_interactive_channel():
    process = SupervisedProcess([sys.executable, '-c',
        'import sys; sys.stdin.read(); print("done", flush=True)'],
        limits=SupervisedLimits(idle_timeout=0.3, total_timeout=0.8))
    process.start()
    result = process.wait()
    assert result.status == 'exited'
    assert result.stdout.strip() == 'done'

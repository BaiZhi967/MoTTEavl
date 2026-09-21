"""Pinned Codex 0.155.1 duplex RPC; one supervised reader, no durable callbacks."""
from __future__ import annotations

import json
import queue
import threading
from concurrent.futures import Future
from uuid import uuid4

from .supervisor import ProcessOutcome, SupervisedLimits, SupervisedProcess

PROTOCOL_SUBSET = ('initialize', 'initialized', 'account/login/start', 'thread/start', 'turn/start', 'turn/steer', 'turn/interrupt')
APPROVAL_METHODS = frozenset({'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'})


class RpcError(RuntimeError):
    def __init__(self, error):
        super().__init__(str(error))
        self.error = error


def typed_id(value):
    if type(value) not in (int, str) or (isinstance(value, str) and not value):
        raise RuntimeError('invalid RPC identity')
    return type(value).__name__, value


class CodexAppServerTransport:
    def __init__(self, argv: list[str], *, cwd: str, env: dict[str, str],
                 limits: SupervisedLimits | None = None, max_events: int = 1024):
        self.events: queue.Queue[dict] = queue.Queue(maxsize=max_events)
        self.pending: dict[tuple, Future] = {}
        self.incoming: dict[tuple, bool] = {}
        self.lock = threading.RLock()
        self.failure: BaseException | None = None
        self.closed = threading.Event()
        self.outcome: ProcessOutcome | None = None
        self.process = SupervisedProcess(argv, cwd=cwd, env=env, limits=limits,
            on_stdout=self._read_frame, name='codex-app-server', interactive_stdin=True)
        self.waiter: threading.Thread | None = None

    @property
    def pid(self):
        return self.process.pid

    def start(self):
        self.process.start()
        self.waiter = threading.Thread(target=self._wait, daemon=True)
        self.waiter.start()

    def _fail(self, error):
        with self.lock:
            self.failure = self.failure or error
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.failure)

    def _wait(self):
        try:
            self.outcome = self.process.wait()
        except Exception as error:  # noqa: BLE001 - wake waiters, never replay
            self._fail(error)
        finally:
            self._fail(RuntimeError('app-server stream closed'))
            self.closed.set()

    def _read_frame(self, line):
        try:
            frame = json.loads(line)
            if not isinstance(frame, dict):
                raise RuntimeError('RPC frame must be an object')
            with self.lock:
                if self.failure is not None:
                    return
                if 'method' in frame:
                    if not isinstance(frame['method'], str) or not isinstance(frame.get('params', {}), dict):
                        raise RuntimeError('invalid RPC method/params')
                    if 'id' in frame:
                        key = typed_id(frame['id'])
                        if key in self.incoming:
                            raise RuntimeError('duplicate server request identity')
                        self.incoming[key] = False
                    self.events.put_nowait(frame)
                else:
                    key = typed_id(frame.get('id'))
                    future = self.pending.get(key)
                    if future is None or future.done():
                        raise RuntimeError('unexpected or duplicate RPC response')
                    if ('result' in frame) == ('error' in frame):
                        raise RuntimeError('invalid RPC response shape')
                    if 'error' in frame:
                        future.set_exception(RpcError(frame['error']))
                    elif not isinstance(frame['result'], dict):
                        raise RuntimeError('RPC result must be an object')
                    else:
                        future.set_result(frame['result'])
        except (ValueError, RuntimeError, queue.Full) as error:
            reason = RuntimeError(f'app-server JSON/protocol failure: {error}')
            self._fail(reason)
            # Cleanup outside callback; waiting here would deadlock bounded drain.
            threading.Thread(target=self.process.interrupt, args=('protocol_error',), daemon=True).start()

    def _send(self, frame):
        if self.failure or self.closed.is_set():
            raise self.failure or RuntimeError('app-server closed')
        self.process.send_line(json.dumps(frame, ensure_ascii=False, separators=(',', ':')))

    def request(self, method, params, *, request_id=None) -> Future:
        request_id = request_id if request_id is not None else f'motte-{uuid4().hex}'
        key = typed_id(request_id)
        future: Future = Future()
        future.rpc_request_id = request_id
        with self.lock:
            if key in self.pending:
                raise RuntimeError('duplicate client request identity')
            if len(self.pending) >= 2048:
                raise RuntimeError('RPC request bound exceeded')
            self.pending[key] = future
        try:
            self._send({'id': request_id, 'method': method, 'params': params})
        except Exception as error:  # noqa: BLE001
            self._fail(error)
            raise
        return future

    def notify(self, method):
        self._send({'method': method})

    def respond(self, request_id, result):
        key = typed_id(request_id)
        with self.lock:
            if key not in self.incoming or self.incoming[key]:
                raise RuntimeError('server request missing/already answered')
            self.incoming[key] = True
        self._send({'id': request_id, 'result': result})

    def poll_event(self, timeout=0):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait_closed(self, timeout=None):
        if not self.closed.wait(timeout):
            raise TimeoutError('app-server cleanup deadline')
        if self.outcome is None:
            raise self.failure or RuntimeError('no process cleanup evidence')
        return self.outcome

    def close(self):
        if not self.closed.is_set():
            self.process.interrupt('session_closed')
        return self.wait_closed(20)

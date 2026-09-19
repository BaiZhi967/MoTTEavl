"""Local development supervisor (standard library only until the API factory runs)."""
from __future__ import annotations

from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
API_PORT = 8000
WEB_PORT = 5173
START_TIMEOUT = 60.0
POLL_INTERVAL = 0.1


def create_dev_app():
    """Identify this launch's health response without changing the production API."""
    from apps.api.app.main import app

    @app.middleware("http")
    async def identify_health(request, call_next):
        response = await call_next(request)
        if request.url.path == "/health":
            response.headers["X-Motte-Dev"] = os.environ["MOTTE_DEV_TOKEN"]
        return response

    return app


def check_port(port):
    with socket.socket() as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((HOST, port))
        except OSError as error:
            raise RuntimeError(
                f"{HOST}:{port} is unavailable; stop the existing listener yourself "
                "before running make dev (no existing server will be reused)."
            ) from error


class WindowsJob:
    """Kill owned descendants on close, including those whose parent already exited."""

    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w

        class BasicLimits(c.Structure):
            _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64),
                        ("flags", w.DWORD), ("min_ws", c.c_size_t),
                        ("max_ws", c.c_size_t), ("active", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD),
                        ("scheduling", w.DWORD)]

        class ExtendedLimits(c.Structure):
            _fields_ = [("basic", BasicLimits), ("io", c.c_uint64 * 6),
                        ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                        ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]

        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, c.byref(limits), c.sizeof(limits)
        ):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        import ctypes

        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Child:
    def __init__(self, name, command, env):
        self.name = name
        self.job = WindowsJob() if os.name == "nt" else None
        self.process = None
        try:
            if self.job:
                # The helper cannot spawn descendants until it belongs to our job.
                self.process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--child", *command],
                    cwd=ROOT, env=env, stdin=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                self.job.assign(self.process)
                self.process.stdin.write(b"go\n")
                self.process.stdin.close()
            else:
                self.process = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
        except BaseException:
            if self.process is not None:
                self.process.kill()
                self.process.wait()
            if self.job:
                self.job.close()
            raise
        print(f"[dev] Started {name} (pid {self.process.pid}).", flush=True)

    def check(self):
        code = self.process.poll()
        if code is not None:
            raise RuntimeError(f"{self.name} exited unexpectedly (exit code {code}); see output above.")

    def stop(self):
        if self.job:
            # Job membership survives the helper exiting; never kill by a stale parent PID.
            self.job.close()
        else:
            # 0-signal probe of OUR group never raises EPERM for a same-uid caller:
            # EPERM here means the pgid was recycled by an unrelated process group,
            # i.e. our group has already dissolved. Polling must stop, not crash
            # (and the trailing SIGKILL must never touch the recycled group).
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    self.process.poll()  # reap the group leader, if it has exited
                    os.killpg(self.process.pid, 0)
                    time.sleep(POLL_INTERVAL)
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # pgid recycled by another owner: our group is gone; nothing to signal.
                pass
        self.process.wait(timeout=5)


def probe_health(token, timeout):
    # Direct connection: no environment proxies and no redirects to other services.
    connection = HTTPConnection(HOST, API_PORT, timeout=timeout)
    try:
        connection.request("GET", "/health")
        with connection.getresponse() as response:
            if response.status != 200:
                return False, f"HTTP {response.status}"
            if response.headers.get("X-Motte-Dev") != token:
                return False, "health response belongs to a different server"
            if json.loads(response.read(1024)) != {"status": "ok"}:
                return False, "unexpected health response"
            return True, "ok"
    except (OSError, HTTPException, ValueError) as error:
        return False, str(error)
    finally:
        connection.close()


def wait_ready(child, token, timeout=START_TIMEOUT):
    url = f"http://{HOST}:{API_PORT}/health"
    deadline = time.monotonic() + timeout
    detail = "no response"
    while True:
        child.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"API not ready after {timeout:g}s at {url}: {detail}")
        ready, detail = probe_health(token, min(0.5, remaining))
        child.check()
        if ready:
            print(f"[dev] API ready at {url}.", flush=True)
            return
        time.sleep(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))


def run():
    children = []
    try:
        check_port(API_PORT)
        check_port(WEB_PORT)
        pnpm = shutil.which("pnpm")
        if not pnpm:
            raise RuntimeError("pnpm was not found on PATH; install the pinned prerequisites first.")
        token = uuid.uuid4().hex
        env = {**os.environ, "MOTTE_DEV_TOKEN": token}
        api = Child("API", [sys.executable, "-m", "uvicorn", "apps.dev:create_dev_app",
                            "--factory", "--reload", "--host", HOST, "--port", str(API_PORT)], env)
        children.append(api)
        wait_ready(api, token)
        check_port(WEB_PORT)
        api.check()
        children.append(Child("Web", [pnpm, "--dir", "apps/web", "dev", "--host", HOST,
                                      "--port", str(WEB_PORT), "--strictPort"], env))
        print(f"[dev] Web starting at http://{HOST}:{WEB_PORT}; Ctrl-C stops both servers.", flush=True)
        while True:
            for child in children:
                child.check()
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\n[dev] Stopping owned development processes.", flush=True)
        return 130
    except (OSError, RuntimeError) as error:
        print(f"[dev] ERROR: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        # A second Ctrl-C must not interrupt cleanup halfway through.
        stop_signals = [signal.SIGINT, signal.SIGTERM]
        if os.name == "nt":
            stop_signals.append(signal.SIGBREAK)
        previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in stop_signals}
        try:
            for child in reversed(children):
                try:
                    child.stop()
                except (OSError, subprocess.TimeoutExpired) as error:
                    print(f"[dev] Could not fully stop {child.name}: {error}", file=sys.stderr)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def interrupted(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        # EOF also aborts startup if the supervisor dies before assigning the job.
        if sys.stdin.buffer.readline() != b"go\n":
            sys.exit(1)
        sys.exit(subprocess.call(sys.argv[2:], stdin=subprocess.DEVNULL))
    signal.signal(signal.SIGTERM, interrupted)
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, interrupted)
    sys.exit(run())

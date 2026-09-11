"""Bounded subprocess transport shared by model requests and trusted test commands."""

import asyncio
import os
import signal
import subprocess


async def terminate(proc, job=None):
    if job is not None:
        job.close()
        await proc.wait()
        return
    if os.name == "nt":
        # taskkill /T includes child processes; no composed shell command.
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(proc.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        await killer.wait()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


async def run_process(argv, *, cwd=None, payload=None, timeout=120, env=None, output_limit=1_000_000):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **options)
    job = None
    if os.name == "nt":
        from .windows_job import ProcessJob
        try:
            job = ProcessJob(proc.pid)
        except BaseException:
            await terminate(proc)
            raise

    async def communicate():
        if payload:
            proc.stdin.write(payload)
            await proc.stdin.drain()
        proc.stdin.close()
        chunks = []
        size = 0
        while chunk := await proc.stdout.read(65536):
            size += len(chunk)
            if size > output_limit:
                raise ValueError("Process output exceeded limit")
            chunks.append(chunk)
        await proc.wait()
        return proc.returncode, b"".join(chunks).decode("utf-8", errors="replace")

    try:
        return await asyncio.wait_for(communicate(), timeout)
    finally:
        # Close the lifetime even when the parent exited: descendants must not
        # outlive a completed/failed test or provider request.
        await asyncio.shield(terminate(proc, job))

"""Operator-only CLI; no credentials accepted as arguments or printed."""

import argparse
from contextlib import contextmanager
from pathlib import Path
import asyncio
import json
import os
import sqlite3
import time
import uuid

from analysis_terminal import live_execution as live
from analysis_terminal.edgex_orders import EdgeXOrders, ExecutionError


async def run(args):
    if not Path(args.db).is_file():
        raise ExecutionError("EXISTING_DB_REQUIRED")
    config = live.Config.from_env()

    @contextmanager
    def db():
        conn = sqlite3.connect(args.db, timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    with db() as conn:
        live.initialize(conn, now_ms=int(time.time() * 1000))
        if args.action == "pause":
            live.pause(conn, "MANUAL_PAUSE")
    if args.action in {"check", "arm"}:
        if config.mode == "OFF" or config.errors():
            raise ExecutionError("CONFIGURATION_REQUIRED")
        adapter = EdgeXOrders(config)
        engine = live.Engine(db, config, adapter)
        try:
            if args.action == "arm":
                with db() as conn:
                    # Consume past READY setups once. Never execute an old signal.
                    previous = [
                        json.loads(x[0])
                        for x in conn.execute("SELECT payload FROM paper_signals")
                    ]
                await engine.arm(previous_ready=previous)
            else:
                # Connectivity check is always read-only, even in LIVE mode.
                await engine.preflight()
        finally:
            await adapter.close()
    with db() as conn:
        return live.report(conn, config, now_ms=int(time.time() * 1000))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("status", "check", "arm", "pause", "request")
    )
    parser.add_argument(
        "--operation", choices=("check", "arm", "pause"), default="check"
    )
    parser.add_argument(
        "--db", default=os.getenv("ANALYSIS_DB_PATH", "/data/analysis_terminal.db")
    )
    args = parser.parse_args()
    if args.action == "request":
        print("EDGEX_EXEC_CONTROL_REQUEST=" + args.operation + ":" + str(uuid.uuid4()))
        return
    try:
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))
    except ExecutionError as exc:
        print(json.dumps({"error": str(exc)}))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({"error": "OPERATOR_COMMAND_FAILED"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

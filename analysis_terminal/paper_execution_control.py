"""Local administrator control of the simulation only; never exposes a public write API."""
import argparse
import json
import sqlite3
import time
from analysis_terminal.paper_execution import report, set_pause


def main():
    parser = argparse.ArgumentParser(description="Pause/resume the PAPER_ONLY simulation")
    parser.add_argument("action",choices=("status","pause","resume"))
    parser.add_argument("--db",default="/data/analysis_terminal.db")
    args = parser.parse_args()
    # Do not accidentally create a database when a path is wrong.
    from pathlib import Path
    if not Path(args.db).is_file():
        parser.error("Existing analysis DB is required")
    with sqlite3.connect(args.db) as conn:
        conn.execute("BEGIN IMMEDIATE" if args.action != "status" else "BEGIN")
        now = int(time.time()*1000)
        if args.action != "status":
            set_pause(conn,paused=args.action == "pause",now_ms=now)
        print(json.dumps(report(conn,now_ms=now,limit=1)["account"],ensure_ascii=False))


if __name__ == "__main__":
    main()

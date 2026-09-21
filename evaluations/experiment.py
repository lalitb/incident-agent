import argparse
import json
import os
import signal
import socket
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from urllib.request import Request, urlopen

from agent.run_records import save_json
from demo.load import percentile, worker


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--python", required=True)
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))

    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    truth = {"experiment": tag, "status": "running", "window": {"start": now()},
             "concurrency": 10, "db_work_seconds": 0.2, "conditions": [],
             "notes": ["Dedicated demo port; existing app and Docker services were not changed.",
                       "Closed-loop load: equal client concurrency, not equal request throughput.",
                       "Other historical gauge series may still be exported by the Collector."]}
    destination = args.directory / "ground_truth.json"
    save_json(destination, truth)
    url = f"http://127.0.0.1:{args.port}"

    for phase, pool, version in [("baseline", 10, "v1"), ("incident", 2, "v2")]:
        condition = {"phase": phase, "pool_size": pool, "service_version": f"eval-{tag}-{version}",
                     "launch_requested_at": now()}
        truth["conditions"].append(condition)
        save_json(destination, truth)
        environment = {**os.environ, "POOL_SIZE": str(pool), "DB_WORK_SECONDS": "0.2",
                       "SERVICE_VERSION": condition["service_version"]}
        process = subprocess.Popen([
            args.python, "-m", "uvicorn", "--app-dir", "demo", "checkout:app",
            "--host", "127.0.0.1", "--port", str(args.port), "--no-access-log",
        ], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(40):
                if process.poll() is not None:
                    raise RuntimeError("Demo exited before becoming ready")
                try:
                    with urlopen(url + "/openapi.json", timeout=1):
                        break
                except OSError:
                    time.sleep(0.5)
            else:
                raise RuntimeError("Demo readiness timeout")
            with urlopen(Request(url + "/checkout", data=b"", method="POST"), timeout=10) as response:
                warmup = json.load(response)
            assert warmup["pool_size"] == pool
            condition["ready_at"] = now()
            condition["verified_pool_size"] = warmup["pool_size"]
            save_json(destination, truth)
            time.sleep(10)
            condition["traffic_start"] = now()
            deadline = time.monotonic() + args.seconds
            print(f"{phase}: pool={pool}; 10 clients for {args.seconds}s; {condition['traffic_start']}", flush=True)
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(worker, url + "/checkout", deadline) for _ in range(10)]
                results = [result for future in futures for result in future.result()]
            condition["traffic_end"] = now()
            durations = sorted(result["seconds"] for result in results)
            condition["observed"] = {
                "requests": len(results), "statuses": dict(Counter(r["status"] for r in results)),
                "mean_ms": round(fmean(durations) * 1000, 3),
                "p50_ms": round(percentile(durations, 0.5) * 1000, 3),
                "p95_ms": round(percentile(durations, 0.95) * 1000, 3),
                "sample_trace_ids": [r["trace_id"] for r in results[:3]],
            }
            save_json(destination, truth)
            print(f"{phase} observed: {condition['observed']['requests']} requests; mean {condition['observed']['mean_ms']} ms", flush=True)
            time.sleep(10)
        finally:
            condition["shutdown_requested_at"] = now()
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            condition["stopped_at"] = now()
            condition["exit_code"] = process.returncode
            save_json(destination, truth)

    time.sleep(10)
    truth["window"]["end"] = now()
    truth["status"] = "completed"
    save_json(destination, truth)
    print("Experiment complete:", json.dumps(truth["window"]), flush=True)


if __name__ == "__main__":
    main()

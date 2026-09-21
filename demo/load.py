import argparse
import json
import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def worker(url, deadline):
    results = []

    while time.monotonic() < deadline:
        started = time.perf_counter()
        trace_id = None

        try:
            request = Request(url, data=b"", method="POST")
            with urlopen(request, timeout=15) as response:
                status = str(response.status)
                body = json.load(response)
                trace_id = body.get("trace_id")

        except HTTPError as exc:
            status = str(exc.code)
            exc.close()

        except (URLError, TimeoutError, OSError):
            status = "connection_error"

        results.append({
            "seconds": time.perf_counter() - started,
            "status": status,
            "trace_id": trace_id,
        })

    return results


def percentile(values, fraction):
    index = max(0, math.ceil(len(values) * fraction) - 1)
    return values[index]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000/checkout",
    )
    args = parser.parse_args()

    if args.concurrency < 1 or args.seconds < 1:
        parser.error("concurrency and seconds must be positive")

    print("Started:", datetime.now(timezone.utc).isoformat())
    print(
        f"Running {args.concurrency} workers "
        f"for {args.seconds} seconds...",
        flush=True,
    )

    started = time.monotonic()
    deadline = started + args.seconds

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(worker, args.url, deadline)
            for _ in range(args.concurrency)
        ]
        results = [
            result
            for future in futures
            for result in future.result()
        ]

    elapsed = time.monotonic() - started
    latencies = sorted(item["seconds"] for item in results)
    statuses = Counter(item["status"] for item in results)

    print("Finished:", datetime.now(timezone.utc).isoformat())
    print(f"Requests: {len(results)}")
    print(f"Throughput: {len(results) / elapsed:.1f} requests/sec")
    print(f"Statuses: {dict(statuses)}")

    for label, fraction in [("p50", 0.50), ("p95", 0.95), ("p99", 0.99)]:
        print(f"{label}: {percentile(latencies, fraction) * 1000:.0f} ms")

    successful = [
        item for item in results
        if item["status"] == "200" and item["trace_id"]
    ]
    if successful:
        slowest = max(successful, key=lambda item: item["seconds"])
        print("Slowest successful trace:", slowest["trace_id"])


if __name__ == "__main__":
    main()
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .gateway import redact
from .llm import load_environment
from .run_records import save_json
from .schemas import IncidentReport
from .validate_report import validate_report


def review_action(run_directory, choice="not_requested"):
    if choice not in {"not_requested", "approved", "rejected"}:
        raise ValueError("Unknown approval choice")
    saved = redact(json.loads((run_directory / "report.json").read_text()))[0]
    evidence = redact(json.loads((run_directory / "evidence.json").read_text()))[0]
    report = IncidentReport.model_validate(saved["report"])
    validate_report(report, evidence["evidence"])
    if not report.recommended_next_steps:
        raise ValueError("The report has no proposed next step")

    record = {
        "proposed_action": report.recommended_next_steps[0],
        "status": choice,
        "simulation_only": True,
        "simulated_result": ("Approval recorded; simulation completed. No external action taken."
                             if choice == "approved" else None),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    # The action remains text. Approval can only write this local record.
    record = save_json(run_directory / "remediation.json", record)
    controller_file = run_directory / "controller.json"
    if controller_file.exists():
        state = json.loads(controller_file.read_text())
        state["remediation_status"] = choice
        save_json(controller_file, state)
    return record


def main(argv=None):
    load_environment()
    parser = argparse.ArgumentParser(description="Review a proposal; simulate approval only")
    parser.add_argument("run_directory", type=Path)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--approve", action="store_true", help="Explicitly approve the local simulation")
    choice.add_argument("--reject", action="store_true")
    args = parser.parse_args(argv)
    try:
        existing = args.run_directory / "remediation.json"
        record = (redact(json.loads(existing.read_text()))[0] if existing.exists()
                  else review_action(args.run_directory))
        print(json.dumps(record, indent=2))
        if args.approve or args.reject:
            status = "approved" if args.approve else "rejected"
            record = review_action(args.run_directory, status)
            print(json.dumps(record, indent=2))
    except (OSError, ValueError, KeyError) as exc:
        detail = redact(str(exc))[0][:2000]
        raise SystemExit(f"Cannot review proposal: {detail}") from None


if __name__ == "__main__":
    main()

"""Run case files through the engine: python -m dlra.cli case.json [...]

A case file is {"bundle": CaseBundle, "diagnosis": Diagnosis, "now": ISO time with offset}.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from dlra.engine import decide, describe
from dlra.models import CaseBundle, Diagnosis
from dlra.policy import load_policy


def main(paths: list[str]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # the Windows console cannot print ₹ by default
    policy = load_policy()
    for path in paths:
        case = json.loads(Path(path).read_text(encoding="utf-8"))
        result = decide(
            CaseBundle.model_validate(case["bundle"]), Diagnosis.model_validate(case["diagnosis"]),
            policy, datetime.fromisoformat(case["now"]),
        )
        print(f"{path}\n{describe(result)}\n")


if __name__ == "__main__":
    main(sys.argv[1:])

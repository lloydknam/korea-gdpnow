"""data/alerts.json → GitHub 이슈 하나로 알림(휴대폰 GitHub 앱·메일로 통지).

미수집 항목이 있으면 '데이터 미수집' 이슈를 열거나 내용을 갱신, 모두 해소되면 자동으로 닫음.
GitHub Actions의 gh CLI + GITHUB_TOKEN(issues: write) 사용. 실패해도 워크플로우는 계속.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL = "data-alert"
TITLE = "[자동] 데이터 미수집 알림"


def gh(*args, check=True) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def body(alerts: list[dict], repo: str) -> str:
    lines = ["발표일이 지났는데 아직 반영되지 않은 자료입니다. 반영되면 이 이슈는 자동으로 닫힙니다.", ""]
    for a in alerts:
        est = " (예상일)" if a.get("est") else ""
        lines.append(f"- **{a['name']}** {a['ref']} — {a['org']}, {a['date']} 발표{est}, {a['days']}일 경과")
        lines.append(f"  - 조치: {a['fix']}")
        if a.get("template"):
            lines.append(f"  - [CSV 편집하기](https://github.com/{repo}/edit/main/data/customs_flash.csv) · 맨 아래 한 줄 형식:")
            lines.append(f"    ```\n    {a['template']}\n    ```")
    lines += ["", "추정은 멈추지 않음: 빠진 자료는 모형이 과거 패턴으로 메우고, 자료가 들어오면 다음 실행에 반영됩니다."]
    return "\n".join(lines)


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "lloydknam/korea-gdpnow")
    try:
        alerts = json.loads((ROOT / "data" / "alerts.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        alerts = []
    try:
        gh("label", "create", LABEL, "--color", "B60205", "--description", "자료 미수집 자동 알림", "--force", check=False)
        found = gh("issue", "list", "--label", LABEL, "--state", "open", "--json", "number", "-q", ".[].number")
        num = found.splitlines()[0] if found else None
        if alerts:
            b = body(alerts, repo)
            if num:
                gh("issue", "edit", num, "--body", b)
                print(f"이슈 #{num} 갱신 ({len(alerts)}건)")
            else:
                print(gh("issue", "create", "--title", TITLE, "--label", LABEL, "--body", b))
        elif num:
            gh("issue", "close", num, "--comment", "모든 자료 반영 완료 — 자동 종료")
            print(f"이슈 #{num} 종료")
        else:
            print("미수집 없음")
    except Exception as e:  # noqa: BLE001
        print(f"::warning::알림 이슈 처리 실패 — {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

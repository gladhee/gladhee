import json
import os
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from xml.etree import ElementTree

from collect_work_languages import refresh_public

ROOT = Path(__file__).resolve().parents[1]
WEEKS = 12
DAYS = WEEKS * 7
WIDTH, HEIGHT = 354, 158
START_MARKER = "<!-- profile-stats:start -->"
END_MARKER = "<!-- profile-stats:end -->"
QUERY = """
query($owner: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $owner) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar { weeks { contributionDays { date contributionCount } } }
    }
  }
}
"""


def github(*args):
    return json.loads(subprocess.check_output(["gh", "api", *args], text=True))


def public_activity(owner, start, end):
    reply = github("graphql", "-f", "query=" + QUERY, "-f", f"owner={owner}",
                   "-f", f"from={start}T00:00:00Z", "-f", f"to={end}T23:59:59Z")
    calendar = reply["data"]["user"]["contributionsCollection"]["contributionCalendar"]
    counts = {day["date"]:day["contributionCount"] for week in calendar["weeks"] for day in week["contributionDays"]}
    return [counts[(start + timedelta(days=i)).isoformat()] for i in range(DAYS)]


def text(x, y, value, style="label", anchor="start"):
    return f'<text x="{x}" y="{y}" class="{style}" text-anchor="{anchor}">{escape(str(value))}</text>'


def card(title, description, elements):
    elements = "\n  ".join(elements)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img">
  <title>{escape(title)}</title>
  <desc>{escape(description)}</desc>
  <style>
    text {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
    .title {{ fill: #f0f4f8; font-size: 17px; font-weight: 650; }}
    .number {{ fill: #f0f4f8; font-size: 24px; font-weight: 650; font-variant-numeric: tabular-nums; }}
    .label {{ fill: #dbe1e8; font-size: 12.5px; }}
    .muted {{ fill: #aab7c4; font-size: 11px; }}
    .percent {{ fill: #c2cdd7; font-size: 12px; font-variant-numeric: tabular-nums; }}
  </style>
  <rect x=".5" y=".5" width="{WIDTH-1}" height="{HEIGHT-1}" rx="9" fill="#161c24" stroke="#37414d"/>
  {elements}
</svg>
"""


def activity_card(daily, start, end):
    total, active = sum(daily), sum(n > 0 for n in daily)
    weekly = [sum(daily[i:i+7]) for i in range(0, DAYS, 7)]
    period = f"{start:%Y.%m.%d} – {end:%Y.%m.%d}"
    description = f"GitHub Activity · {period} · 기여 {total:,}건, 활동일 {active}일. 7일 단위 기여 합계 " + ", ".join(map(str, weekly))
    elements = [
        text(16, 26, "GitHub Activity", "title"), text(338, 25, "최근 12주", "muted", "end"),
        text(16, 58, f"{total:,}", "number"), text(16, 75, "공개 기여", "muted"),
        text(112, 58, f"{active}", "number"), text(112, 75, "활동일 / 84일", "muted"),
        '<path d="M16 127.5H338" stroke="#44515d"/>',
    ]
    peak = max(weekly) or 1
    for i, amount in enumerate(weekly):
        height = max(2, round(40 * amount / peak))
        color = "#64d8a2" if amount == max(weekly) else "#35a779" if amount else "#50606d"
        day = start + timedelta(days=7*i)
        title = f"{day:%m.%d}–{day + timedelta(days=6):%m.%d} · {amount}건"
        elements.append(f'<rect x="{19+27*i}" y="{127-height}" width="18" height="{height}" rx="2" fill="{color}"><title>{title}</title></rect>')
    elements.extend([text(16, 146, period, "muted"), text(338, 146, "7일 단위", "muted", "end")])
    return card("GitHub Activity", description, elements), description


def share(value, total):
    pct = 100 * value / total
    if 0 < pct < 0.01:
        return "<0.01%"
    return f"{pct:.2f}%" if pct < 1 else f"{pct:.1f}%"


def language_card(totals, colors, as_of):
    if any(type(n) is not int or n < 0 for n in totals.values()):
        raise ValueError("Invalid source line totals.")
    ranked = sorted(((name, n) for name, n in totals.items() if n), key=lambda row: (-row[1], row[0]))
    overall = sum(n for _, n in ranked)
    if not overall:
        raise ValueError("No attributed lines to publish.")
    featured = {name for name, _ in ranked[:5]} | {"C", "C++"}
    rows = [(name, n) for name, n in ranked if name in featured]
    remaining = overall - sum(n for _, n in rows)
    if remaining:
        rows.append(("기타", remaining))
    stamp = as_of.strftime("%Y.%m.%d")
    desc = (
        "Most Used Languages · 개인·회사 저장소의 모든 원격 브랜치에서 본인 작성자 커밋이 "
        "추가·수정한 비어 있지 않은 소스 줄 비율. "
        f"비공개 개인·회사 집계 {stamp}. "
        + ", ".join(f"{name} {share(n, overall)}" for name, n in ranked)
    )
    elements = [
        text(16, 26, "Most Used Languages", "title"),
        f'<metadata id="authored-lines-as-of">{as_of.isoformat()}</metadata>',
        '<defs><clipPath id="bar"><rect x="16" y="41" width="322" height="9" rx="4.5"/></clipPath></defs>',
    ]
    x = 16.0
    for i, (name, n) in enumerate(rows):
        color = colors.get(name)
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            color = "#8192a4"
        width = 322 * n / overall
        elements.append(
            f'<rect clip-path="url(#bar)" x="{x:.3f}" y="41" width="{width:.3f}" height="9" fill="{color}"/>'
        )
        x += width
        left, y = 19 + 165 * (i % 2), 70 + 19 * (i // 2)
        elements.append(
            f'<circle cx="{left}" cy="{y - 4}" r="4" fill="{color}" stroke="#cbd5df" stroke-opacity=".32"/>'
        )
        elements.extend(
            [text(left + 10, y, name), text(left + 145, y, share(n, overall), "percent", "end")]
        )
    elements.append(text(16, 146, "개인·회사 저장소 · 본인 커밋의 추가·수정 줄", "muted"))
    parts = [
        "<details>",
        "<summary>언어별 기여 비율 전체 보기</summary>",
        "",
        ("모든 원격 브랜치에서 본인 작성자 커밋이 추가·수정한 소스 줄을 집계합니다. "
         f"개인 공개 저장소는 매일 갱신하며, 비공개 개인·회사 저장소 집계일은 {stamp}입니다."),
        "",
        "| 언어 | 비율 |",
        "|---|---:|",
    ]
    parts.extend(f"| {escape(name)} | {escape(share(n, overall))} |" for name, n in ranked)
    parts.extend(["", "</details>"])
    return card("Most Used Languages", desc, elements), desc, "\n".join(parts)


def verified_languages(owner, raw, path, old_section):
    snapshot = json.loads(raw) if raw else {}
    if snapshot.get("version") == 3:
        totals, colors = refresh_public(owner, snapshot)
        return language_card(totals, colors, date.fromisoformat(snapshot["as_of"]))
    previous = path.read_text()
    root = ElementTree.fromstring(previous)
    ns = "{http://www.w3.org/2000/svg}"
    if root.find(f"{ns}metadata[@id='authored-lines-as-of']") is None:
        raise ValueError("A verified commit-based language snapshot is required.")
    details = old_section.partition("</p>")[2].strip()
    print("Preserving the last verified authored-line statistics.")
    return previous, root.findtext(ns + "desc"), details


def main():
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "gladhee")
    end = datetime.now(timezone(timedelta(hours=9))).date() - timedelta(days=1)
    start = end - timedelta(days=DAYS - 1)
    activity, activity_alt = activity_card(public_activity(owner, start, end), start, end)
    readme = ROOT / "README.md"
    source = readme.read_text()
    if source.count(START_MARKER) != 1 or source.count(END_MARKER) != 1:
        raise ValueError("Expected one GitHub stats section.")
    before, _, rest = source.partition(START_MARKER)
    old_section, marker, after = rest.partition(END_MARKER)
    if not marker:
        raise ValueError("The end marker must follow the start marker.")
    path = ROOT / "assets/languages.svg"
    languages, alt, details = verified_languages(
        owner, os.environ.get("WORK_LANGUAGE_SNAPSHOT"), path, old_section
    )
    activity_url = f"https://github.com/{owner}?tab=overview&amp;from={start}&amp;to={end}"
    section = (
        f'<p>\n  <a href="{activity_url}"><img src="./assets/activity.svg" width="346" alt="{escape(activity_alt, quote=True)}"></a>\n'
        f'  <a href="./assets/languages.svg"><img src="./assets/languages.svg" width="346" alt="{escape(alt, quote=True)}"></a>\n</p>\n\n{details}'
    )
    (ROOT / "assets/activity.svg").write_text(activity)
    path.write_text(languages)
    readme.write_text(before + START_MARKER + "\n" + section + "\n" + END_MARKER + after)
    print(activity_alt, alt, sep="\n")


if __name__ == "__main__":
    main()

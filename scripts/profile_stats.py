from collections import Counter
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from html import escape
import json
import os
from pathlib import Path
import re
import subprocess
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
WEEKS = 12
DAYS = WEEKS * 7
WIDTH, HEIGHT = 354, 158
START_MARKER = "<!-- profile-stats:start -->"
END_MARKER = "<!-- profile-stats:end -->"
QUERY = """
query($owner: String!, $from: DateTime!, $to: DateTime!, $endCursor: String) {
  user(login: $owner) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar { weeks { contributionDays { date contributionCount } } }
    }
    repositories(first: 100, after: $endCursor, ownerAffiliations: [OWNER], privacy: PUBLIC) {
      nodes {
        id name
        languages(first: 100, orderBy: {field: SIZE, direction: DESC}) {
          totalSize
          edges { size node { name color } }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def github(*args):
    return json.loads(subprocess.check_output(["gh", "api", *args], text=True))


def repository_key(node_id):
    return sha256(node_id.encode("utf-8")).hexdigest()


def public_data(owner, start, end):
    pages = github(
        "graphql", "--paginate", "--slurp", "-f", "query=" + QUERY,
        "-f", f"owner={owner}", "-f", f"from={start}T00:00:00Z",
        "-f", f"to={end}T23:59:59Z",
    )
    calendar = pages[0]["data"]["user"]["contributionsCollection"]["contributionCalendar"]
    by_date = {d["date"]: d["contributionCount"] for w in calendar["weeks"] for d in w["contributionDays"]}
    # A missing day is an incomplete API result, not a day with zero activity.
    daily = [by_date[(start + timedelta(days=i)).isoformat()] for i in range(DAYS)]

    by_repo, colors = {}, {}
    for page in pages:
        for repo in page["data"]["user"]["repositories"]["nodes"]:
            languages = repo["languages"]
            sizes = {e["node"]["name"]: e["size"] for e in languages["edges"]}
            colors.update({e["node"]["name"]: e["node"]["color"] for e in languages["edges"]})
            if sum(sizes.values()) != languages["totalSize"]:
                sizes = github(f"repos/{owner}/{repo['name']}/languages")
            by_repo[repository_key(repo["id"])] = sizes
    return daily, by_repo, colors


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


def language_card(totals, colors, snapshot_date=None):
    overall = sum(totals.values())
    top = sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:5]
    rows = [(name, size, colors.get(name) or "#7891a6") for name, size in top]
    remaining = overall - sum(size for _, size, _ in rows)
    if remaining:
        rows.append(("기타", remaining, "#8192a4"))
    description = "Most Used Languages · 포크와 프로필을 포함한 개인 공개 저장소의 코드 바이트 비율. "
    footer = "공개 개인 저장소 · 코드 바이트 기준"
    elements = [text(16, 26, "Most Used Languages", "title"),
                '<defs><clipPath id="bar"><rect x="16" y="43" width="322" height="10" rx="5"/></clipPath></defs>']
    if snapshot_date is not None:
        description = ("Most Used Languages · 개인 소유 저장소 전체(공개·비공개, 포크·프로필 포함)와 "
                       "기여한 회사 저장소의 코드 바이트 비율. "
                       f"비공개 개인·회사 집계 {snapshot_date:%Y.%m.%d}. ")
        footer = "개인 전체 + 기여한 회사 저장소 · 코드 바이트 기준"
        elements.append(f'<metadata id="work-language-as-of">{snapshot_date.isoformat()}</metadata>')
    if not rows:
        description += "집계할 코드가 없습니다."
        elements.append(text(16, 85, "집계할 공개 코드가 없습니다."))
    else:
        description += ", ".join(f"{name} {100 * size / overall:.1f}%" for name, size, _ in rows)
        x = 16.0
        for i, (name, size, color) in enumerate(rows):
            width = 322 * size / overall
            elements.append(f'<rect clip-path="url(#bar)" x="{x:.3f}" y="43" width="{width:.3f}" height="10" fill="{color}"/>')
            x += width
            col, row = i % 2, i // 2
            left, y = 19 + 165*col, 76 + 22*row
            elements += [f'<circle cx="{left}" cy="{y-4}" r="4" fill="{color}" stroke="#cbd5df" stroke-opacity=".32"/>',
                         text(left+10, y, name), text(left+145, y, f"{100 * size / overall:.1f}%", "percent", "end")]
    elements.append(text(16, 146, footer, "muted"))
    return card("Most Used Languages", description, elements), description


def sum_languages(repositories):
    total = Counter()
    for sizes in repositories.values():
        total.update(sizes)
    return total


def combined_language_card(public_repos, public_colors, raw_snapshot, previous_path):
    snapshot = json.loads(raw_snapshot) if raw_snapshot else {}
    if snapshot.get("version") != 2:
        # Preserve the scope described by the last card until a complete census is available.
        if previous_path.is_file():
            previous = previous_path.read_text()
            root = ElementTree.fromstring(previous)
            svg_ns = "{http://www.w3.org/2000/svg}"
            if root.find(f"{svg_ns}metadata[@id='work-language-as-of']") is not None:
                print("Using the last verified combined language card.")
                return previous, root.findtext(f"{svg_ns}desc")
        return language_card(sum_languages(public_repos), public_colors)

    as_of = date.fromisoformat(snapshot["as_of"])
    supplement = snapshot["bytes"]
    baseline = snapshot["public_baseline"]
    covered = set(snapshot["covered"])
    if covered.intersection(baseline) or not all(
        isinstance(sizes, dict) and all(
            isinstance(name, str) and name and type(amount) is int and amount > 0
            for name, amount in sizes.items()
        ) for sizes in [supplement, *baseline.values()]
    ):
        raise ValueError("Invalid repository language snapshot.")
    colors = {name: color for name, color in snapshot.get("colors", {}).items()
              if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color)}
    colors.update({name: color for name, color in public_colors.items() if color})
    # Keep the dated value for newly hidden repos. A newly public repo already in
    # the supplement stays there until the next census to avoid counting it twice.
    personal = dict(baseline)
    personal.update((key, sizes) for key, sizes in public_repos.items() if key not in covered)
    combined = sum_languages(personal)
    combined.update(supplement)
    return language_card(combined, colors, as_of)


def main():
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "gladhee")
    end = datetime.now(timezone(timedelta(hours=9))).date() - timedelta(days=1)
    start = end - timedelta(days=DAYS - 1)
    daily, public_repos, colors = public_data(owner, start, end)
    activity, activity_alt = activity_card(daily, start, end)
    languages_path = ROOT / "assets/languages.svg"
    languages, languages_alt = combined_language_card(public_repos, colors, os.environ.get("WORK_LANGUAGE_SNAPSHOT"), languages_path)

    readme = ROOT / "README.md"
    source = readme.read_text()
    if source.count(START_MARKER) != 1 or source.count(END_MARKER) != 1:
        raise ValueError("Expected one GitHub stats section in README.md")
    before, _, rest = source.partition(START_MARKER)
    _, marker, after = rest.partition(END_MARKER)
    if not marker:
        raise ValueError("The end marker must follow the start marker")
    activity_url = f"https://github.com/{owner}?tab=overview&amp;from={start}&amp;to={end}"
    links = (f'<p>\n  <a href="{activity_url}"><img src="./assets/activity.svg" width="346" alt="{escape(activity_alt, quote=True)}"></a>\n'
             f'  <a href="./assets/languages.svg"><img src="./assets/languages.svg" width="346" alt="{escape(languages_alt, quote=True)}"></a>\n</p>')
    (ROOT / "assets/activity.svg").write_text(activity)
    languages_path.write_text(languages)
    readme.write_text(before + START_MARKER + "\n" + links + "\n" + END_MARKER + after)
    print(activity_alt, languages_alt, sep="\n")


if __name__ == "__main__":
    main()

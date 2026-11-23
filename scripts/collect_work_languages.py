"""Build a private aggregate from the contributor’s authenticated Git histories."""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import authored_lines as authored

INVENTORY = """
query($org:String!, $author:ID!, $after:String) {
  organization(login:$org) {
    repositories(first:20, after:$after) {
      nodes {
        id name isEmpty
        refs(refPrefix:"refs/heads/", first:1) { totalCount }
        defaultBranchRef { target { ... on Commit { history(first:1, author:{id:$author}) { nodes { oid } } } } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
MERGED_PRS = """
query($filter:String!, $after:String) {
  search(query:$filter, type:ISSUE, first:100, after:$after) {
    issueCount nodes { ... on PullRequest { repository { id } } }
    pageInfo { hasNextPage endCursor }
  }
}
"""
BRANCHES = """
query($id:ID!, $author:ID!, $after:String) {
  node(id:$id) { ... on Repository {
    refs(refPrefix:"refs/heads/", first:20, after:$after) {
      nodes { target { ... on Commit { history(first:1, author:{id:$author}) { nodes { oid } } } } }
      pageInfo { hasNextPage endCursor }
    }
  } }
}
"""


def api(args, payload=None):
    request = json.dumps(payload) if payload is not None else None
    proc = subprocess.run(["gh", "api", *args], input=request, text=True, capture_output=True, check=False)
    if proc.returncode:
        # Repository paths and API diagnostics can contain private identifiers.
        raise RuntimeError(
            "GitHub metadata request failed. Check the CLI login and organization access."
        )
    response = json.loads(proc.stdout)
    if isinstance(response, dict) and response.get("errors"):
        raise RuntimeError("GitHub returned incomplete metadata. The snapshot was not produced.")
    return response


def graph(query, **variables):
    return api(["graphql", "--input", "-"], {"query": query, "variables": variables})["data"]


def connections(query, path, **variables):
    cursor = None
    while True:
        page = graph(query, **variables, after=cursor)
        for key in path:
            page = page[key]
        yield page
        if not page["pageInfo"]["hasNextPage"]:
            return
        cursor = page["pageInfo"]["endCursor"]


def has_authored_commit(ref):
    return bool(ref and (ref.get("target") or {}).get("history", {}).get("nodes"))


def contributing_repositories(org, viewer):
    inventory = {}
    for page in connections(INVENTORY, ("organization", "repositories"), org=org, author=viewer["id"]):
        inventory.update((row["id"], row) for row in page["nodes"] if not row["isEmpty"])
    selected = {key for key, repo in inventory.items() if has_authored_commit(repo["defaultBranchRef"])}

    terms = f"is:pr is:merged author:{viewer['login']}"
    for page in connections(MERGED_PRS, ("search",), filter=f"org:{org} {terms}"):
        selected.update(pr["repository"]["id"] for pr in page["nodes"] if pr and pr.get("repository"))
        if page["issueCount"] > 1000:
            # Search returns at most 1,000 matches. An individual repository only needs one hit.
            for key in inventory.keys() - selected:
                query = f"repo:{org}/{inventory[key]['name']} {terms}"
                if graph(MERGED_PRS, filter=query)["search"]["issueCount"]:
                    selected.add(key)
            break

    def branch_match(key):
        for page in connections(BRANCHES, ("node", "refs"), id=key, author=viewer["id"]):
            if any(has_authored_commit(ref) for ref in page["nodes"]):
                return key
        return None

    remaining = [
        key
        for key in inventory.keys() - selected
        if inventory[key]["refs"]["totalCount"] > 1 or not inventory[key]["defaultBranchRef"]
    ]
    with ThreadPoolExecutor(max_workers=3) as pool:
        selected.update(key for key in pool.map(branch_match, remaining) if key)
    return {key: inventory[key]["name"] for key in inventory if key in selected}


def owned_repositories(login):
    pages = api(["--paginate", "--slurp", "user/repos?affiliation=owner&visibility=all&per_page=100"])
    return {
        repo["node_id"]: repo
        for page in pages
        for repo in page
        if repo["owner"]["login"].casefold() == login.casefold()
    }


def repo_key(node_id):
    return sha256(node_id.encode()).hexdigest()


def commit_key(oid):
    # Opaque membership keys let a public refresh skip commits already counted privately.
    return sha256(bytes.fromhex(oid)).digest()[:8]


def refs_key(heads):
    return sha256("\n".join(sorted(set(heads))).encode()).hexdigest()


def fork_details(repo):
    if repo["fork"]:
        detail = api(["repos/" + repo["full_name"]])
        return {"parent": detail["parent"]["full_name"], "created_at": detail["created_at"]}
    return None


def clone_histories(repos, folder):
    def clone(item):
        key, repo = item
        dest = Path(folder) / key
        command = [
            *authored.GIT,
            "clone",
            "--bare",
            "--quiet",
            "--no-tags",
            "--filter=blob:limit=384k",
            "https://github.com/" + repo["full_name"] + ".git",
            str(dest),
        ]
        result = subprocess.run(
            command, check=False, capture_output=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        )
        if result.returncode:
            raise RuntimeError(
                "A repository history could not be read. No partial aggregate will be published."
            )
        return key, dest

    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(clone, repos.items()))


def graphics_origins(full_name):
    data = json.loads(Path(__file__).with_name("graphics_origins.json").read_text())
    return {path: row["blob"] for path, row in data.get(full_name, {}).items()}


def snapshot_records(repos, mirrors, identities, private_keys=frozenset()):
    def prepare(item):
        key, repo = item
        inherited = authored.inherited_commits(mirrors[key], fork_details(repo))
        return key, authored.author_commits(mirrors[key], identities, inherited)

    with ThreadPoolExecutor(max_workers=4) as pool:
        histories = dict(pool.map(prepare, repos.items()))
    seen, omitted = set(), {}
    for key, repo in sorted(
        repos.items(),
        key=lambda item: (
            item[1]["public_personal"],
            item[1]["fork"],
            item[1]["size"],
            item[1]["full_name"].casefold(),
        ),
    ):
        candidates, _ = histories[key]
        omitted[key] = (seen & candidates) | {
            oid for oid in candidates if commit_key(oid) in private_keys
        }
        seen.update(candidates)

    def measure(item):
        key, repo = item
        mirror = mirrors[key]
        result = authored.calculate(
            mirror,
            identities,
            repo["personal"],
            baseline=graphics_origins(repo["full_name"]),
            omit_commits=omitted[key],
            author_history=histories[key],
        )
        result["heads"] = refs_key(
            authored.git(mirror, "for-each-ref", "--format=%(objectname)", "refs/heads")
            .decode()
            .splitlines()
        )
        return key, result

    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(measure, repos.items()))


def make_snapshot(repos, records, identities, as_of):
    private_lines, colors, used = Counter(), {}, set()
    covered, public = [], {}
    for key, result in records.items():
        colors.update(result["colors"])
        if repos[key]["public_personal"]:
            public[key] = {"heads": result["heads"], "lines": result["lines"]}
        else:
            private_lines.update(result["lines"])
            used.update(result["commits"])
            covered.append(key)
    keys = sorted({commit_key(oid) for oid in used})
    if len(keys) != len(used):
        raise RuntimeError("Private commit membership keys collided.")
    return {
        "version": 3,
        "as_of": as_of,
        "identities": sorted(identities),
        "colors": colors,
        "private_lines": dict(+private_lines),
        "covered": sorted(covered),
        "public": public,
        "private_commits": base64.b64encode(b"".join(keys)).decode(),
    }


def current_public_repos(owner):
    pages = api(["--paginate", "--slurp", f"users/{owner}/repos?type=owner&per_page=100"])
    return {
        repo_key(repo["node_id"]): {**repo, "personal": True, "public_personal": True}
        for page in pages
        for repo in page
        if repo["owner"]["login"].casefold() == owner.casefold() and not repo["private"]
    }


def live_public_heads(item):
    key, repo = item
    cmd = [*authored.GIT, "ls-remote", "--heads", "https://github.com/" + repo["full_name"] + ".git"]
    result = subprocess.run(
        cmd, check=False, capture_output=True, text=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    )
    if result.returncode:
        raise RuntimeError("Could not verify public branch heads.")
    return key, refs_key(line.split()[0] for line in result.stdout.splitlines() if line)


def refresh_public(owner, snapshot):
    covered = set(snapshot["covered"])
    repos = {key: r for key, r in current_public_repos(owner).items() if key not in covered}
    baseline = dict(snapshot["public"])
    colors = dict(snapshot["colors"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        heads = dict(pool.map(live_public_heads, repos.items()))
    if any(baseline.get(k, {}).get("heads") != rev for k, rev in heads.items()):
        raw = base64.b64decode(snapshot["private_commits"], validate=True)
        if len(raw) % 8:
            raise ValueError("Invalid commit membership data.")
        private_keys = {raw[i : i + 8] for i in range(0, len(raw), 8)}
        with tempfile.TemporaryDirectory(prefix="profile-public-") as folder:
            records = snapshot_records(
                repos, clone_histories(repos, folder), set(snapshot["identities"]), private_keys
            )
        for key, record in records.items():
            baseline[key] = {"heads": record["heads"], "lines": record["lines"]}
            colors.update(record["colors"])
    total = Counter(snapshot["private_lines"])
    for row in baseline.values():
        total.update(row["lines"])
    return total, colors


def confirmed_aliases(repos, mirrors, viewer, explicit):
    verified = {m.strip().casefold() for m in explicit if m.strip()}
    verified.update(
        {
            f"{viewer['databaseId']}+{viewer['login']}@users.noreply.github.com".casefold(),
            f"{viewer['login']}@users.noreply.github.com".casefold(),
        }
    )
    if viewer.get("email"):
        verified.add(viewer["email"].strip().casefold())
    sample = {}
    for key, mirror in mirrors.items():
        for line in (
            authored.git(mirror, "log", "--branches", "--format=%H%x09%ae")
            .decode(errors="replace")
            .splitlines()
        ):
            oid, mail = line.split("\t", 1)
            if mail.strip():
                sample.setdefault(mail.strip().casefold(), (key, oid))
    for group in authored.batch(sample.items(), 20):
        fields = []
        for index, (mail, (key, oid)) in enumerate(group):
            owner, name = repos[key]["full_name"].split("/")
            fields.append(
                f"r{index}:repository(owner:{json.dumps(owner)},name:{json.dumps(name)})"
                f"{{object(oid:{json.dumps(oid)}){{... on Commit{{author{{email user{{login}}}}}}}}}}"
            )
        answer = graph("query {" + " ".join(fields) + "}")
        for index, (mail, _) in enumerate(group):
            author = answer[f"r{index}"]["object"]["author"]
            if (
                author["email"].strip().casefold() == mail
                and (author.get("user") or {}).get("login", "").casefold() == viewer["login"].casefold()
            ):
                verified.add(mail)
    return verified


def matches_emails(org, emails, selected):
    inventory = INVENTORY.replace("$author:ID!", "$emails:[String!]!").replace(
        "author:{id:$author}", "author:{emails:$emails}"
    )
    branches = BRANCHES.replace("$author:ID!", "$emails:[String!]!").replace(
        "author:{id:$author}", "author:{emails:$emails}"
    )
    extra, pending = set(), []
    for page in connections(
        inventory, ("organization", "repositories"), org=org, emails=sorted(emails)
    ):
        for repo in page["nodes"]:
            key = repo["id"]
            if key in selected or repo["isEmpty"]:
                continue
            if has_authored_commit(repo["defaultBranchRef"]):
                extra.add(key)
            elif repo["refs"]["totalCount"] > 1 or not repo["defaultBranchRef"]:
                pending.append(key)

    def match(key):
        for page in connections(branches, ("node", "refs"), id=key, emails=sorted(emails)):
            if any(has_authored_commit(ref) for ref in page["nodes"]):
                return key

    with ThreadPoolExecutor(max_workers=4) as pool:
        extra.update(key for key in pool.map(match, pending) if key)
    return extra


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("organization")
    parser.add_argument("--contributor", required=True)
    parser.add_argument(
        "--author-email",
        action="append",
        default=[],
        help="Additional addresses confirmed by the contributor",
    )
    args = parser.parse_args()
    viewer = graph("query {viewer {login id databaseId}}")["viewer"]
    viewer["email"] = api(["user"]).get("email")
    if viewer["login"].casefold() != args.contributor.casefold():
        raise ValueError("Use the contributor’s GitHub CLI login.")
    owned = owned_repositories(viewer["login"])
    pages = api(["--paginate", "--slurp", f"orgs/{args.organization}/repos?type=all&per_page=100"])
    company = {r["node_id"]: r for page in pages for r in page}
    matched = set(contributing_repositories(args.organization, viewer))

    def metadata(company_ids):
        rows = {
            repo_key(k): {**company[k], "personal": False, "public_personal": False}
            for k in company_ids
        }
        rows.update(
            {
                repo_key(k): {**r, "personal": True, "public_personal": not r["private"]}
                for k, r in owned.items()
            }
        )
        return rows

    repos = metadata(matched)
    with tempfile.TemporaryDirectory(prefix="profile-census-") as directory:
        mirrors = clone_histories(repos, directory)
        emails = confirmed_aliases(repos, mirrors, viewer, args.author_email)
        extra = matches_emails(args.organization, emails, matched)
        if extra:
            expanded = metadata(matched | extra)
            mirrors.update(
                clone_histories({k: v for k, v in expanded.items() if k not in repos}, directory)
            )
            repos = expanded
        identities = {authored.fingerprint(mail) for mail in emails}
        records = snapshot_records(repos, mirrors, identities)
    stamp = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    value = json.dumps(
        make_snapshot(repos, records, identities, stamp), ensure_ascii=False, separators=(",", ":")
    )
    if len(value.encode()) > 48 * 1024:
        raise ValueError("Snapshot exceeds GitHub’s repository secret size limit.")
    print(
        f"Audited {len(owned)} personal and {len(matched | extra)} company repositories.",
        file=sys.stderr,
    )
    print(value)


if __name__ == "__main__":
    main()

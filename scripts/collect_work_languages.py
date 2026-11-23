"""Collect language statistics for owned repositories and verified organization contributions."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from profile_stats import repository_key

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
LANGUAGES = """
query($ids:[ID!]!) {
  nodes(ids:$ids) { ... on Repository {
    id languages(first:100, orderBy:{field:SIZE,direction:DESC}) {
      totalSize edges { size node { name color } }
    }
  } }
}
"""


def api(args, payload=None):
    request = json.dumps(payload) if payload is not None else None
    proc = subprocess.run(["gh", "api", *args], input=request, text=True, capture_output=True)
    if proc.returncode:
        # Repository paths and API diagnostics can contain private identifiers.
        raise RuntimeError("GitHub metadata request failed. Check the CLI login and organization access.")
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

    remaining = [key for key in inventory.keys() - selected
                 if inventory[key]["refs"]["totalCount"] > 1 or not inventory[key]["defaultBranchRef"]]
    with ThreadPoolExecutor(max_workers=3) as pool:
        selected.update(key for key in pool.map(branch_match, remaining) if key)
    return {key: inventory[key]["name"] for key in inventory if key in selected}


def owned_repositories(login):
    pages = api(["--paginate", "--slurp", "user/repos?affiliation=owner&visibility=all&per_page=100"])
    return {repo["node_id"]: repo for page in pages for repo in page
            if repo["owner"]["login"].casefold() == login.casefold()}


def aggregate(repos, public_ids):
    supplement, colors, public_baseline = Counter(), {}, {}
    ids = list(repos)
    for offset in range(0, len(ids), 20):
        batch = ids[offset:offset + 20]
        rows = graph(LANGUAGES, ids=batch)["nodes"]
        if {row["id"] for row in rows if row} != set(batch):
            raise RuntimeError("A selected repository became inaccessible before its language scan.")
        for row in rows:
            langs = row["languages"]
            sizes = {edge["node"]["name"]: edge["size"] for edge in langs["edges"]}
            colors.update({edge["node"]["name"]: edge["node"]["color"] for edge in langs["edges"]})
            if sum(sizes.values()) != langs["totalSize"]:
                sizes = api([f"repos/{repos[row['id']]}/languages"])
            if row["id"] in public_ids:
                public_baseline[repository_key(row["id"])] = sizes
            else:
                supplement.update(sizes)
    return {
        "version": 2,
        "as_of": datetime.now(timezone(timedelta(hours=9))).date().isoformat(),
        "bytes": dict(sorted(supplement.items())),
        "colors": {key: value for key, value in sorted(colors.items()) if value},
        "covered": sorted(repository_key(repo_id) for repo_id in repos.keys() - public_ids),
        "public_baseline": public_baseline,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("organization")
    parser.add_argument("--contributor", required=True)
    args = parser.parse_args()
    viewer = graph("query { viewer { login id } }")["viewer"]
    if viewer["login"].casefold() != args.contributor.casefold():
        raise ValueError("The active GitHub CLI account must be the requested contributor.")
    owned = owned_repositories(viewer["login"])
    public_ids = {key for key, repo in owned.items() if repo["visibility"] == "public"}
    company = contributing_repositories(args.organization, viewer)
    repos = {key: f"{args.organization}/{name}" for key, name in company.items()}
    repos.update({key: repo["full_name"] for key, repo in owned.items()})
    snapshot = aggregate(repos, public_ids)
    print(f"{len(owned)} owned ({len(public_ids)} public), {len(company)} contributed company, "
          f"{len(repos)} distinct repositories.", file=sys.stderr)
    print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()

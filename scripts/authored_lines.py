"""Count nonblank lines added by the verified Git author on reachable branches."""

import base64
import json
import os
import re
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from itertools import islice
from pathlib import Path, PurePosixPath

ZERO = "0" * 40
GIT = [
    "git",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "credential.helper=",
    "-c",
    "credential.helper=!gh auth git-credential",
]
EXCLUDED_PARTS = {
    ".git",
    "node_modules",
    "bower_components",
    "site-packages",
    ".venv",
    "venv",
    ".next",
    ".nuxt",
    ".cache",
    "__pycache__",
    "coverage",
    "dist",
    "target",
    "Pods",
}
GRAPHICS = {"mlx", "mlx42", "glfw", "glad", "glew", "minilibx", "minilibx-linux", "raylib", "imgui"}
AUTOMATION = {"noreply@anthropic.com", "noreply@openai.com"}
TRAILER_MAIL = re.compile(r"<([^<>]+)>")
OBJECT_ID = re.compile(rb"[0-9a-f]{40}")


def git(repo, *args, data=None):
    proc = subprocess.run(
        [*GIT, "-C", str(repo), *args],
        input=data,
        check=False,
        capture_output=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"},
    )
    if proc.returncode:
        raise RuntimeError(
            "Git could not complete the requested history or object operation: " + args[0]
        )
    return proc.stdout


def fingerprint(value):
    return sha256(value.strip().casefold().encode()).hexdigest()


def author_commits(repo, identities, inherited=()):
    fmt = "%H%x09%ae%x09%(trailers:key=Co-authored-by,valueonly,unfold,separator=%x1f)"
    own, shared = set(), 0
    for line in (
        git(repo, "log", "--branches", "--no-merges", "--format=" + fmt)
        .decode(errors="replace")
        .split("\n")
    ):
        if not line:
            continue
        oid, author, trailers = line.split("\t", 2)
        if oid in inherited or fingerprint(author) not in identities:
            continue
        partners = [m.casefold() for m in TRAILER_MAIL.findall(trailers)]
        if any(
            fingerprint(m) not in identities and m not in AUTOMATION and "[bot]@" not in m
            for m in partners
        ):
            shared += 1
            continue
        own.add(oid)
    return own, shared


def inherited_commits(repo, fork):
    if not fork:
        return set()
    git(
        repo,
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-write-fetch-head",
        "--filter=tree:0",
        "https://github.com/" + fork["parent"] + ".git",
        "+refs/heads/*:refs/profile-upstream/*",
    )
    return set(
        git(repo, "rev-list", "--before=" + fork["created_at"], "--glob=refs/profile-upstream/*")
        .decode()
        .splitlines()
    )


def changes(repo, commits):
    if not commits:
        return []
    stream = git(
        repo,
        "diff-tree",
        "--stdin",
        "--root",
        "-r",
        "--raw",
        "-z",
        "--no-abbrev",
        "-M",
        "-C100%",
        "--find-copies-harder",
        "--pretty=format:%x00%H%x00",
        data=("\n".join(sorted(commits)) + "\n").encode(),
    )
    tokens = iter(stream.split(b"\x00"))
    result, oid = [], None
    for field in tokens:
        field = field.lstrip(b"\n")
        if not field:
            continue
        if OBJECT_ID.fullmatch(field):
            oid = field.decode()
            continue
        old_mode, new_mode, old, new, status = field[1:].decode().split()
        path = next(tokens)
        if status[:1] in ("R", "C"):
            path = next(tokens)
        if not new_mode.startswith("100") or old == new:
            continue
        path = path.decode("utf-8", "surrogateescape")
        if any(part in EXCLUDED_PARTS for part in PurePosixPath(path).parts):
            continue
        result.append(
            (oid, old if old_mode.startswith("100") else ZERO, new, path, new_mode == "100755")
        )
    return result


def classify(requests):
    if not requests:
        return []
    payload = b"".join((json.dumps(r, ensure_ascii=True) + "\n").encode() for r in requests)
    proc = subprocess.run(
        [os.environ["LANGDETECT_BIN"]], input=payload, capture_output=True, check=True
    )
    out = [r.split("\t", 1) for r in proc.stdout.decode().splitlines()]
    if len(out) != len(requests):
        raise RuntimeError("Incomplete language classification.")
    return out


def objects(repo, ids):
    if not ids:
        return {}
    data = git(repo, "cat-file", "--batch", data=("\n".join(ids) + "\n").encode())
    pos, blobs = 0, {}
    for oid in ids:
        end = data.index(b"\n", pos)
        found, kind, size = data[pos:end].split()
        if kind != b"blob" or found.decode() != oid:
            raise RuntimeError("Unexpected Git object.")
        pos = end + 1
        blobs[oid] = data[pos : pos + int(size)]
        pos += int(size) + 1
    return blobs


def batch(it, amount):
    it = iter(it)
    while True:
        group = list(islice(it, amount))
        if not group:
            return
        yield group


def graphics_path(path):
    return any(
        part.casefold() in GRAPHICS or part.casefold().startswith("minilibx")
        for part in PurePosixPath(path).parts[:-1]
    )


def added_lines(repo, old, new):
    patch = git(
        repo,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "-w",
        "--ignore-blank-lines",
        "--unified=0",
        old,
        new,
    )
    hunks = False
    count = 0
    for line in patch.split(b"\n"):
        if line.startswith(b"@@ "):
            hunks = True
        elif hunks and line.startswith(b"+") and line[1:].strip():
            count += 1
    return count


def calculate(
    repo, identities, personal, fork=None, baseline=None, omit_commits=(), author_history=None
):
    repo = Path(repo)
    if author_history is None:
        author_history = author_commits(repo, identities, inherited_commits(repo, fork))
    candidates, shared = author_history
    own = candidates.difference(omit_commits)
    rows = changes(repo, own)
    eligible_paths = {}
    for _, _, _, path, executable in rows:
        eligible_paths[path] = {
            "Path": path,
            "Executable": executable,
            "Graphics": personal and graphics_path(path),
            "Probe": True,
        }
    for reqs in batch(eligible_paths.values(), 400):
        for req, (allowed, _) in zip(reqs, classify(reqs)):
            req["allowed"] = bool(allowed)

    graphics_base = baseline or {}
    totals, colors, graphics = Counter(), {}, Counter()
    seen, selected = set(), {}
    for commit, old, new, path, _ in rows:
        req = eligible_paths[path]
        if not req["allowed"]:
            continue
        if personal and graphics_path(path):
            original = graphics_base.get(path)
            if new == original:
                continue
            if old == ZERO and original:
                # Compare a first import with its known upstream file, including edits in that import.
                old = original
        key = (old, new, path)
        if key not in selected:
            selected[key] = commit

    changed = []
    for group in batch(selected, 64):
        blobs = objects(repo, list(dict.fromkeys(n for _, n, _ in group)))
        requests = [
            {
                "Path": p,
                "Graphics": personal and graphics_path(p),
                "Content": base64.b64encode(blobs[n]).decode(),
            }
            for _, n, p in group
        ]
        for (old, new, path), (lang, col) in zip(group, classify(requests)):
            if not lang or (old, new, lang) in seen:
                continue
            seen.add((old, new, lang))
            if col:
                colors[lang] = col
            is_graphics = personal and (
                graphics_path(path) or PurePosixPath(path).parts[0].casefold() in {"fract-ol", "cub3d"}
            )
            if old == ZERO:
                count = sum(bool(line.strip()) for line in blobs[new].split(b"\n"))
                totals[lang] += count
                if is_graphics:
                    graphics[lang] += count
            else:
                changed.append((old, new, lang, is_graphics))
    with ThreadPoolExecutor(max_workers=3) as pool:
        for change, count in zip(changed, pool.map(lambda c: added_lines(repo, c[0], c[1]), changed)):
            lang = change[2]
            totals[lang] += count
            if change[3]:
                graphics[lang] += count
    return {
        "lines": dict(+totals),
        "colors": colors,
        "commits": sorted(own),
        "shared": shared,
        "graphics": dict(+graphics),
    }

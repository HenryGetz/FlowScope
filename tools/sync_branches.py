#!/usr/bin/env python3
"""Pull-and-verify branch sync: merge ``origin/<from>`` into ``<to>`` behind a temporary worktree.

The invoking checkout is never disturbed (it is the user's): the merge and the gate
run happen inside a temporary ``git worktree`` which is always removed afterwards.
Only refs move, and only after the gates pass; ``--push`` then publishes ``<to>``.

Usage:
    python tools/sync_branches.py --from main --to mlx [--push] [--dry-run]
                                  [--tier quick|full] [--json out/sync.json]

Flow:
    1. ``git fetch origin``; refuse to run on a dirty tracked tree (this tool never
       stashes, discards, or force-cleans user work -- clean the tree manually).
       Its own outputs (``out/sync_log.jsonl``, ``--json PATH``) are exempt.
    2. Temporary worktree with local ``<to>`` checked out; when no local ``<to>``
       exists it is created at ``origin/<to>`` (covers detached CI checkouts).
    3. ``git merge --no-edit origin/<from>`` inside the worktree.
    4. ``python tools/run_gates.py --tier <tier> --python <abs> --json <tmp>``
       against the merged tree (cwd = worktree root; interpreter: the worktree's
       ``.venv/bin/python``, else the main checkout's ``.venv/bin/python``, else
       ``python3`` -- the same absolute interpreter is handed to run_gates.py's
       ``--python`` for its import smokes).
    5. Gates pass  -> the merge stands on ``<to>``; ``--push`` runs
       ``git push origin <to>``.
       Gates fail or the merge conflicts -> ``<to>`` is rolled back to its
       pre-merge sha and the tool exits 2 (failing gate names / conflict files
       are printed).
       ``--dry-run`` validates fetch/merge/gates like a real sync but always
       restores ``<to>`` to its pre-merge sha and pushes nothing.

The final stdout line is a JSON summary
``{from,to,merged_sha,pre_sha,gates_passed,gates_failed,pushed}``; the same line
is appended to ``out/sync_log.jsonl`` when ``out/`` exists, and ``--json PATH``
writes the summary object to PATH.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EXIT_OK = 0
EXIT_PRECONDITION = 1  # dirty tree, missing refs, fetch/push trouble
EXIT_SYNC_FAILED = 2  # merge conflicts or failing gates


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def _ref_sha(repo: Path, ref: str) -> str | None:
    proc = _git(repo, "rev-parse", "--verify", "-q", ref)
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def _relay(proc: subprocess.CompletedProcess) -> None:
    for stream in (proc.stdout, proc.stderr):
        text = (stream or "").strip()
        if text:
            print(text)


def rollback(worktree: Path, pre_sha: str) -> None:
    """Undo the merge attempt inside the worktree, restoring <to> to pre_sha."""
    if _git(worktree, "rev-parse", "--verify", "-q", "MERGE_HEAD").returncode == 0:
        _git(worktree, "merge", "--abort")  # conflicted merge in progress
    else:
        _git(worktree, "reset", "--hard", pre_sha)  # completed merge


def parse_gate_results(gates_json: Path, lines: list[str], rc: int) -> tuple[list[str], list[str]]:
    """Extract (passed, failed) gate names from run_gates.py output."""
    passed: list[str] = []
    failed: list[str] = []
    data = None
    try:
        if gates_json.is_file():
            data = json.loads(gates_json.read_text())
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and ("passed" in data or "failed" in data):
        passed = [str(name) for name in data.get("passed", [])]
        failed = [str(name) for name in data.get("failed", [])]
    elif isinstance(data, dict) and data.get("gates"):
        for obj in data["gates"]:  # {"name": ..., "status": ...} entries
            if not isinstance(obj, dict) or obj.get("name") is None:
                continue
            status = str(obj.get("status", "")).lower()
            (passed if status in ("ok", "pass", "passed") else failed).append(str(obj["name"]))
    else:  # per-gate JSON lines: {"name": ..., "ok": ...} or {"gate": ..., "status": ...}
        for line in lines:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            name = obj.get("name", obj.get("gate"))
            if name is None:
                continue
            ok = obj.get("ok")
            if not isinstance(ok, bool):
                ok = str(obj.get("status", "")).lower() in ("ok", "pass", "passed")
            (passed if ok else failed).append(str(name))
    if not passed and not failed:
        if rc == 0:
            passed = ["run_gates.py"]
        else:
            failed = ["run_gates.py (see output above)"]
    return passed, failed


def resolve_python(worktree: Path, checkout: Path) -> str:
    """Interpreter for the gate run.

    A temporary worktree carries no .venv of its own (untracked), but the main
    checkout's venv runs the merged tree's scripts fine (same interpreter family,
    shared site-packages). Chain: worktree .venv, else the checkout's .venv
    (derived from the git common dir), else python3.
    """
    candidates = [worktree / ".venv" / "bin" / "python"]
    common = _git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode == 0 and common.stdout.strip():
        candidates.append(Path(common.stdout.strip()).parent / ".venv" / "bin" / "python")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("python3") or "python3"


def run_gates(worktree: Path, checkout: Path, tier: str) -> tuple[int, list[str], list[str]]:
    """Run tools/run_gates.py --tier <tier> against the merged tree in the worktree."""
    runner = worktree / "tools" / "run_gates.py"
    if not runner.is_file():
        fallback = checkout / "tools" / "run_gates.py"
        if fallback.is_file():
            print(
                f"note: merged tree has no tools/run_gates.py (not pushed yet?); "
                f"running {fallback} against the worktree",
                file=sys.stderr,
            )
            runner = fallback
        else:
            print(
                "error: tools/run_gates.py exists neither in the merged tree nor in "
                "the invoking checkout; cannot gate the merge",
                file=sys.stderr,
            )
            return EXIT_SYNC_FAILED, [], ["run_gates.py (missing)"]
    interpreter = resolve_python(worktree, checkout)
    gates_json = worktree.parent / "gates.json"
    cmd = [
        interpreter, "-u", str(runner),
        "--tier", tier, "--python", interpreter, "--json", str(gates_json),
    ]
    print(f"-- gates: {' '.join(cmd)}  (cwd={worktree})")
    proc = subprocess.Popen(
        cmd,
        cwd=worktree,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="")
        lines.append(line)
    rc = proc.wait()
    passed, failed = parse_gate_results(gates_json, lines, rc)
    return rc, passed, failed


def emit_summary(summary: dict, root: Path, json_path: str | None) -> None:
    line = json.dumps(summary)
    print(line)
    log = root / "out" / "sync_log.jsonl"
    if log.parent.is_dir():
        try:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            print(f"warning: could not append to {log}: {exc}", file=sys.stderr)
    if json_path:
        path = Path(json_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(summary, indent=2) + "\n")
        except OSError as exc:
            print(f"warning: could not write {path}: {exc}", file=sys.stderr)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sync_branches.py",
        description="Merge origin/<from> into <to> in a temporary worktree, gate it, "
        "and only then let the merge stand (optionally pushing <to>).",
    )
    parser.add_argument("--from", dest="source", required=True, metavar="FROM",
                        help="branch on origin to merge from (e.g. main)")
    parser.add_argument("--to", dest="target", required=True, metavar="TO",
                        help="local branch to merge into (e.g. mlx)")
    parser.add_argument("--push", action="store_true",
                        help="push <to> to origin after the gates pass")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch/merge/gate for real, then restore <to> to its "
                        "pre-merge sha and push nothing")
    parser.add_argument("--tier", choices=("quick", "full"), default="full",
                        help="gate tier for tools/run_gates.py (default: full)")
    parser.add_argument("--json", dest="json_path", metavar="PATH",
                        help="also write the JSON summary to PATH")
    parser.add_argument("--force-clean", action="store_true",
                        help="REJECTED on purpose: this tool never stashes or "
                        "discards your work")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    summary = {
        "from": args.source,
        "to": args.target,
        "merged_sha": None,
        "pre_sha": None,
        "gates_passed": [],
        "gates_failed": [],
        "pushed": False,
    }

    root_proc = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    root = Path(root_proc.stdout.strip()) if root_proc.returncode == 0 else None

    def finish(code: int) -> int:
        if root is not None:
            emit_summary(summary, root, args.json_path)
        else:
            emit_summary(summary, Path.cwd(), args.json_path)
        return code

    if args.force_clean:
        print(
            "refusing: --force-clean is not supported; this tool never stashes or "
            "discards your work. Commit or stash manually and re-run.",
            file=sys.stderr,
        )
        return finish(EXIT_PRECONDITION)
    if root is None:
        print("error: not inside a git work tree", file=sys.stderr)
        return finish(EXIT_PRECONDITION)
    if args.source == args.target:
        print("error: --from and --to name the same branch", file=sys.stderr)
        return finish(EXIT_PRECONDITION)

    fetch = _git(root, "fetch", "origin")
    _relay(fetch)
    if fetch.returncode != 0:
        print("error: git fetch origin failed", file=sys.stderr)
        return finish(EXIT_PRECONDITION)

    status = _git(root, "status", "--porcelain")

    # This tool's own outputs are not dirt: it appends to out/sync_log.jsonl on
    # every run (by design) and --json PATH is written here too; neither may
    # block a re-run once committed.
    own_outputs = {"out/sync_log.jsonl"}
    if args.json_path:
        try:
            rel = Path(args.json_path).resolve().relative_to(root.resolve()).as_posix()
            own_outputs.add(rel)
        except ValueError:
            pass  # outside the repository: cannot be repository dirt

    def is_dirt(line: str) -> bool:
        if line.startswith("??") or line.startswith("!!"):
            return False
        path = line[3:].split(" -> ")[-1].strip('"')
        return path not in own_outputs

    lines = status.stdout.splitlines()
    dirty = [line for line in lines if is_dirt(line)]
    exempt = [
        line for line in lines
        if line and not is_dirt(line) and not line.startswith(("??", "!!"))
    ]
    if exempt:
        print("note: ignoring this tool's own output files in the clean-tree check:")
        for line in exempt:
            print(f"  {line}")
    if dirty:
        print(
            "refusing: the tracked tree is dirty and this tool will not stash or "
            "discard your work; commit or stash manually and re-run:",
            file=sys.stderr,
        )
        for line in dirty:
            print(f"  {line}", file=sys.stderr)
        return finish(EXIT_PRECONDITION)

    from_ref = f"origin/{args.source}"
    from_sha = _ref_sha(root, f"refs/remotes/origin/{args.source}")
    if from_sha is None:
        print(f"error: no such ref on origin: {from_ref}", file=sys.stderr)
        return finish(EXIT_PRECONDITION)
    to_local = _ref_sha(root, f"refs/heads/{args.target}")
    to_remote = _ref_sha(root, f"refs/remotes/origin/{args.target}")
    if to_local is None and to_remote is None:
        print(
            f"error: neither refs/heads/{args.target} nor origin/{args.target} "
            "exists; nothing to merge into",
            file=sys.stderr,
        )
        return finish(EXIT_PRECONDITION)
    pre_sha = to_local if to_local is not None else to_remote
    summary["pre_sha"] = pre_sha

    print("== sync plan ==")
    print(f"  from      : {from_ref} ({from_sha[:12]})")
    print(f"  to        : {args.target} @ {pre_sha[:12]}"
          + ("" if to_local is not None else f" (created from origin/{args.target})"))
    print(f"  gates     : tools/run_gates.py --tier {args.tier}")
    if args.dry_run:
        mode = (f"dry-run: merge+gates validated, then {args.target} reset to "
                f"{pre_sha[:12]}; nothing pushed")
    elif args.push:
        mode = f"merge stands on {args.target} and is pushed to origin"
    else:
        mode = f"merge stands on {args.target} (no push requested)"
    print(f"  mode      : {mode}")
    if args.dry_run and args.push:
        print("  note      : --dry-run suppresses --push")
    print("  worktree  : temporary; always removed afterwards")

    tmp = Path(tempfile.mkdtemp(prefix="flowscope-sync-"))
    worktree = tmp / "wt"
    added = False
    try:
        if to_local is not None:
            wt_add = _git(root, "worktree", "add", str(worktree), args.target)
        else:
            wt_add = _git(
                root, "worktree", "add", "-b", args.target,
                str(worktree), f"origin/{args.target}",
            )
        if wt_add.returncode != 0:
            _relay(wt_add)
            print(
                f"error: could not create a temporary worktree for {args.target} "
                "(is it checked out in another worktree?)",
                file=sys.stderr,
            )
            return finish(EXIT_PRECONDITION)
        added = True

        merge = _git(worktree, "merge", "--no-edit", from_ref)
        _relay(merge)
        if merge.returncode != 0:
            conflicts = [
                line
                for line in _git(worktree, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
                if line
            ]
            rollback(worktree, pre_sha)
            print(f"merge of {from_ref} into {args.target} failed; rolled back", file=sys.stderr)
            if conflicts:
                print("conflict files:", file=sys.stderr)
                for path in conflicts:
                    print(f"  {path}", file=sys.stderr)
            return finish(EXIT_SYNC_FAILED)

        merged_sha = _ref_sha(worktree, "HEAD")
        summary["merged_sha"] = merged_sha

        rc, passed, failed = run_gates(worktree, root, args.tier)
        summary["gates_passed"], summary["gates_failed"] = passed, failed
        if rc != 0:
            rollback(worktree, pre_sha)
            print(f"gates failed: {', '.join(failed)}", file=sys.stderr)
            print(
                f"merge result rolled back; {args.target} restored to {pre_sha[:12]}",
                file=sys.stderr,
            )
            return finish(EXIT_SYNC_FAILED)

        if args.dry_run:
            _git(worktree, "reset", "--hard", pre_sha)
            print(
                f"dry-run: gates passed on merge result {merged_sha[:12]}; "
                f"{args.target} restored to {pre_sha[:12]}; nothing pushed"
            )
            return finish(EXIT_OK)

        if args.push:
            push = _git(root, "push", "origin", args.target)
            _relay(push)
            if push.returncode != 0:
                summary["pushed"] = False
                print(
                    f"error: the merge stands on {args.target} at {merged_sha[:12]} "
                    f"(gates passed) but `git push origin {args.target}` failed",
                    file=sys.stderr,
                )
                return finish(EXIT_PRECONDITION)
            summary["pushed"] = True
            print(
                f"gates passed: {args.target} now at {merged_sha[:12]} "
                f"(pre: {pre_sha[:12]}), pushed to origin"
            )
        else:
            print(
                f"gates passed: {args.target} now at {merged_sha[:12]} "
                f"(pre: {pre_sha[:12]}), not pushed"
            )
        return finish(EXIT_OK)
    finally:
        if added:
            _git(root, "worktree", "remove", "--force", str(worktree))
        _git(root, "worktree", "prune")
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

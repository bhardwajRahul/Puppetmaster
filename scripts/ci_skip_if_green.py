#!/usr/bin/env python3
"""Skip a push CI suite when this git tree already has a successful CI run.

Pull requests always run. A tag or main push of dest-into-main bytes that
already passed on the PR is a third flake lottery; Puppetmaster has no
installer-adopt path, so that lottery blocks the tag. Lookup failure
writes skip_suite=false (run the suite).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def matching_green_run(target_tree, runs, tree_for_sha):
    if not target_tree:
        return None
    for run in runs:
        if run.get("conclusion") != "success":
            continue
        head = run.get("headSha") or run.get("head_sha") or ""
        if not head:
            continue
        tree = tree_for_sha(head)
        if tree and tree == target_tree:
            return run
    return None


def filter_successful_runs(runs):
    return [run for run in runs if run.get("conclusion") == "success"]


def should_skip_push_suite(
    event_name, target_tree, runs, tree_for_sha, current_run_id=None
):
    if (event_name or "") != "push":
        return False
    skip_ids = set()
    if current_run_id not in (None, ""):
        skip_ids.add(str(current_run_id))
    filtered = []
    for run in filter_successful_runs(runs):
        run_id = run.get("databaseId") or run.get("id")
        if run_id is not None and str(run_id) in skip_ids:
            continue
        filtered.append(run)
    return matching_green_run(target_tree, filtered, tree_for_sha) is not None


def write_github_output(name, value, path=None):
    dest = path if path is not None else os.environ.get("GITHUB_OUTPUT")
    if not dest:
        return
    with open(dest, "a", encoding="utf-8") as handle:
        handle.write("{}={}\n".format(name, value))


def git_tree_sha(rev="HEAD"):
    return subprocess.check_output(
        ["git", "rev-parse", "{}^{{tree}}".format(rev)],
        text=True,
    ).strip()


def _gh_json(args, repo=None):
    cmd = ["gh"] + args
    if repo and "--repo" not in args:
        cmd[1:1] = ["--repo", repo]
    return json.loads(subprocess.check_output(cmd, text=True))


def _detect_repo():
    env = os.environ.get("GITHUB_REPOSITORY") or ""
    if env:
        return env
    data = json.loads(
        subprocess.check_output(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            text=True,
        )
    )
    return data["nameWithOwner"]


def _commit_tree_via_api(repo, sha):
    try:
        payload = json.loads(
            subprocess.check_output(
                ["gh", "api", "repos/{}/commits/{}".format(repo, sha)],
                text=True,
            )
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None
    commit = payload.get("commit") or {}
    tree = commit.get("tree") or {}
    sha_out = tree.get("sha")
    return sha_out if isinstance(sha_out, str) and sha_out else None


def _tree_resolver(repo):
    cache = {}

    def tree_for(sha):
        if sha in cache:
            return cache[sha]
        tree = None
        try:
            tree = git_tree_sha(sha)
        except subprocess.CalledProcessError:
            tree = None
        if not tree:
            tree = _commit_tree_via_api(repo, sha)
        cache[sha] = tree
        return tree

    return tree_for


def list_workflow_runs(repo, workflow, limit):
    return _gh_json(
        [
            "run",
            "list",
            "--workflow",
            workflow,
            "--limit",
            str(limit),
            "--json",
            "headSha,databaseId,url,event,displayTitle,conclusion",
        ],
        repo=repo,
    )


def cmd_skip_if_green(args):
    event_name = args.event or os.environ.get("GITHUB_EVENT_NAME") or ""
    if (event_name or "") != "push":
        write_github_output("skip_suite", "false")
        sys.stdout.write("skip_suite=false event={}\n".format(event_name))
        return 0
    try:
        repo = args.repo or _detect_repo()
        current_run = args.run_id or os.environ.get("GITHUB_RUN_ID") or ""
        target_tree = git_tree_sha(args.sha)
        runs = list_workflow_runs(repo, args.workflow, args.limit)
        skip = should_skip_push_suite(
            event_name,
            target_tree,
            runs,
            _tree_resolver(repo),
            current_run,
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        write_github_output("skip_suite", "false")
        sys.stdout.write("skip_suite=false lookup failed: {}\n".format(exc))
        return 0
    write_github_output("skip_suite", "true" if skip else "false")
    if skip:
        sys.stdout.write(
            "skip_suite=true tree {} already has a successful CI run\n".format(
                target_tree
            )
        )
    else:
        sys.stdout.write(
            "skip_suite=false event={} tree {}\n".format(event_name, target_tree)
        )
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="ci_skip_if_green")
    parser.add_argument(
        "--event",
        default="",
        help="GitHub event name (default: GITHUB_EVENT_NAME)",
    )
    parser.add_argument("--repo", default="", help="owner/name")
    parser.add_argument("--run-id", default="", help="current Actions run id")
    parser.add_argument("--sha", default="HEAD", help="git revision")
    parser.add_argument("--workflow", default="CI", help="workflow name")
    parser.add_argument("--limit", type=int, default=80)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return cmd_skip_if_green(args)


if __name__ == "__main__":
    raise SystemExit(main())

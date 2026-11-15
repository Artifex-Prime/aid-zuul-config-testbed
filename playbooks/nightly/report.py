"""The nightly's result, to AIDepartment's ci-nightly issue (AIDepartment#1350; its ci/zuul/publish.md, "The nightly").

aid-nightly-report runs beside AIDepartment's aid-nightly in periodic, on the executor. It does what nightly.yml's
report job does on GitHub Actions (AIDepartment's ci/nightly.mjs report), for the same issue: closes it when the
nightly passed, otherwise comments on it or opens it. First, as aid-nightly's own script does, it lets through only
the timer that is 04:00 in Madrid, and only while AIDepartment's ci/publisher names Zuul; otherwise it writes nothing.
Then it waits for aid-nightly's result in this buildset, from Zuul's builds API.

    python3 report.py --publisher-file F --zuul URL --tenant T --buildset UUID --build-url URL --token-file F

Standard library only. Reaches api.github.com through the environment's https_proxy, as the executor does.
"""

import argparse
import datetime
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
REPOSITORY = "kristofk/AIDepartment"
LABEL = "ci-nightly"
TITLE = "Nightly CI failed"
JOB = "aid-nightly"
# The periodic pipeline's two times (zuul.d/pipelines.yaml), in UTC: Zuul's timer has no time zone.
SCHEDULES = (2, 3)


class Failure(Exception):
    pass


def last_sunday(year, month):
    day = datetime.datetime(year, month + 1, 1, tzinfo=datetime.timezone.utc) - datetime.timedelta(days=1)
    return day - datetime.timedelta(days=(day.weekday() + 1) % 7)


def madrid_offset(instant):
    """Madrid's offset from UTC, in hours: CEST (2) from the last Sunday of March to the last Sunday of October, at
    01:00 UTC both, as the EU's rule has it since 1996; CET (1) otherwise. No tz database needed on the executor."""
    start = last_sunday(instant.year, 3).replace(hour=1)
    end = last_sunday(instant.year, 10).replace(hour=1)
    return 2 if start <= instant < end else 1


def fired(now):
    """The timer that fired: the latest of SCHEDULES at or before now (AIDepartment's ci/nightly.mjs, zuulSchedule)."""
    candidates = []
    for hour in SCHEDULES:
        today = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        candidates.append(today if today <= now else today - datetime.timedelta(days=1))
    return max(candidates)


def is_madrid_four(instant):
    return (instant + datetime.timedelta(hours=madrid_offset(instant))).hour == 4


def call(method, url, token=None, body=None, ok=(200, 201)):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "aid-zuul-nightly-report"}
    if token:
        headers |= {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
    request = urllib.request.Request(url, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, text = response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        status, text = error.code, ""
    except urllib.error.URLError as error:
        raise Failure(f"{method} {urllib.parse.urlsplit(url).path} failed: {error.reason}")
    if status not in ok:
        raise Failure(f"{method} {urllib.parse.urlsplit(url).path}: {status}")
    return json.loads(text) if text else None


def nightly_result(zuul, tenant, buildset, deadline, interval=60):
    """aid-nightly's result in the buildset; None when it has none by the deadline."""
    query = urllib.parse.urlencode({"buildset": buildset, "job_name": JOB})
    while True:
        builds = call("GET", f"{zuul.rstrip('/')}/api/tenant/{tenant}/builds?{query}") or []
        done = [build for build in builds if build.get("result")]
        if done:
            return done[0]["result"], done[0].get("uuid")
        if time.time() + interval > deadline:
            return None, None
        time.sleep(interval)


def report(result, link, token, api=API, today=None):
    """Closes the open ci-nightly issues on SUCCESS, else comments on the newest or opens one. Returns what it did."""
    repo = f"{api}/repos/{REPOSITORY}"
    issues = call("GET", f"{repo}/issues?" + urllib.parse.urlencode({"labels": LABEL, "state": "open", "per_page": 100}), token)
    issues = sorted((issue for issue in issues if "pull_request" not in issue), key=lambda issue: -issue["number"])
    if result == "SUCCESS":
        for issue in issues:
            call("POST", f"{repo}/issues/{issue['number']}/comments", token, {"body": f"Passed in {link}"})
            call("PATCH", f"{repo}/issues/{issue['number']}", token, {"state": "closed", "state_reason": "completed"})
        return "closed" if issues else "none", [issue["number"] for issue in issues]
    date = (today or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%d")
    body = "\n".join([
        f"The nightly full run on Zuul of {date} (UTC) {f'ended with `{result}`' if result else 'has no result after the job allowed for it'}: {link}",
        "",
        "Its log says which target failed. The nightly on Zuul is Bazel's build and test of everything; the rest of CI "
        "runs on GitHub Actions until Zuul has it (AIDepartment's ci/zuul/publish.md, \"The nightly\").",
        "",
        "This issue closes when a nightly run passes (aid-zuul-config's aid-nightly-report).",
    ])
    if issues:
        call("POST", f"{repo}/issues/{issues[0]['number']}/comments", token, {"body": body})
        return "commented", [issues[0]["number"]]
    created = call("POST", f"{repo}/issues", token, {"title": TITLE, "body": body, "labels": [LABEL]})
    return "created", [created["number"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--publisher-file", required=True, help="AIDepartment's ci/publisher, in the job's checkout")
    parser.add_argument("--zuul", required=True, help="zuul-web's address, from the executor")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--buildset", required=True)
    parser.add_argument("--build-url", required=True, help="the nightly's buildset or build page, for the issue")
    parser.add_argument("--token-file", help="the App's token, with issues: write; without it, decide only")
    parser.add_argument("--wait", type=int, default=6600, help="seconds to wait for aid-nightly's result")
    parser.add_argument("--now", help=argparse.SUPPRESS)
    parser.add_argument("--api", default=API, help=argparse.SUPPRESS)
    args = parser.parse_args()
    now = datetime.datetime.fromisoformat(args.now) if args.now else datetime.datetime.now(datetime.timezone.utc)
    instant = fired(now)
    if not is_madrid_four(instant):
        return {"report": False, "why": f"the timer of {instant:%Y-%m-%dT%H:%MZ} is not 04:00 in Madrid"}
    with open(args.publisher_file) as handle:
        publisher = handle.read().strip()
    if publisher != "zuul":
        return {"report": False, "why": f"ci/publisher names {publisher!r}: {publisher} reports the nightly"}
    if not args.token_file:
        return {"report": True}
    result, _ = nightly_result(args.zuul, args.tenant, args.buildset, time.time() + args.wait)
    with open(args.token_file) as handle:
        token = handle.read().strip()
    action, issues = report(result, args.build_url, token, api=args.api.rstrip("/"))
    return {"report": True, "result": result, "action": action, "issues": issues}


if __name__ == "__main__":
    try:
        print(json.dumps(main(), sort_keys=True))
    except (Failure, OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}))
        sys.exit(1)

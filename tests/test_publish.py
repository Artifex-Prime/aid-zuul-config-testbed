"""The publishing jobs' playbooks (zuul.d/publish.yaml; AIDepartment#1350) under real Ansible with Zuul's plugins, as
test_playbooks.py runs the release's: each stage of playbooks/publish/ with a stand-in for AIDepartment's stage
script, which records what reached it, and the real helper against the stand-in for GitHub. And
playbooks/nightly/report.py against stand-ins for Zuul's builds API and GitHub's issues.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
import stub_github  # noqa: E402
from test_github_app_token import KEY, PEM  # noqa: E402
from test_playbooks import HELPER, ROOT, ansible, setUpModule  # noqa: E402,F401

PUSHER = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJl"
# The stand-in for AIDepartment's stage script: what reached it, as JSON, and an exit status the test chooses.
SCRIPT = r"""#!/usr/bin/env python3
import json, os, stat, sys
def token(name):
    path = os.environ.get(name, "")
    if not path:
        return None
    return {"path": path, "value": open(path).read(), "mode": stat.S_IMODE(os.stat(path).st_mode)}
seen = {"argv": sys.argv[1:], "cwd": os.getcwd(),
        "env": {k: v for k, v in os.environ.items() if k.startswith(("ZUUL_", "BUILDKIT_", "CI_"))},
        "github": token("CI_GITHUB_TOKEN_FILE"), "pusher": token("CI_PUBLISH_TOKEN_FILE")}
with open(os.path.join(os.environ["ZUUL_STATE"], "seen-" + sys.argv[1] + ".json"), "w") as handle:
    json.dump(seen, handle)
sys.exit(int(os.environ.get("TEST_EXIT", "0")))
"""
ENDPOINTS = {"BUILDKIT_HOST": "tcp://buildkitd:1234", "CI_SCRATCH_REGISTRY": "scratch:5000",
             "CI_RUN_NAMESPACE": "imagecheck", "CI_PUBLISH_URL": "http://pusher/v1/publish",
             "CI_PUBLISH_AUDIENCE": "thunderbox-registry", "CI_PUBLISH_TIMEOUT_SECONDS": "900"}
STAGES = {"build": {"permissions": ["contents=read"]}, "push": {"permissions": [], "pusher": True},
          "record": {"permissions": ["contents=write"]}, "report": {"permissions": ["issues=write"]}}


class StagePlaybookTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="publish-")
        self.addCleanup(shutil.rmtree, self.work)
        shutil.copytree(os.path.join(ROOT, "playbooks", "publish"), os.path.join(self.work, "playbooks", "publish"))
        os.makedirs(os.path.join(self.work, "playbooks", "release"))
        self.src = os.path.join(self.work, "src")
        script = os.path.join(self.src, "ci", "zuul", "publish", "apps.sh")
        os.makedirs(os.path.dirname(script))
        with open(script, "w") as handle:
            handle.write(SCRIPT)
        os.chmod(script, 0o755)
        self.root = os.path.join(self.work, "state")
        os.makedirs(self.root)
        self.credentials = os.path.join(self.work, "credentials")

    def run_stage(self, stub, stage, endpoints=ENDPOINTS, success=True, exit_status=0):
        with open(os.path.join(self.work, "playbooks", "release", "github-app-token.py"), "w") as shim:
            shim.write("import runpy, sys\n"
                       f"sys.argv[1:] += ['--api', {stub.url!r}]\n"
                       f"sys.argv[0] = {HELPER!r}\n"
                       f"runpy.run_path({HELPER!r}, run_name='__main__')\n")
        # A second run in the same test sets Zuul's plugins up again.
        shutil.rmtree(os.path.join(self.work, "zuul-ansible"), ignore_errors=True)
        os.environ["TEST_EXIT"] = str(exit_status)
        self.addCleanup(os.environ.pop, "TEST_EXIT", None)
        return ansible(os.path.join(self.work, "playbooks", "publish", f"{stage}.yaml"), {
            "zuul": {"job": "aid-publish-apps", "pipeline": "post", "tenant": "aid", "build": "b" * 32,
                     "oldrev": "1" * 40, "newrev": "2" * 40, "project": {"src_dir": self.src}},
            "zuul_success": success, "zuul_web_root": "https://zuul.example",
            "aid_release_github": {"app_id": stub_github.APP_ID, "private_key": PEM.decode()}, "aid_pusher": PUSHER,
            "publish_script": "ci/zuul/publish/apps.sh", "publish_stages": STAGES, "publish_endpoints": endpoints,
            "publish_root": self.root, "publish_credentials": self.credentials}, self.work)

    def seen(self, stage):
        with open(os.path.join(self.root, f"seen-{stage}.json")) as handle:
            return json.load(handle)

    def assertNoSecret(self, stub, output):
        self.assertNotIn(b"".join(PEM.splitlines()[1:-1]).decode()[:40], output, "the App's key is in the output")
        for secret in stub.secrets() + [PUSHER]:
            self.assertNotIn(secret, output, "a JWT, the App's token or the pusher's is in the output")

    def assertGone(self):
        self.assertFalse(os.path.exists(self.credentials), "the stage's tokens were left on the node")
        tmp = os.path.join(self.work, "tmp")
        self.assertEqual([name for name in os.listdir(tmp) if name.endswith("-publish")], [], "the executor's copy was left")

    def test_build_gets_a_read_token_alone_and_revokes_it(self):
        with stub_github.StubGitHub(KEY.public_key(), granted={"contents": "read"}) as stub:
            status, output, _ = self.run_stage(stub, "build")
        self.assertEqual(status, 0, output)
        seen = self.seen("build")
        self.assertEqual((seen["argv"], seen["cwd"]), (["build"], self.src))
        self.assertEqual(seen["github"]["value"], stub_github.TOKEN)
        self.assertEqual(seen["github"]["mode"], 0o600)
        self.assertIsNone(seen["pusher"], "build has no pusher token")
        self.assertEqual(stub.calls[1]["body"]["permissions"], {"contents": "read"})
        self.assertTrue(stub.revoked(), "the stage's token is revoked when it ends")
        for key, value in {**ENDPOINTS, "ZUUL_PIPELINE": "post", "ZUUL_OLDREV": "1" * 40, "ZUUL_NEWREV": "2" * 40,
                           "ZUUL_BUILD": "b" * 32, "ZUUL_STATE": self.root,
                           "ZUUL_BUILD_URL": f"https://zuul.example/t/aid/build/{'b' * 32}"}.items():
            self.assertEqual(seen["env"][key], value, key)
        self.assertNoSecret(stub, output)
        self.assertGone()

    def test_push_gets_the_pushers_token_and_no_app_token(self):
        with stub_github.StubGitHub(KEY.public_key()) as stub:
            status, output, _ = self.run_stage(stub, "push")
        self.assertEqual(status, 0, output)
        seen = self.seen("push")
        self.assertEqual((seen["pusher"]["value"], seen["pusher"]["mode"]), (PUSHER, 0o600))
        self.assertIsNone(seen["github"])
        self.assertEqual(stub.routes(), [], "push asked GitHub for nothing")
        self.assertNoSecret(stub, output)
        self.assertGone()

    def test_a_failed_stage_fails_and_still_revokes_and_removes_its_tokens(self):
        with stub_github.StubGitHub(KEY.public_key()) as stub:
            status, output, _ = self.run_stage(stub, "record", exit_status=1)
        self.assertNotEqual(status, 0, output)
        self.assertEqual(self.seen("record")["github"]["value"], stub_github.TOKEN)
        self.assertTrue(stub.revoked())
        self.assertNoSecret(stub, output)
        self.assertGone()

    def test_an_endpoint_without_a_value_fails_before_anything(self):
        with stub_github.StubGitHub(KEY.public_key(), granted={"contents": "read"}) as stub:
            status, output, _ = self.run_stage(stub, "build", endpoints={**ENDPOINTS, "BUILDKIT_HOST": ""})
        self.assertNotEqual(status, 0, output)
        self.assertIn("has no value for BUILDKIT_HOST", " ".join(output.split()))
        self.assertEqual(stub.routes(), [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "seen-build.json")))

    def test_report_runs_only_after_a_failure(self):
        with stub_github.StubGitHub(KEY.public_key(), granted={"issues": "write"}) as stub:
            status, output, _ = self.run_stage(stub, "report", success=True)
            self.assertEqual(status, 0, output)
            self.assertFalse(os.path.exists(os.path.join(self.root, "seen-report.json")))
            self.assertEqual(stub.routes(), [])
            status, output, _ = self.run_stage(stub, "report", success=False)
        self.assertEqual(status, 0, output)
        self.assertEqual(self.seen("report")["github"]["value"], stub_github.TOKEN)
        self.assertEqual(stub.calls[1]["body"]["permissions"], {"issues": "write"})
        self.assertTrue(stub.revoked())
        self.assertGone()


REPORT = os.path.join(ROOT, "playbooks", "nightly", "report.py")


class Stand(ThreadingHTTPServer):
    """Zuul's builds API and GitHub's issues, as far as report.py reads and writes them."""

    def __init__(self, builds, issues):
        self.builds, self.issues, self.calls = builds, issues, []
        stand = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def handle_call(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                stand.calls.append((method, self.path, body, self.headers.get("Authorization")))
                if self.path.startswith("/api/tenant/aid/builds?"):
                    return self.answer(200, stand.builds.pop(0) if len(stand.builds) > 1 else stand.builds[0])
                if self.path.startswith("/repos/kristofk/AIDepartment/issues?"):
                    return self.answer(200, stand.issues)
                if method == "POST" and self.path == "/repos/kristofk/AIDepartment/issues":
                    return self.answer(201, {"number": 99})
                return self.answer(200 if method == "PATCH" else 201, {})

            def do_GET(self):
                self.handle_call("GET")

            def do_POST(self):
                self.handle_call("POST")

            def do_PATCH(self):
                self.handle_call("PATCH")

        super().__init__(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()


class NightlyReportTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="nightly-")
        self.addCleanup(shutil.rmtree, self.work)
        self.publisher = os.path.join(self.work, "publisher")
        self.write(self.publisher, "zuul\n")
        self.token = os.path.join(self.work, "token")
        self.write(self.token, "ghs_nightly")

    @staticmethod
    def write(path, text):
        with open(path, "w") as handle:
            handle.write(text)

    def report(self, stand, now, token=True):
        args = [sys.executable, "-I", REPORT, "--publisher-file", self.publisher, "--zuul", stand.url, "--tenant", "aid",
                "--buildset", "s" * 32, "--build-url", "https://zuul.example/t/aid/buildset/x", "--wait", "5",
                "--now", now, "--api", stand.url] + (["--token-file", self.token] if token else [])
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        return result.returncode, json.loads(result.stdout)

    def test_only_the_timer_at_four_in_madrid_reports(self):
        with Stand([[{"result": "SUCCESS"}]], []) as stand:
            for now, report in [("2026-07-10T02:00:30+00:00", True), ("2026-07-10T03:00:30+00:00", False),
                                ("2026-01-10T02:00:30+00:00", False), ("2026-01-10T03:00:30+00:00", True),
                                # The changes of 2026: 29 March and 25 October, at 01:00 UTC.
                                ("2026-03-29T02:01:00+00:00", True), ("2026-10-25T02:01:00+00:00", False),
                                ("2026-10-25T03:01:00+00:00", True)]:
                status, decided = self.report(stand, now, token=False)
                self.assertEqual((status, decided["report"]), (0, report), (now, decided))
            self.assertEqual(stand.calls, [], "deciding reads nothing")

    def test_only_while_ci_publisher_names_zuul(self):
        self.write(self.publisher, "actions\n")
        with Stand([[{"result": "FAILURE"}]], []) as stand:
            status, decided = self.report(stand, "2026-07-10T02:00:30+00:00")
        self.assertEqual((status, decided["report"]), (0, False))
        self.assertIn("names 'actions'", decided["why"])
        self.assertEqual(stand.calls, [])

    def test_a_pass_closes_the_open_issues(self):
        with Stand([[], [{"result": None}], [{"result": "SUCCESS", "uuid": "u"}]], [{"number": 5}, {"number": 3, "pull_request": {}}]) as stand:
            # Polled every minute in production; the stand-in answers at once, so the test waits on --wait only.
            stand.builds = [[{"result": "SUCCESS", "uuid": "u"}]]
            status, done = self.report(stand, "2026-07-10T02:00:30+00:00")
        self.assertEqual((status, done["action"], done["issues"]), (0, "closed", [5]), done)
        writes = [(method, path, body) for method, path, body, _ in stand.calls if method != "GET"]
        self.assertEqual(writes, [("POST", "/repos/kristofk/AIDepartment/issues/5/comments", {"body": "Passed in https://zuul.example/t/aid/buildset/x"}),
                                  ("PATCH", "/repos/kristofk/AIDepartment/issues/5", {"state": "closed", "state_reason": "completed"})])
        self.assertTrue(all(auth == "Bearer ghs_nightly" for method, path, _, auth in stand.calls if path.startswith("/repos/")))

    def test_a_failure_opens_the_issue_or_comments_on_it(self):
        with Stand([[{"result": "FAILURE"}]], []) as stand:
            status, done = self.report(stand, "2026-07-10T02:00:30+00:00")
        self.assertEqual((status, done["action"], done["issues"]), (0, "created", [99]), done)
        created = [body for method, path, body, _ in stand.calls if method == "POST"][0]
        self.assertEqual((created["title"], created["labels"]), ("Nightly CI failed", ["ci-nightly"]))
        self.assertIn("ended with `FAILURE`", created["body"])
        with Stand([[{"result": "TIMED_OUT"}]], [{"number": 7}, {"number": 9}]) as stand:
            status, done = self.report(stand, "2026-07-10T02:00:30+00:00")
        self.assertEqual((status, done["action"], done["issues"]), (0, "commented", [9]), done)

    def test_no_result_in_time_is_a_failure(self):
        with Stand([[{"result": None}]], []) as stand:
            status, done = self.report(stand, "2026-07-10T02:00:30+00:00")
        self.assertEqual((status, done["result"], done["action"]), (0, None, "created"), done)
        body = [body for method, path, body, _ in stand.calls if method == "POST"][0]["body"]
        self.assertIn("has no result after the job allowed for it", body)


if __name__ == "__main__":
    unittest.main()

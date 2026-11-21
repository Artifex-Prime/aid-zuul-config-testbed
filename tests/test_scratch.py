"""Scratch lifecycle wiring with real Ansible/Zuul plugins and isolated hook/API doubles."""
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import yaml
from test_playbooks import ROOT, ansible, setUpModule  # noqa: F401


class ScratchTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="scratch-playbook-")
        self.addCleanup(shutil.rmtree, self.work)
        self.root = Path(self.work)
        shutil.copytree(Path(ROOT) / "playbooks", self.root / "playbooks")
        self.src = self.root / "src"
        (self.src / "ci/zuul").mkdir(parents=True)
        self.hook = self.root / "hook"
        self.hook.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
state = pathlib.Path(os.environ['CI_SCRATCH_STATE']); state.mkdir(parents=True, exist_ok=True)
with (state / 'calls').open('a') as out: out.write(json.dumps({'args': sys.argv[1:], 'build': os.environ['GITHUB_RUN_ID'], 'attempt': os.environ['GITHUB_RUN_ATTEMPT'], 'registry': os.environ['CI_SCRATCH_REGISTRY']}) + '\\n')
sys.exit(7 if os.environ.get('TEST_FAIL') == '1' else 0)
''')
        self.hook.chmod(0o755)
        cleanup = self.src / "ci/zuul/scratch-cleanup.sh"
        shutil.copyfile(self.hook, cleanup)
        cleanup.chmod(0o755)
        self.vars = {
            "zuul": {"build": "a" * 32, "project": {"src_dir": str(self.src)}},
            "publish_root": str(self.root / "state"), "scratch_control_hook": str(self.hook),
            "publish_endpoints": {"CI_SCRATCH_LIFECYCLE": "1", "CI_SCRATCH_REGISTRY": "scratch:5000"},
        }

    def run_book(self, name):
        # Each invocation gets its own plugin directory, as the executor does.
        shutil.rmtree(self.root / "zuul-ansible", ignore_errors=True)
        return ansible(str(self.root / "playbooks/publish" / name), self.vars, self.work)

    def calls(self):
        return [json.loads(line) for line in (self.root / "state/scratch/calls").read_text().splitlines()]

    def test_admit_and_cleanup_share_attempt_and_durable_state(self):
        for name in ("scratch-admit.yaml", "scratch-cleanup.yaml"):
            code, output, _ = self.run_book(name)
            self.assertEqual(code, 0, output)
        seen = self.calls()
        self.assertEqual([call["args"] for call in seen], [["admit"], ["a" * 32]])
        self.assertTrue(all(call["build"] == "a" * 32 and call["attempt"] == "1" and call["registry"] == "scratch:5000" for call in seen))

    def test_rollout_off_skips_both_hook_and_cleanup(self):
        for flag in ("0", None):
            if flag is None:
                self.vars["publish_endpoints"].pop("CI_SCRATCH_LIFECYCLE", None)
            else:
                self.vars["publish_endpoints"]["CI_SCRATCH_LIFECYCLE"] = flag
            for name in ("scratch-admit.yaml", "scratch-cleanup.yaml"):
                code, output, _ = self.run_book(name)
                self.assertEqual(code, 0, output)
        self.assertFalse((self.root / "state").exists())

    def test_refused_admit_or_failed_cleanup_is_visible(self):
        self.vars["publish_endpoints"]["TEST_FAIL"] = "1"
        for name in ("scratch-admit.yaml", "scratch-cleanup.yaml"):
            code, output, _ = self.run_book(name)
            self.assertNotEqual(code, 0, output)

    def test_hourly_executor_request_uses_current_build_and_propagates_refusal(self):
        requests = []
        status = [200]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(status[0]); self.end_headers(); self.wfile.write(b'{}')
            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.vars['scratch_control_url'] = f'http://127.0.0.1:{server.server_port}'
        for expected in (200, 409, 503):
            status[0] = expected
            shutil.rmtree(self.root / 'zuul-ansible', ignore_errors=True)
            code, output, _ = ansible(str(self.root / 'playbooks/scratch/maintenance.yaml'), self.vars, self.work)
            self.assertEqual(code == 0, expected == 200, output)
        self.assertEqual(requests, [('/admit', {'build': 'a' * 32})] * 3)


class LayoutTest(unittest.TestCase):
    def test_all_post_producer_consumers_hold_same_slot_through_cleanup(self):
        data = yaml.safe_load((Path(ROOT) / 'zuul.d/publish.yaml').read_text())
        jobs = {entry['job']['name']: entry['job'] for entry in data if 'job' in entry}
        for name in ('aid-publish-apps', 'aid-release', 'aid-publish-images'):
            job = jobs[name]
            self.assertEqual(job['semaphores'], ['aid-scratch-registry'])
            self.assertEqual(job['pre-run'], ['playbooks/secrets/guard.yaml', 'playbooks/publish/scratch-admit.yaml'])
            cleanup = {'name': 'playbooks/publish/scratch-cleanup.yaml', 'cleanup': True}
            self.assertEqual(job['post-run'].count(cleanup), 1)
            self.assertEqual(job['vars']['publish_endpoints']['CI_SCRATCH_LIFECYCLE'], '0')

    def test_hourly_job_is_executor_only_and_not_enrolled_before_rollout(self):
        data = yaml.safe_load((Path(ROOT) / 'zuul.d/scratch.yaml').read_text())
        pipeline = next(entry['pipeline'] for entry in data if 'pipeline' in entry)
        job = next(entry['job'] for entry in data if 'job' in entry)
        self.assertEqual(pipeline['trigger']['timer'], [{'time': '0 * * * *'}])
        self.assertTrue(pipeline['post-review'])
        self.assertEqual(job['nodeset'], {'nodes': []})
        self.assertEqual(job['semaphores'], ['aid-scratch-registry'])
        self.assertEqual(job['attempts'], 1)
        self.assertFalse(any('project' in entry for entry in data))

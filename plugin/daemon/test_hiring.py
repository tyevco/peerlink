"""Tests for hiring workers: the worker flag and its limits, retire, the cap and its queue, the night guard, and
starting agents in herdr core's terminals. Run: python3 -m unittest discover -s plugin/daemon"""
import copy
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-hiring-test-"))

import peerlinkd  # noqa: E402

d = peerlinkd


class HiringTest(unittest.TestCase):
    """lead hires workers into folders under /tmp; other is an agent that hired nobody."""

    def setUp(self):
        d.load_config()
        d.load_discord()
        self.kept = copy.deepcopy(({k: d.config.get(k) for k in ("max_live_spawns", "night_guard", "auto_approve_folders")}, d.spawns, d.tasks, d.messages))
        self.addCleanup(self.restore)
        self.dirs = [tempfile.mkdtemp(prefix="hire-") for _ in range(3)]
        d.config.update({"rate_limit_per_min": 100, "auto_approve_folders": ["/x/lead"], "max_live_spawns": 0, "night_guard": ""})
        now = time.time()
        for n in ("lead", "other"):
            d.sessions["s-" + n] = {"id": "s-" + n, "name": n, "cwd": "/x/" + n, "agent": "a-" + n, "last_seen": now}
        d.agent_of.update({"s-lead": "a-lead", "s-other": "a-other"})
        d.spawns.clear()
        d.inboxes.clear()
        launched = self.launched = []

        def fake_launch(sp):
            launched.append(sp["id"])
            return "in a test terminal", ""
        self.trusted, self.real_trust = [], d.trust_folder
        for target, fake in (("launch", fake_launch), ("hard_stop", lambda *a, **k: None), ("trust_folder", self.trusted.append)):
            p = unittest.mock.patch.object(d, target, fake)
            p.start()
            self.addCleanup(p.stop)

    def restore(self):
        conf, spawns, tasks, messages = self.kept
        d.config.update(conf)
        for sid in [s for s in d.sessions if s.startswith("s-")]:
            d.sessions.pop(sid)
        for store, was in ((d.spawns, spawns), (d.tasks, tasks)):
            store.clear()
            store.update(was)
        d.messages[:] = messages
        d.inboxes.clear()

    def hire(self, i=0, **extra):
        status, out = d.request_spawn({"by": "s-lead", "dir": self.dirs[i], "purpose": "one ticket", "first_prompt": "Do the ticket",
                                       "name": f"w{i}", "worker": True, **extra})
        self.assertEqual(status, 200, out)
        return out["id"]

    def register(self, spawn_id, name):
        """The started worker's agent checks in, as the plugin's registration would record it."""
        sid, aid = "s-" + name, "a-" + name
        d.sessions[sid] = {"id": sid, "name": name, "cwd": d.spawns[spawn_id]["dir"], "agent": aid, "last_seen": time.time()}
        d.agent_of[sid] = aid
        d.spawns[spawn_id].update(agent_id=sid, agent=aid)
        return sid

    def told(self, name):
        return [m["text"] for m in d.inboxes.get("s-" + name, [])]

    def test_a_worker_is_started_and_cant_hire_or_schedule(self):
        sp = self.hire()
        self.assertEqual((d.spawns[sp]["status"], d.spawns[sp]["worker"]), ("started", True))
        sid = self.register(sp, "w0")
        refused = d.request_spawn({"by": sid, "dir": self.dirs[1], "purpose": "help", "first_prompt": "go"})
        self.assertEqual(refused[0], 403)
        self.assertIn("ask your lead", refused[1]["error"])
        self.assertEqual(d.create_schedule({"prompt": "check later", "at": "in 1h"}, d.sessions[sid])[0], 403)
        # The block is the agent's: a cleared context, a new session of the same agent, is still a worker.
        d.sessions["s-w0b"] = dict(d.sessions[sid], id="s-w0b")
        self.assertEqual(d.request_spawn({"by": "s-w0b", "dir": self.dirs[1], "purpose": "help", "first_prompt": "go"})[0], 403)
        self.assertEqual(d.client_config(sid)["hired_by"], "lead")
        self.assertEqual(d.client_config("s-lead")["hired_by"], "")

    def test_past_the_cap_a_hire_queues_and_starts_when_a_worker_is_gone(self):
        d.config["max_live_spawns"] = 1
        first = self.hire(0)
        sid = self.register(first, "w0")
        second = self.hire(1)
        self.assertEqual(d.spawns[second]["status"], "queued")
        self.assertIn(f"{second} is queued: 1 of 1 workers", self.told("lead")[-1])
        self.assertEqual(self.launched, [first])
        # A standing agent started the old way isn't capped.
        standing = d.request_spawn({"by": "s-lead", "dir": self.dirs[2], "purpose": "restart a lead", "first_prompt": "go", "name": "lead2"})[1]["id"]
        self.assertEqual(d.spawns[standing]["status"], "started")
        d.start_queued()
        self.assertEqual(d.spawns[second]["status"], "queued")
        d.sessions.pop(sid)  # the first worker retired
        d.start_queued()
        self.assertEqual(d.spawns[second]["status"], "started")
        self.assertIn(f"{second} left the queue", self.told("lead")[-1])

    def test_a_worker_retires_once_its_tasks_are_closed_and_its_lead_hears_how_they_ended(self):
        sp = self.hire()
        sid = self.register(sp, "w0")
        status, out = d.send({"from": "s-lead", "to": "w0", "kind": "task", "text": "Fix the login copy"})
        self.assertEqual(status, 200, out)
        tid = out["task"]
        status, out = d.retire(sid, {})
        self.assertEqual(status, 409)
        self.assertIn(f"you still own {tid}", out["error"])
        self.assertEqual(d.update_task(tid, {"by": sid, "status": "done", "note": "Fixed."})[0], 200)
        with unittest.mock.patch.object(d, "terminate", wraps=d.terminate) as ended:
            self.assertEqual(d.retire(sid, {})[0], 200)
            ended.assert_called_once()
        self.assertEqual(d.spawns[sp]["status"], "retired")
        self.assertIn(f"w0 retired, {tid} done.", self.told("lead"))

    def test_only_the_lead_that_hired_a_worker_retires_it_and_a_standing_agent_never_is(self):
        sp = self.hire()
        sid = self.register(sp, "w0")
        self.assertEqual(d.retire("s-other", {"agent": "w0"})[0], 403)
        status, out = d.retire("s-lead", {})
        self.assertEqual(status, 403)
        self.assertIn("you stay running", out["error"])
        status, out = d.retire("s-lead", {"agent": "w0"})
        self.assertEqual(status, 200, out)
        self.assertTrue(d.sessions[sid].get("terminate"))

    def repo(self):
        root = tempfile.mkdtemp(prefix="wt-")
        repo = os.path.join(root, "app")
        os.mkdir(repo)
        for args in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "start"]):
            d.subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)
        return repo

    def test_a_worker_starts_in_a_new_worktree_made_when_it_starts_and_kept_when_it_retires(self):
        repo = self.repo()
        sp = self.hire(dir="", worktree={"repo": repo, "branch": "fix/login-copy", "base": "main"})
        path = repo + "-wt-fix-login-copy"
        self.assertEqual(d.spawns[sp]["dir"], os.path.realpath(path) if os.path.realpath(repo) != repo else path)
        wt = d.spawns[sp]["worktree"]
        self.assertTrue(wt["created"] and os.path.isdir(wt["path"]))
        self.assertEqual(d.git(wt["path"], "rev-parse", "--abbrev-ref", "HEAD"), (0, "fix/login-copy"))
        self.assertEqual(self.trusted, [wt["path"]])
        # A lead-made link with another agent ends when it retires; its worktree stays, and the lead is told how to remove it.
        sid = self.register(sp, "w0")
        d.config.setdefault("agent_links", []).append(["a-other", "a-w0"])
        d.config.setdefault("link_info", {})["a-other|a-w0"] = {"by": ["lead"], "why": "", "task": "", "ts": 0}
        self.assertEqual(d.retire(sid, {})[0], 200)
        self.assertNotIn(["a-other", "a-w0"], d.config["agent_links"])
        self.assertTrue(os.path.isdir(wt["path"]))
        self.assertIn(f"worktree remove", self.told("lead")[-1])
        # An existing branch is checked out rather than made again; a folder in the way, or no repo, is refused up front.
        d.git(repo, "branch", "fix/other")
        other = self.hire(1, dir="", worktree={"repo": repo, "branch": "fix/other"})
        self.assertEqual(d.git(d.spawns[other]["dir"], "rev-parse", "--abbrev-ref", "HEAD"), (0, "fix/other"))
        busy = tempfile.mkdtemp()
        status, out = d.request_spawn({"by": "s-lead", "dir": busy, "purpose": "p", "first_prompt": "f", "worktree": {"repo": repo, "branch": "x"}})
        self.assertEqual(status, 400)
        self.assertIn("isn't a worktree on x", out["error"])
        self.assertEqual(d.request_spawn({"by": "s-lead", "purpose": "p", "first_prompt": "f", "worktree": {"repo": busy, "branch": "x"}})[0], 400)

    def test_dir_naming_the_repo_puts_the_worktree_beside_it(self):
        repo = self.repo()
        sp = self.hire(dir=repo, worktree={"repo": repo, "branch": "SKY-1-relay"})
        self.assertEqual(os.path.basename(d.spawns[sp]["dir"]), "app-wt-SKY-1-relay")
        # The repo can come from dir alone.
        other = self.hire(1, dir=repo, worktree={"branch": "SKY-2-copy"})
        self.assertEqual(d.git(d.spawns[other]["dir"], "rev-parse", "--abbrev-ref", "HEAD"), (0, "SKY-2-copy"))

    def test_a_session_reloaded_onto_newer_tools_is_told_what_they_take_once(self):
        rec = d.sessions["s-lead"]
        rec["plugin"] = "0.9.65"
        with d.lock:
            d.tool_changes_notice(rec, "0.9.65", "0.9.67")
            d.tool_changes_notice(rec, "0.9.66", "0.9.67")
            d.tool_changes_notice(rec, "", "0.9.67")
        told = [m for m in d.inboxes.get("s-lead", []) if m["from"] == "peerlink"]
        self.assertEqual(len(told), 1)
        self.assertEqual(told[0]["kind"], "announcement")  # read at its next turn, never starts one
        self.assertIn("worktree", told[0]["text"])

    def test_a_worktree_beside_its_repo_sits_in_its_repos_space(self):
        repo = self.repo()
        for folder, text in ((os.path.dirname(repo), "space: skyforge\n"), (repo, "space: pixelgrid\nparent: skyforge\nrole: the lead\n")):
            with open(os.path.join(folder, ".peerlink"), "w") as f:
                f.write(text)
        wt = repo + "-wt-fix"
        d.git(repo, "worktree", "add", "-q", "-b", "fix", wt)
        self.assertEqual(os.path.realpath(d.find_space_file(wt)), os.path.realpath(os.path.join(repo, ".peerlink")))
        rec = {"id": "s-x", "cwd": wt}
        self.assertEqual(d.space_of(rec)[:2], ("pixelgrid", "file"))
        self.assertEqual(d.folder_role(rec), "")  # the lead's role stays the lead's
        # A plain folder beside the repo still finds the one above it.
        plain = repo + "-notes"
        os.mkdir(plain)
        self.assertEqual(d.find_space_file(plain), os.path.join(os.path.dirname(repo), ".peerlink"))

    def test_a_hire_says_its_model_and_effort_and_gets_them(self):
        status, out = d.request_spawn({"by": "s-lead", "dir": self.dirs[0], "purpose": "p", "first_prompt": "f",
                                        "model": "opus", "effort": "high", "worker": True})
        self.assertEqual((status, out["model"], out["effort"]), (200, "opus", "high"))
        self.assertIn("--model opus --effort high", out["note"])
        self.assertEqual(d.launch_args(d.spawns[out["id"]]), ["--model", "opus", "--effort", "high"])
        for bad in ({"effort": "huge"}, {"model": "opus; rm -rf ~"}):
            status, out = d.request_spawn({"by": "s-lead", "dir": self.dirs[1], "purpose": "p", "first_prompt": "f", **bad})
            self.assertEqual(status, 400, bad)

    def test_a_folder_is_marked_trusted_only_when_nothing_above_it_is(self):
        home = tempfile.mkdtemp()
        conf = os.path.join(home, ".claude.json")
        with open(conf, "w") as f:
            json.dump({"projects": {os.path.join(home, "Repos"): {"hasTrustDialogAccepted": True}}, "other": 1}, f)
        with unittest.mock.patch.dict(os.environ, {"HOME": home}):
            self.real_trust(os.path.join(home, "Repos", "app-wt-x"))
            with open(conf) as f:
                self.assertEqual(len(json.load(f)["projects"]), 1)  # under a trusted folder already
            self.real_trust(os.path.join(home, "work", "app-wt-y"))
            with open(conf) as f:
                data = json.load(f)
        self.assertTrue(data["projects"][os.path.join(home, "work", "app-wt-y")]["hasTrustDialogAccepted"])
        self.assertEqual(data["other"], 1)

    def test_an_auto_approved_start_that_fails_isnt_posted_as_started(self):
        posts = []
        for target, fake in (("approval_channel", lambda rec: "c1"), ("chat_ready", lambda channel="": True),
                             ("post_async", posts.extend), ("close_buttons", lambda item: None)):
            p = unittest.mock.patch.object(d, target, fake)
            p.start()
            self.addCleanup(p.stop)
        self.hire(0)
        with unittest.mock.patch.object(d, "launch", lambda sp: ("", "starting an agent needs herdr core (hc) on this machine")):
            self.hire(1)
        self.assertEqual([t for _, t, _ in posts], ["**lead** started w0", "**lead** couldn't start w1: starting an agent needs herdr core (hc) on this machine"])

    def test_the_night_guard_holds_its_hours_and_refuses_hires_in_them(self):
        at = lambda h: time.mktime((2026, 11, 21, h, 30, 0, 0, 0, -1))
        d.config["night_guard"] = "22-8"
        self.assertEqual([d.night_guard_on(at(h)) for h in (21, 22, 3, 8)], [False, True, True, False])
        d.config["night_guard"] = "1-5"
        self.assertEqual([d.night_guard_on(at(h)) for h in (0, 1, 4, 5)], [False, True, True, False])
        with unittest.mock.patch.object(d, "night_guard_on", lambda now=None: True):
            status, out = d.request_spawn({"by": "s-lead", "dir": self.dirs[0], "purpose": "p", "first_prompt": "f", "worker": True})
        self.assertEqual(status, 409)
        self.assertIn("night guard", out["error"])
        self.assertEqual(d.config_value("night_guard", "25-3")[0], None)
        self.assertEqual(d.config_value("night_guard", " 22-8 "), ("22-8", None))
        self.assertIn("max_live_spawns", d.BROWSER_ONLY_SETTINGS)


class HerdrLaunchTest(unittest.TestCase):
    def setUp(self):
        # No login shell to ask about an alias, unless a test says otherwise; no herdr app to name tabs.
        for target, fake in (("user_shell", lambda: ""), ("name_herdr_tab", lambda *a: None)):
            p = unittest.mock.patch.object(d, target, fake)
            p.start()
            self.addCleanup(p.stop)
        # The settings a running daemon has (peer_ttl_s and so on), where no other test loaded them.
        p = unittest.mock.patch.dict(d.config, {k: v for k, v in d.DEFAULT_CONFIG.items() if k not in d.config})
        p.start()
        self.addCleanup(p.stop)

    def test_claude_starts_through_your_shell_when_you_have_an_alias_for_it(self):
        def run_with(out):
            return lambda argv, **kw: unittest.mock.Mock(returncode=0, stdout=out, stderr="")
        with unittest.mock.patch.object(d, "user_shell", lambda: "/bin/zsh"), unittest.mock.patch.dict(os.environ, {"PEERLINK_CLAUDE": ""}):
            with unittest.mock.patch.object(d.subprocess, "run", run_with("claude: alias\n")):
                self.assertEqual(d.claude_argv("/bin/claude"), ["/bin/zsh", "-lic", "claude"])
                self.assertEqual(d.claude_argv("/bin/claude", ["--model", "opus"]), ["/bin/zsh", "-lic", "claude --model opus"])
            with unittest.mock.patch.object(d.subprocess, "run", run_with("claude: command\n")):
                self.assertEqual(d.claude_argv("/bin/claude"), ["/bin/claude"])
                self.assertEqual(d.claude_argv("/bin/claude", ["--effort", "low"]), ["/bin/claude", "--effort", "low"])

    def test_an_agent_starts_in_a_herdr_terminal_that_outlives_the_daemons(self):
        sp = {"id": "s9", "dir": "/x/api", "name": "pg-client", "space": "pixelgrid"}
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            out = {"address": "t42"} if argv[1] == "run" else {"state": "live", "pid": 10, "foreground": {"pid": 11}}
            return unittest.mock.Mock(returncode=0, stdout=json.dumps(out), stderr="")
        with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                unittest.mock.patch.object(d.subprocess, "run", run):
            how, err = d.launch(sp)
            self.assertEqual((how, err, sp["hc"]), ("in a herdr core terminal (hc attach t42)", "", "t42"))
            self.assertEqual(d.hc_pid("t42"), 11)
        argv = calls[0]
        for flag in ("--bg", "--no-parent", "--outlive-parent", "--rm"):
            self.assertIn(flag, argv)
        self.assertNotIn("--keep", argv)
        self.assertIn("peerlink.spawn=s9", argv)
        self.assertEqual(argv[argv.index("--cwd") + 1], "/x/api")
        self.assertEqual(argv[-1], "/bin/claude")

    def test_a_hire_opens_in_its_leads_herdr_app_space(self):
        d.sessions["s-lead9"] = {"id": "s-lead9", "name": "lead9", "cwd": "/x/lead9", "pid": 77, "last_seen": time.time()}
        self.addCleanup(d.sessions.pop, "s-lead9", None)
        sp = {"id": "s9", "dir": "/x/api", "name": "pg-client", "space": "pixelgrid", "requester": {"id": "s-lead9", "cwd": "/x/lead9"}}
        terms = {"t1": {"cwd": "/x/lead9", "labels": {"herdr.space": "sp-other"}, "foreground": {"pid": 5}},
                 "t2": {"cwd": "/x/lead9", "labels": {"herdr.space": "sp-lead"}, "foreground": {"pid": 77}}}
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if argv[1] == "ls":
                out = {"terminals": [{"address": a, "cwd": t["cwd"], "state": "live"} for a, t in terms.items()]}
            elif argv[1] == "get":
                out = {"state": "live", **terms.get(argv[2], {})}
            else:
                out = {"address": "t42"}
            return unittest.mock.Mock(returncode=0, stdout=json.dumps(out), stderr="")
        with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                unittest.mock.patch.object(d.subprocess, "run", run):
            self.assertEqual(d.launch(sp), ("in the herdr app (hc attach t42)", ""))
        argv = next(a for a in calls if a[1] == "run")
        self.assertIn("herdr.space=sp-lead", argv)
        self.assertTrue(any(a.startswith("herdr.tab=tb") for a in argv))  # its own tab, named after it
        self.assertEqual(argv[-1], "/bin/claude")

    def test_with_the_herdr_app_off_a_hire_runs_in_herdr_core_unshown(self):
        d.sessions["s-lead9"] = {"id": "s-lead9", "name": "lead9", "cwd": "/x/lead9", "pid": 77, "last_seen": time.time()}
        self.addCleanup(d.sessions.pop, "s-lead9", None)
        sp = {"id": "s9", "dir": "/x/api", "name": "pg-client", "space": "pixelgrid", "requester": {"id": "s-lead9", "cwd": "/x/lead9"}}
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            out = ({"terminals": [{"address": "t2", "cwd": "/x/lead9", "state": "live"}]} if argv[1] == "ls"
                   else {"state": "live", "labels": {"herdr.space": "sp-lead"}, "foreground": {"pid": 77}} if argv[1] == "get"
                   else {"address": "t42"})
            return unittest.mock.Mock(returncode=0, stdout=json.dumps(out), stderr="")
        with unittest.mock.patch.dict(d.config, {"use_herdr": False}), unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), \
                unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), unittest.mock.patch.object(d.subprocess, "run", run):
            how, err = d.launch(sp)
        self.assertEqual((how, err), ("in a herdr core terminal (hc attach t42)", ""))
        argv = next(a for a in calls if a[1] == "run")
        self.assertFalse(any(a.startswith(("herdr.space=", "herdr.tab=")) for a in argv), argv)
        self.assertIn("peerlink.spawn=s9", argv)
        self.assertNotIn("ls", [a[1] for a in calls])  # the app's spaces aren't even looked up

    def test_a_worker_nests_under_its_leads_terminal(self):
        d.sessions["s-lead9"] = {"id": "s-lead9", "name": "lead9", "cwd": "/x/lead9", "pid": 77, "terminal": "hc:t2@50",
                                 "last_seen": time.time()}
        self.addCleanup(d.sessions.pop, "s-lead9", None)
        for worker, lead_pid, parented in ((True, 50, True), (True, 51, False), (False, 50, False)):
            with self.subTest(worker=worker, lead_pid=lead_pid):
                sp = {"id": "s9", "dir": "/x/api", "name": "pg-client", "space": "pixelgrid", "worker": worker,
                      "requester": {"id": "s-lead9", "cwd": "/x/lead9"}}
                calls = []

                def run(argv, **kw):
                    calls.append(argv)
                    out = {"terminals": []} if argv[1] == "ls" else {"state": "live", "pid": lead_pid} if argv[1] == "get" else {"address": "t42"}
                    return unittest.mock.Mock(returncode=0, stdout=json.dumps(out), stderr="")
                with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                        unittest.mock.patch.object(d.subprocess, "run", run):
                    self.assertEqual(d.launch(sp)[1], "")
                argv = next(a for a in calls if a[1] == "run")
                self.assertIn("--outlive-parent", argv)
                if parented:  # its lead's terminal still runs the lead's shell: nested under it, quietly
                    self.assertEqual(argv[argv.index("--parent") + 1], "t2")
                    self.assertIn("--no-notify", argv)
                    self.assertNotIn("--no-parent", argv)
                else:  # not a worker, or the address now belongs to another shell
                    self.assertIn("--no-parent", argv)
                    self.assertNotIn("--parent", argv)

    def test_any_agent_but_the_lead_nests_under_its_spaces_lead(self):
        d.sessions["s-lead9"] = {"id": "s-lead9", "name": "lead9", "cwd": "/x/lead9", "pid": 77, "terminal": "hc:t2@50", "last_seen": time.time()}
        d.sessions["s-boss"] = {"id": "s-boss", "name": "boss", "cwd": "/x/boss", "pid": 78, "terminal": "hc:t5@55", "last_seen": time.time()}
        for sid in ("s-lead9", "s-boss"):
            self.addCleanup(d.sessions.pop, sid, None)
        leads = {"pixelgrid": {"cwd": "/x/lead9", "how": "set"}}
        pids = {"t2": 50, "t5": 55}
        cases = (("a standing agent in the space", {"dir": "/x/api", "space": "pixelgrid", "requester": {"id": "s-boss"}}, "t2"),
                 ("the lead restarting", {"dir": "/x/lead9", "space": "pixelgrid", "requester": {"id": "s-boss"}}, ""),
                 ("a worker in a space with no lead running", {"dir": "/x/api", "space": "games", "worker": True, "requester": {"id": "s-boss"}}, "t5"),
                 ("an agent in no space", {"dir": "/x/api", "space": "", "requester": {}}, ""))
        for why, extra, parent in cases:
            with self.subTest(why), unittest.mock.patch.dict(d.config, {"space_leads": leads}):
                sp = {"id": "s9", "name": "pg-client", **extra}
                calls = []

                def run(argv, **kw):
                    calls.append(argv)
                    out = {"terminals": []} if argv[1] == "ls" else {"state": "live", "pid": pids.get(argv[2], 0)} if argv[1] == "get" else {"address": "t42"}
                    return unittest.mock.Mock(returncode=0, stdout=json.dumps(out), stderr="")
                with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                        unittest.mock.patch.object(d.subprocess, "run", run), unittest.mock.patch.object(d, "space_of", lambda r: ("skyforge", "", "")), \
                        unittest.mock.patch.object(d, "lead_rec", lambda space: d.sessions["s-lead9"] if space == "pixelgrid" else None):
                    self.assertEqual(d.launch(sp)[1], "")
                argv = next(a for a in calls if a[1] == "run")
                self.assertEqual(argv[argv.index("--parent") + 1] if "--parent" in argv else "", parent)

    def test_when_hc_fails_the_agent_doesnt_start_and_says_why(self):
        sp = {"id": "s9", "dir": "/x/api", "name": "", "space": ""}
        seen = []

        def run(argv, **kw):
            seen.append(argv)
            return unittest.mock.Mock(returncode=1, stdout="", stderr="no server")
        links = {"/u/.local/bin/hc": "/u/.local/bin/herdr-core"}  # the installer's hc, a link to herdr-core
        for hc, hint in (("/u/.local/bin/herdr-core", False), ("/u/.local/bin/hc", False), ("/usr/bin/hc", True)):  # a bare hc may be another program
            with self.subTest(hc=hc), unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), \
                    unittest.mock.patch.object(d, "find_hc", lambda: hc), unittest.mock.patch.object(d.subprocess, "run", run), \
                    unittest.mock.patch.object(d.os.path, "realpath", lambda p: links.get(p, p)):
                seen.clear()
                how, err = d.launch(sp)
                self.assertEqual(how, "")
                self.assertTrue(err.startswith("hc couldn't start it: no server"), err)
                self.assertEqual(d.HC_INSTALL in err, hint, err)
                self.assertEqual({a[0] for a in seen}, {hc})  # nothing else is tried: no tmux, no Terminal window
                self.assertNotIn("hc", sp)

    def test_when_hc_hangs_or_cant_run_the_agent_doesnt_start_and_says_why(self):
        for boom in (d.subprocess.TimeoutExpired(["hc"], 20), OSError("exec format error")):
            sp = {"id": "s9", "dir": "/x/api", "name": "", "space": ""}

            def run(argv, **kw):
                raise boom
            with self.subTest(boom=type(boom).__name__), unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), \
                    unittest.mock.patch.object(d, "find_hc", lambda: "/bin/herdr-core"), unittest.mock.patch.object(d.subprocess, "run", run):
                how, err = d.launch(sp)
            self.assertEqual(how, "")
            self.assertTrue(err.startswith("hc couldn't start it: "), err)
            self.assertNotIn("hc", sp)

    def test_without_herdr_core_starting_an_agent_says_how_to_get_it(self):
        sp = {"id": "s9", "dir": "/x/api", "name": "", "space": ""}
        with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: ""), \
                unittest.mock.patch.object(d.subprocess, "run", lambda *a, **kw: self.fail("nothing runs without hc")):
            how, err = d.launch(sp)
        self.assertEqual(how, "")
        self.assertIn("needs herdr core (hc)", err)
        self.assertIn(d.HC_INSTALL, err)
        self.assertIn("releases.herdr.dev/core/install", d.HC_INSTALL)

    def test_with_the_herdr_app_off_hc_still_starts_agents(self):
        sp = {"id": "s9", "dir": "/x/api", "name": "", "space": ""}
        seen = []

        def run(argv, **kw):
            seen.append(argv)
            return unittest.mock.Mock(returncode=0, stdout=json.dumps({"address": "t7"}), stderr="")
        with unittest.mock.patch.dict(d.config, {"use_herdr": False}), unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), \
                unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), unittest.mock.patch.object(d.subprocess, "run", run):
            how, err = d.launch(sp)
        self.assertEqual((how, err), ("in a herdr core terminal (hc attach t7)", ""))
        self.assertEqual(seen[0][:2], ["/bin/hc", "run"])

    def test_one_started_before_is_left_running_and_a_trust_prompt_there_is_said(self):
        trust = "Claude Code\n\nDo you trust this folder?\n > 1. Yes, I trust this folder\n"
        cases = (("waiting for trust", {"peerlink.spawn": "c3"}, trust, ("", "trust")),
                 ("still starting", {"peerlink.spawn": "c3"}, "Welcome back\n> ", ("still starting (hc attach t42)", "")),
                 # hc gave the ended terminal's address to another program: that isn't this one's, so a new one starts.
                 ("address reused", {"peerlink.spawn": "s5"}, trust, ("in a herdr core terminal (hc attach t43)", "")))
        for why, labels, screen, want in cases:
            with self.subTest(why):
                sp = {"id": "c3", "dir": "/x/api", "hc": "t42"}
                seen = []

                def run(argv, **kw):
                    seen.append(argv[1])
                    out = {"get": json.dumps({"state": "live", "foreground": {"pid": 11}, "labels": labels}), "read": screen,
                           "run": json.dumps({"address": "t43"})}[argv[1]]
                    return unittest.mock.Mock(returncode=0, stdout=out, stderr="")
                with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                        unittest.mock.patch.object(d.subprocess, "run", run):
                    how, err = d.launch(sp)
                self.assertEqual(how, want[0])
                if want[1]:
                    self.assertIn("trust the folder: run hc attach t42", err)
                else:
                    self.assertEqual(err, "")
                self.assertEqual("run" in seen, why == "address reused", seen)

    def test_a_schedule_doesnt_start_a_second_agent_while_its_first_waits_for_trust(self):
        c = {"id": "c3", "folder": "/x/api", "agent": "", "requester": {"cwd": "/x/lead"}}
        live, told, runs = {}, [], []

        def run(argv, **kw):
            if argv[1] == "run":
                runs.append(argv)
                live["t1"] = {"state": "live", "foreground": {"pid": 11}, "labels": {"peerlink.spawn": "c3"}}
                return unittest.mock.Mock(returncode=0, stdout=json.dumps({"address": "t1"}), stderr="")
            if argv[1] == "get":
                return unittest.mock.Mock(returncode=0 if argv[2] in live else 1, stdout=json.dumps(live.get(argv[2], {})), stderr="")
            return unittest.mock.Mock(returncode=0, stdout="Do you trust this folder?\n > 1. Yes, I trust this folder\n", stderr="")
        with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                unittest.mock.patch.object(d.subprocess, "run", run), unittest.mock.patch.object(d, "save_schedules", lambda: None), \
                unittest.mock.patch.object(d, "schedule_told", lambda c, text: told.append(text)):
            d.start_for_schedule(c)
            self.assertEqual((len(runs), c["hc"], c["note"]), (1, "t1", "started an agent in a herdr core terminal (hc attach t1)"))
            d.start_for_schedule(c)
            d.start_for_schedule(c)
            self.assertEqual(len(runs), 1)  # the first one is still there: no second agent
            self.assertIn("trust the folder: run hc attach t1", c["note"])
            self.assertEqual(len(told), 1, told)  # said once, not on every run
            live.clear()  # it's gone (you closed it): the next run starts a new one
            d.start_for_schedule(c)
            self.assertEqual(len(runs), 2)

    def schedule_hc(self, live, runs, screen="Do you trust this folder?\n > 1. Yes, I trust this folder\n"):
        """A fake hc for schedule starts: run makes t1, t2, ...; get says what `live` holds; read shows `screen`."""
        def run(argv, **kw):
            if argv[1] == "run":
                runs.append(argv)
                addr = f"t{len(runs)}"
                live[addr] = {"state": "live", "cwd": argv[argv.index("--cwd") + 1], "foreground": {"pid": 11},
                              "labels": {"peerlink.spawn": argv[argv.index("--label") + 1].split("=", 1)[1]}}
                return unittest.mock.Mock(returncode=0, stdout=json.dumps({"address": addr}), stderr="")
            if argv[1] == "get":
                return unittest.mock.Mock(returncode=0 if argv[2] in live else 1, stdout=json.dumps(live.get(argv[2], {})), stderr="")
            return unittest.mock.Mock(returncode=0, stdout=screen, stderr="")
        return run

    def test_a_schedules_agent_still_waiting_for_trust_keeps_its_name(self):
        c = {"id": "c3", "folder": "/x/api", "agent": "alex", "requester": {"cwd": "/x/lead"}}
        live, runs, told = {}, [], []
        self.addCleanup(d.name_claims.pop, "/x/api", None)
        with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                unittest.mock.patch.object(d.subprocess, "run", self.schedule_hc(live, runs)), \
                unittest.mock.patch.object(d, "save_schedules", lambda: None), unittest.mock.patch.object(d, "schedule_told", lambda c, t: told.append(t)):
            d.name_claims["/x/api"] = ["alex"]  # fire_schedule's claim, for the agent this starts
            d.start_for_schedule(c)
            d.start_for_schedule(c)  # the next run: it's at the trust prompt
            self.assertEqual(len(runs), 1)
            self.assertEqual(d.name_claims["/x/api"], ["alex"])  # it still comes up as alex once you trust the folder
            self.assertTrue(c["note"].startswith("alex hasn't come up: Claude Code there is waiting for you to trust the folder"), c["note"])
            self.assertIn("the agent it started in /x/api hasn't come up", told[0])
            live.clear()
            with unittest.mock.patch.object(d, "find_hc", lambda: ""):  # one that couldn't start at all gives the name up
                d.start_for_schedule(c)
            self.assertEqual(d.name_claims["/x/api"], [])

    def test_a_schedules_agent_missed_after_a_wake_isnt_started_twice(self):
        c = {"id": "c3", "folder": "/x/api", "agent": "alex", "cron": "0 * * * *", "prompt": "Check the queue.", "start": True,
             "requester": {"cwd": "/x/lead"}}
        live, runs = {}, []
        self.addCleanup(d.name_claims.pop, "/x/api", None)
        self.addCleanup(d.sessions.pop, "s-alex", None)
        self.addCleanup(d.starting.pop, "/x/api", None)
        with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                unittest.mock.patch.object(d.subprocess, "run", self.schedule_hc(live, runs, screen="> ")), \
                unittest.mock.patch.object(d, "save_schedules", lambda: None), unittest.mock.patch.object(d, "schedule_told", lambda c, t: None), \
                unittest.mock.patch.object(d, "record", lambda m: None):
            d.start_for_schedule(c)
            # Its agent came up from t1, and the next run finds it there.
            d.sessions["s-alex"] = {"id": "s-alex", "name": "alex", "cwd": "/x/api", "terminal": "hc:t1@1", "last_seen": time.time()}
            with d.lock:
                self.assertIsNone(d.fire_schedule(c, time.time()))
            self.assertEqual(c["hc"], "t1")  # kept: the agent may not be found next time while t1 still runs it
            # The machine slept: alex's heartbeat is late, and the next run finds no alex, though t1 still runs it.
            d.sessions["s-alex"]["last_seen"] = time.time() - 3600
            d.starting.pop("/x/api", None)
            with d.lock:
                self.assertIs(d.fire_schedule(c, time.time()), c)
            d.start_for_schedule(c)
        self.assertEqual(len(runs), 1, c["note"])
        self.assertEqual(c["note"], "alex is still starting (hc attach t1)")
        d.inboxes.pop("agent:alex", None)
        d.inboxes.pop("s-alex", None)

    def test_a_terminal_whose_agent_came_up_or_in_another_folder_doesnt_hold_a_start_back(self):
        for why in ("its agent came up", "the schedule now points at another folder"):
            with self.subTest(why):
                c = {"id": "c3", "folder": "/x/api", "agent": "", "requester": {"cwd": "/x/lead"}}
                live, runs = {}, []
                with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                        unittest.mock.patch.object(d.subprocess, "run", self.schedule_hc(live, runs, screen="> ")), \
                        unittest.mock.patch.object(d, "save_schedules", lambda: None), unittest.mock.patch.object(d, "schedule_told", lambda c, t: None):
                    d.start_for_schedule(c)
                    if why == "its agent came up":
                        # It registered from t1 (and goes by another name now, so the schedule didn't find it).
                        d.sessions["s-t1"] = {"id": "s-t1", "name": "api-2", "cwd": "/x/api", "terminal": "hc:t1@10", "last_seen": time.time()}
                    else:
                        c["folder"] = "/x/web"
                    try:
                        d.start_for_schedule(c)
                    finally:
                        d.sessions.pop("s-t1", None)
                self.assertEqual(len(runs), 2, c["note"])
                self.assertEqual(c["hc"], "t2")

    @unittest.skipIf(os.name == "nt", "the folders looked in differ on Windows")
    def test_herdr_core_is_found_by_its_installed_name_before_any_other_hc(self):
        local = os.path.join(os.path.expanduser("~/.local/bin"), "herdr-core")
        cases = (("both on PATH", {"herdr-core": "/u/bin/herdr-core", "hc": "/usr/bin/hc"}, set(), "/u/bin/herdr-core"),
                 ("only hc", {"hc": "/usr/bin/hc"}, set(), "/usr/bin/hc"),
                 ("none", {}, set(), ""),
                 # Installed after the daemon started, so not on its PATH: found in the installer's folder.
                 ("installed since", {}, {local}, local),
                 # Another program's hc on PATH doesn't win over herdr core in the installer's folder.
                 ("another hc on PATH", {"hc": "/usr/bin/hc"}, {local}, local))
        env = {k: v for k, v in os.environ.items() if k != "HC_INSTALL_DIR"}
        for why, on_path, files, want in cases:
            with self.subTest(why), unittest.mock.patch.object(d.shutil, "which", lambda n: on_path.get(n)), \
                    unittest.mock.patch.object(d.os.path, "isfile", lambda p: p in files), \
                    unittest.mock.patch.object(d.os, "access", lambda p, m: p in files), unittest.mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(d.find_hc(), want)

    def test_the_attach_command_is_the_one_you_can_type(self):
        links = {"/u/.local/bin/hc": "/u/.local/bin/herdr-core"}  # the installer's hc, a link to herdr-core
        for why, on_path, hc, want in (("hc is herdr core (a link to it)", "/u/.local/bin/hc", "/u/.local/bin/herdr-core", "hc"),
                                       ("hc is another program", "/usr/bin/hc", "/u/.local/bin/herdr-core", "herdr-core"),
                                       ("no hc", None, "/u/.local/bin/herdr-core", "herdr-core"),
                                       ("only an hc", "/opt/bin/hc", "/opt/bin/hc", "hc")):
            with self.subTest(why), unittest.mock.patch.object(d.shutil, "which", lambda n: on_path if n == "hc" else None), \
                    unittest.mock.patch.object(d.os.path, "realpath", lambda p: links.get(p, p)):
                self.assertEqual(d.hc_cmd(hc), want)

if __name__ == "__main__":
    unittest.main()

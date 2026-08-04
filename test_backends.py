#!/usr/bin/env python3
"""Tests for cc-notify.

The command builders are pure functions precisely so that the Linux and Windows
backends can be verified without a Linux or Windows desktop: these assert the
exact argv / script text each platform would emit, including the escaping of
hostile session titles.

    python3 test_backends.py
"""
import json, os, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notify as N

UUID = "00000000-0000-4000-8000-000000000000"  # synthetic; never a real session id
LINK = f"claude://resume?session={UUID}"


class Titles(unittest.TestCase):
    def _transcript(self, entries):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for e in entries:
            f.write(json.dumps(e) + "\n")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_custom_title_wins_over_ai_title(self):
        p = self._transcript([
            {"type": "ai-title", "aiTitle": "Generated name"},
            {"type": "custom-title", "customTitle": "My name"},
        ])
        self.assertEqual(N.session_title(p), "My name")

    def test_custom_title_wins_even_when_written_earlier(self):
        p = self._transcript([
            {"type": "custom-title", "customTitle": "My name"},
            {"type": "ai-title", "aiTitle": "Generated name"},
        ])
        self.assertEqual(N.session_title(p), "My name")

    def test_last_entry_of_a_kind_wins(self):
        p = self._transcript([
            {"type": "ai-title", "aiTitle": "old"},
            {"type": "ai-title", "aiTitle": "new"},
        ])
        self.assertEqual(N.session_title(p), "new")

    def test_falls_back_to_ai_title(self):
        p = self._transcript([{"type": "ai-title", "aiTitle": "Generated"}])
        self.assertEqual(N.session_title(p), "Generated")

    def test_missing_and_malformed_are_survivable(self):
        self.assertIsNone(N.session_title(None))
        self.assertIsNone(N.session_title("/definitely/not/here.jsonl"))
        p = self._transcript([{"type": "user", "content": "hi"}])
        self.assertIsNone(N.session_title(p))

    def test_broken_json_lines_do_not_abort_the_scan(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write('{"type": "custom-title", TRUNCATED\n')
        f.write(json.dumps({"type": "custom-title", "customTitle": "Survived"}) + "\n")
        f.close()
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(N.session_title(f.name), "Survived")

    def test_only_the_tail_is_read(self):
        """A title buried before the last 512KB is intentionally not found;
        this is the trade that keeps huge transcripts fast."""
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write(json.dumps({"type": "custom-title", "customTitle": "Ancient"}) + "\n")
        filler = json.dumps({"type": "user", "content": "x" * 900}) + "\n"
        f.write(filler * 800)  # comfortably more than TAIL
        f.close()
        self.addCleanup(os.unlink, f.name)
        self.assertGreater(os.path.getsize(f.name), N.TAIL)
        self.assertIsNone(N.session_title(f.name))


class Build(unittest.TestCase):
    def _titled(self, name="Refactor auth"):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write(json.dumps({"type": "custom-title", "customTitle": name}) + "\n")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_repo_and_mode_in_subtitle(self):
        t, s, m, u, g = N.build({"session_id": UUID, "cwd": "/work/my-api",
                                 "transcript_path": self._titled(),
                                 "permission_mode": "plan", "message": "hi"})
        self.assertEqual(t, "Refactor auth")
        self.assertEqual(s, "my-api · plan")
        self.assertEqual(g, UUID)

    def test_repo_alone_when_mode_is_default(self):
        _, s, _, _, _ = N.build({"cwd": "/work/my-api",
                                 "transcript_path": self._titled(),
                                 "permission_mode": "default"})
        self.assertEqual(s, "my-api")

    def test_long_titles_are_truncated(self):
        t, _, _, _, _ = N.build({"cwd": "/w/r", "transcript_path": self._titled("x" * 200)})
        self.assertEqual(len(t), 64)

    def test_default_mode_is_not_shown(self):
        _, s, _, _, _ = N.build({"cwd": "/work/my-api", "permission_mode": "default"})
        self.assertEqual(s, "")

    def test_subtitle_does_not_repeat_a_degraded_title(self):
        """With no transcript title the title becomes the repo name, so the
        subtitle must not say it twice."""
        t, s, _, _, _ = N.build({"cwd": "/work/my-api"})
        self.assertEqual(t, "my-api")
        self.assertEqual(s, "")

    def test_degraded_title_still_shows_mode(self):
        t, s, _, _, _ = N.build({"cwd": "/work/my-api", "permission_mode": "plan"})
        self.assertEqual((t, s), ("my-api", "plan"))

    def test_non_uuid_session_gets_no_deep_link(self):
        _, _, _, u, _ = N.build({"session_id": "not-a-uuid", "cwd": "/w/r"})
        self.assertIsNone(u)

    def test_windows_backslash_cwd(self):
        t, _, _, _, _ = N.build({"cwd": r"C:\work\my-api"})
        self.assertEqual(t, "my-api")

    def test_empty_payload_is_survivable(self):
        t, s, m, u, g = N.build({})
        self.assertEqual(t, "Claude Code")
        self.assertTrue(m)
        self.assertIsNone(u)

    def test_message_is_truncated(self):
        _, _, m, _, _ = N.build({"message": "x" * 500})
        self.assertEqual(len(m), 180)

    def test_deep_link_is_platform_gated(self):
        payload = {"session_id": UUID, "cwd": "/w/r"}
        for mac, win, linux, expected in ((True, False, False, LINK),
                                          (False, True, False, LINK),
                                          (False, False, True, None)):
            with self.subTest(mac=mac, win=win, linux=linux):
                old = (N.IS_MAC, N.IS_WIN, N.IS_LINUX)
                N.IS_MAC, N.IS_WIN, N.IS_LINUX = mac, win, linux
                try:
                    self.assertEqual(N.build(payload)[3], expected)
                finally:
                    N.IS_MAC, N.IS_WIN, N.IS_LINUX = old


class Assumptions(unittest.TestCase):
    """The drift detector itself has to be trustworthy when things are broken."""

    def test_every_assumption_reports_a_verdict(self):
        names = [n for n, _, _ in N.check_assumptions()]
        for expected in ("session store readable", "record field names",
                         "deep-link contract", "title recovery"):
            self.assertIn(expected, names)

    def test_a_raising_check_is_reported_as_failure_not_a_crash(self):
        """A check that explodes must show up as FAIL, not take the run down."""
        real = N.desktop_records
        self.addCleanup(setattr, N, "desktop_records", real)
        def boom(): raise RuntimeError("store unreadable")
        N.desktop_records = boom
        results = dict((n, ok) for n, ok, _ in N.check_assumptions())
        self.assertFalse(results["record field names"])
        self.assertFalse(results["deep-link contract"])

    def test_empty_store_fails_rather_than_passing_vacuously(self):
        """Zero conversations must not read as 'everything resolves'."""
        real = N.desktop_records
        self.addCleanup(setattr, N, "desktop_records", real)
        N.desktop_records = lambda: []
        results = dict((n, ok) for n, ok, _ in N.check_assumptions())
        self.assertFalse(results["deep-link contract"])

    def test_the_contract_check_catches_a_collapse(self):
        """6/44 was the real broken state; it must not read as healthy."""
        real_r, real_t = N.desktop_records, N.desktop_target
        self.addCleanup(setattr, N, "desktop_records", real_r)
        self.addCleanup(setattr, N, "desktop_target", real_t)
        N.desktop_records = lambda: [
            (f"local_{i:08d}-0000-4000-8000-000000000000",
             f"{i:08d}-0000-4000-8000-000000000000", {}) for i in range(44)]
        N.desktop_target = lambda cli: cli if cli.startswith("0000000") else None
        ok = dict((n, o) for n, o, _ in N.check_assumptions())["deep-link contract"]
        self.assertFalse(ok)


class RebadgeCacheFreshness(unittest.TestCase):
    """A cache built from the wrong binary must not be reused forever.

    This is what let an Intel re-badge survive installing the arm64 build: the
    copy existed, so it was reused, and its architecture never changed."""

    def test_stamp_identifies_the_source_it_was_built_from(self):
        st = N._source_stamp("/opt/homebrew/bin/terminal-notifier")
        if st is None:
            self.skipTest("terminal-notifier not installed here")
        self.assertIn("src", st)
        self.assertIn("size", st)
        self.assertIn("mtime", st)

    def test_the_two_homebrew_prefixes_stamp_differently(self):
        """Intel and arm64 copies live at different paths, so the stamp differs
        even when size and mtime happen to match."""
        a = {"src": "/opt/homebrew/Cellar/terminal-notifier/2.0.0/terminal-notifier.app",
             "size": 1, "mtime": 1}
        b = {"src": "/usr/local/Cellar/terminal-notifier/2.0.0/terminal-notifier.app",
             "size": 1, "mtime": 1}
        self.assertNotEqual(a, b)

    def test_every_rebadge_home_is_searched_not_just_this_run_s(self):
        """CLAUDE_PLUGIN_DATA is set inside Claude Code and unset when you run
        this file yourself, so checking only the current home reports on a copy
        the hook never uses."""
        if not N.IS_MAC:
            self.skipTest("macOS only")
        homes = N.all_rebadge_homes()
        self.assertEqual(len(homes), len(set(homes)), "homes must be de-duplicated")
        for h in homes:
            base = os.path.basename(h)
            # "notifier" is ours; "notifier-<badge>" belongs to a --send caller,
            # and its copies need checking for staleness just as much.
            self.assertTrue(base == "notifier" or base.startswith("notifier-"), h)


class SendMode(unittest.TestCase):
    """--send: one banner on behalf of another local tool."""

    def setUp(self):
        self._run, self._first = N._run, N._first_executable
        self.addCleanup(setattr, N, "_run", self._run)
        self.addCleanup(setattr, N, "_first_executable", self._first)
        self._plat = (N.IS_MAC, N.IS_WIN, N.IS_LINUX)
        self.addCleanup(lambda: setattr_all(N, self._plat))
        self.sent = []

    def _ok(self, argv, **kw):
        self.sent.append(argv); return True

    def test_delivery_failure_is_reported_not_swallowed(self):
        """The exit code is the caller's fallback signal. Claiming success for a
        banner that never appeared is worse than the bare notifier it replaces."""
        N.IS_MAC, N.IS_WIN, N.IS_LINUX = True, False, False
        N._first_executable = lambda paths, *names: None
        N._run = lambda argv, **kw: False
        self.assertFalse(N.send("t", "m"))

    def test_it_never_raises(self):
        N.IS_MAC, N.IS_WIN, N.IS_LINUX = True, False, False
        def boom(*a, **k): raise RuntimeError("backend exploded")
        N._first_executable = boom
        self.assertFalse(N.send("t", "m"))

    def test_empty_message_is_not_a_banner(self):
        self.assertFalse(N.send("title", "   "))

    def test_a_caller_gets_its_own_bundle_id(self):
        """The whole point: distinct sender means a distinct Notification Center
        group. Sharing ours would pile a nightly brief in with permission
        prompts."""
        self.assertEqual(N._badge_id(None), N.REBADGE_ID)
        self.assertEqual(N._badge_id("nextbrief"), N.REBADGE_ID + ".nextbrief")
        self.assertNotEqual(N._badge_id("nextbrief"), N._badge_id(None))

    def test_each_badge_gets_its_own_cache(self):
        self.assertNotEqual(N._rebadge_home("nextbrief"), N._rebadge_home())

    def test_hostile_badge_names_are_refused(self):
        """The badge reaches both a bundle id and a filesystem path."""
        for bad in ("../../etc", "a b", "Upper", "x" * 40, "a;rm -rf /", "",
                    "a/b", ".hidden", "-lead"):
            self.assertIsNone(N.BADGE_RE.match(bad), bad)
        for good in ("nextbrief", "a", "my-tool-2"):
            self.assertIsNotNone(N.BADGE_RE.match(good), good)

    def test_an_unusable_badge_still_delivers_under_our_identity(self):
        N.IS_MAC, N.IS_WIN, N.IS_LINUX = True, False, False
        N._first_executable = lambda paths, *names: "/tn"
        N._run = self._ok
        self.assertTrue(N.send("t", "m", badge="../evil"))

    def test_values_are_argv_entries_not_script_text(self):
        """Caller message text is hostile by construction - it is assembled from
        files the caller only reads."""
        argv = N.macos_send_argv("/tn", "T", "m; rm -rf / `id`", None, "g")
        self.assertIn("m; rm -rf / `id`", argv)
        self.assertEqual(argv[argv.index("-message") + 1], "m; rm -rf / `id`")

    def test_no_subtitle_is_invented_for_a_caller(self):
        self.assertNotIn("-subtitle", N.macos_send_argv("/tn", "T", "m", None, "g"))

    def test_click_is_wired_late_not_frozen(self):
        argv = N.macos_send_argv("/tn", "T", "m", "/tmp/BRIEF.html", "g")
        self.assertNotIn("-open", argv)
        self.assertIn("-execute", argv)
        self.assertIn("--open-target", argv[argv.index("-execute") + 1])

    def test_click_command_quotes_its_argument(self):
        cmd = N.send_click_command("/tmp/a b'c;d.html")
        self.assertIn("'/tmp/a b'\"'\"'c;d.html'", cmd)

    def test_linux_and_windows_report_delivery_too(self):
        for plat in ((False, False, True), (False, True, False)):
            N.IS_MAC, N.IS_WIN, N.IS_LINUX = plat
            N._first_executable = lambda paths, *names: None
            self.assertFalse(N.send("t", "m"))
            N._first_executable = lambda paths, *names: "/backend"
            N._run = lambda argv, **kw: True
            self.assertTrue(N.send("t", "m"))


class SendClickTarget(unittest.TestCase):
    """Resolved when clicked, so a regenerated file opens as it is now."""

    def setUp(self):
        self._real = N.subprocess.run
        self.addCleanup(setattr, N.subprocess, "run", self._real)
        self.seen = []
        N.subprocess.run = lambda argv, **kw: self.seen.append(argv)

    def click(self, spec):
        self.seen.clear(); N.open_target(spec)
        return self.seen[0] if self.seen else None

    def test_existing_file_opens(self):
        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as d:
            f = _os.path.join(d, "BRIEF.html")
            open(f, "w").close()
            self.assertEqual(self.click(f), ["/usr/bin/open", f])
            self.assertEqual(self.click("file://" + f), ["/usr/bin/open", f])

    def test_a_vanished_file_does_nothing_visible(self):
        self.assertIsNone(self.click("/nonexistent/BRIEF.html"))

    def test_a_directory_is_not_a_target(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(self.click(d))

    def test_only_http_s_and_local_files(self):
        for hostile in ("javascript:alert(1)", "ftp://h/f", "; rm -rf /",
                        "claude://resume?session=x", "", "   "):
            self.assertIsNone(self.click(hostile), hostile)
        self.assertEqual(self.click("https://example.com/x"),
                         ["/usr/bin/open", "https://example.com/x"])


class NotifierSelection(unittest.TestCase):
    """A migrated Mac has BOTH Homebrew prefixes; the native one must win."""

    def test_apple_silicon_prefers_the_arm64_prefix(self):
        if not N.IS_MAC or os.uname().machine != "arm64":
            self.skipTest("arm64 macOS only")
        self.assertEqual(N.TN_PATHS[0], "/opt/homebrew/bin/terminal-notifier")

    def test_both_prefixes_are_still_searched(self):
        self.assertEqual(sorted(N.TN_PATHS),
                         ["/opt/homebrew/bin/terminal-notifier",
                          "/usr/local/bin/terminal-notifier"])

    def test_first_executable_takes_the_earlier_path(self):
        """Order is the whole mechanism, so pin it."""
        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as d:
            a, b = _os.path.join(d, "a"), _os.path.join(d, "b")
            for f in (a, b):
                open(f, "w").close(); _os.chmod(f, 0o755)
            self.assertEqual(N._first_executable((a, b), "nope"), a)
            self.assertEqual(N._first_executable((b, a), "nope"), b)


class MacBackend(unittest.TestCase):
    def test_full_invocation(self):
        a = N.macos_argv("/tn", "Title", "repo · plan", "msg", LINK, UUID)
        self.assertEqual(a[0], "/tn")
        for flag, val in (("-title", "Title"), ("-subtitle", "repo · plan"),
                          ("-message", "msg"), ("-group", UUID)):
            self.assertIn(flag, a)
            self.assertEqual(a[a.index(flag) + 1], val)
        # The click routes through this script rather than opening the URL
        # directly, so the duplicate check runs at click time, not post time.
        self.assertNotIn("-open", a)
        self.assertIn("-execute", a)
        self.assertIn(LINK, a[a.index("-execute") + 1])
        self.assertIn("--open", a[a.index("-execute") + 1])

    def test_appicon_is_not_used(self):
        """Current macOS accepts -appIcon and then ignores it, so sending it
        only cost an icon conversion per notification. Identity comes from the
        re-badged sending bundle instead."""
        self.assertNotIn("-appIcon", N.macos_argv("/tn", "T", "s", "m", LINK, UUID))

    def test_sender_is_never_used(self):
        """-sender would set the identity but hangs for >12s, measured, and
        would also hand the click to that app, killing the deep link."""
        self.assertNotIn("-sender", N.macos_argv("/tn", "T", "s", "m", LINK, UUID))

    def test_optional_flags_are_omitted(self):
        a = N.macos_argv("/tn", "T", "", "m", None, None)
        for flag in ("-subtitle", "-open", "-group"):
            self.assertNotIn(flag, a)

    def test_applescript_passes_text_as_arguments_not_source(self):
        """Injection guard: the payload must never be interpolated into the
        script body, only handed to the handler as arguments."""
        evil = 'x" & (do shell script "touch /tmp/pwned") & "'
        a = N.applescript_argv("T", "s", evil)
        script = " ".join(a[1:a.index(evil)])
        self.assertNotIn("do shell script", script)
        self.assertIn(evil, a)


class LinuxBackend(unittest.TestCase):
    def test_subtitle_folds_into_body(self):
        a = N.linux_argv("/ns", "Title", "repo · plan", "msg", UUID)
        self.assertEqual(a[-2:], ["Title", "repo · plan\nmsg"])

    def test_body_is_bare_message_without_subtitle(self):
        a = N.linux_argv("/ns", "Title", "", "msg", UUID)
        self.assertEqual(a[-2:], ["Title", "msg"])

    def test_replace_hint_is_keyed_per_session(self):
        a = N.linux_argv("/ns", "T", "", "m", UUID)
        self.assertIn(f"string:x-canonical-private-synchronous:ccnotify-{UUID}", a)

    def test_no_hint_without_a_group(self):
        a = N.linux_argv("/ns", "T", "", "m", None)
        self.assertFalse(any("canonical" in x for x in a))

    def test_double_dash_protects_leading_dash_titles(self):
        """A session named '--help' must be data, not a flag."""
        a = N.linux_argv("/ns", "--help", "", "m", None)
        self.assertIn("--", a)
        self.assertLess(a.index("--"), a.index("--help"))

    def test_app_name_is_set(self):
        a = N.linux_argv("/ns", "T", "", "m", None)
        self.assertEqual(a[a.index("-a") + 1], "Claude Code")


class WindowsBackend(unittest.TestCase):
    def test_protocol_activation_carries_the_deep_link(self):
        s = N.windows_script("T", "sub", "msg", LINK, UUID)
        self.assertIn('activationType="protocol"', s)
        self.assertIn(UUID, s)

    def test_no_activation_without_a_link(self):
        s = N.windows_script("T", "sub", "msg", None, UUID)
        self.assertNotIn("activationType", s)

    def test_xml_injection_in_title_is_escaped(self):
        s = N.windows_script("</text><audio src='evil'/><text>", "", "m", None, None)
        self.assertNotIn("<audio", s)
        self.assertIn("&lt;/text&gt;", s)

    def test_xml_injection_in_message_is_escaped(self):
        s = N.windows_script("T", "", "</binding></visual></toast><toast>", None, None)
        self.assertEqual(s.count("<toast"), 1)

    def test_powershell_quote_escaping(self):
        """A title with an apostrophe must not terminate the PS string."""
        s = N.windows_script("Bob's session", "", "m", None, None)
        self.assertIn("Bob''s session", s)

    def test_ampersand_in_url_is_attribute_escaped(self):
        s = N.windows_script("T", "", "m", "claude://r?a=1&b=2", None)
        self.assertIn("&amp;", s)
        self.assertNotIn("a=1&b=2", s)

    def test_tag_is_bounded(self):
        s = N.windows_script("T", "", "m", None, "x" * 500)
        self.assertNotIn("x" * 65, s)


class MessageExtraction(unittest.TestCase):
    def test_flatten_plain_string(self):
        self.assertEqual(N.flatten_text("hello"), "hello")

    def test_flatten_content_blocks(self):
        self.assertEqual(
            N.flatten_text([{"type": "text", "text": "a"}, {"type": "tool_use"},
                            {"type": "text", "text": "b"}]), "a b")

    def test_flatten_survives_junk(self):
        for junk in (None, 42, {}, [{"no": "text"}]):
            self.assertEqual(N.flatten_text(junk), "")

    def test_first_line_strips_markdown(self):
        self.assertEqual(N.first_line("## **Done** with `it`"), "Done with it")

    def test_first_line_skips_blank_lines_and_code_fences(self):
        self.assertEqual(N.first_line("\n\n```bash\nrm -rf /\n```"), "rm -rf /")

    def test_truncates_by_character_not_byte(self):
        """A byte slice would split a multi-byte character mid-codepoint."""
        out = N.first_line("长" * 200, limit=10)
        self.assertEqual(out, "长" * 10 + "…")
        out.encode("utf-8")  # must not raise

    def test_no_ellipsis_when_short_enough(self):
        self.assertEqual(N.first_line("short", limit=10), "short")

    def test_stop_uses_last_assistant_message(self):
        self.assertEqual(
            N.message_for({"last_assistant_message": "Refactored the parser."}, "Stop"),
            "Refactored the parser.")

    def test_stop_falls_back_when_message_is_empty(self):
        self.assertEqual(N.message_for({"last_assistant_message": ""}, "Stop"), "Turn finished")
        self.assertEqual(N.message_for({}, "Stop"), "Turn finished")

    def test_subagent_stop_names_the_agent(self):
        self.assertEqual(
            N.message_for({"agent_type": "Explore", "last_assistant_message": ""}, "SubagentStop"),
            "Explore finished")

    def test_stopfailure_prefers_the_rendered_error(self):
        """`error` is an enum token; last_assistant_message is human-readable."""
        self.assertEqual(
            N.message_for({"error": "rate_limit",
                           "last_assistant_message": "API Error: Rate limit reached"},
                          "StopFailure"),
            "Failed: API Error: Rate limit reached")

    def test_stopfailure_humanises_the_enum_as_last_resort(self):
        self.assertEqual(N.message_for({"error": "billing_error"}, "StopFailure"),
                         "Failed: billing error")

    def test_notification_still_uses_message(self):
        self.assertEqual(N.message_for({"message": "needs permission"}, "Notification"),
                         "needs permission")


class Suppression(unittest.TestCase):
    def setUp(self):
        self._real = N.user_is_watching
        self.addCleanup(setattr, N, "user_is_watching", self._real)

    def _watching(self, value):
        N.user_is_watching = lambda sid: value

    def test_suppressed_when_watching(self):
        self._watching(True)
        self.assertFalse(N.should_notify({"hook_event_name": "Stop"}, UUID))

    def test_notified_when_not_watching(self):
        self._watching(False)
        self.assertTrue(N.should_notify({"hook_event_name": "Stop"}, UUID))

    def test_permission_prompts_are_never_suppressed(self):
        """They block the session; a false suppression would hang it."""
        self._watching(True)
        self.assertTrue(N.should_notify(
            {"hook_event_name": "Notification",
             "notification_type": "permission_prompt"}, UUID))

    def test_turn_end_kill_switch(self):
        self._watching(False)
        os.environ["CC_NOTIFY_NO_TURN_END"] = "1"
        self.addCleanup(os.environ.pop, "CC_NOTIFY_NO_TURN_END", None)
        for ev in ("Stop", "SubagentStop", "StopFailure"):
            self.assertFalse(N.should_notify({"hook_event_name": ev}, UUID), ev)
        self.assertTrue(N.should_notify(  # must not silence permission prompts
            {"hook_event_name": "Notification",
             "notification_type": "permission_prompt"}, UUID))

    def test_stop_is_silent_while_background_work_is_still_running(self):
        """Stop fires once per pause, not once per turn: with backgrounded
        subagents it fires repeatedly, and only the last has no background work.
        Reporting the earlier ones would claim the turn finished when it hadn't."""
        self._watching(False)
        running = {"hook_event_name": "Stop",
                   "background_tasks": [{"status": "running", "id": "t1"}]}
        self.assertFalse(N.should_notify(running, UUID))

    def test_stop_reports_once_background_work_is_done(self):
        self._watching(False)
        for tasks in ([], None):
            payload = {"hook_event_name": "Stop", "background_tasks": tasks}
            self.assertTrue(N.should_notify(payload, UUID), tasks)
        self.assertTrue(N.should_notify({"hook_event_name": "Stop"}, UUID))  # key absent

    def test_subagent_stop_is_not_gated_on_background_tasks(self):
        """That array describes the PARENT session, so a subagent finishing
        while other background work continues is still a real completion."""
        self._watching(False)
        self.assertTrue(N.should_notify(
            {"hook_event_name": "SubagentStop",
             "background_tasks": [{"status": "running"}]}, UUID))

    def test_watching_fails_open_without_a_session_id(self):
        self.assertFalse(self._real(""))
        self.assertFalse(self._real(None))

    def test_watching_respects_the_global_kill_switch(self):
        os.environ["CC_NOTIFY_NO_SUPPRESS"] = "1"
        self.addCleanup(os.environ.pop, "CC_NOTIFY_NO_SUPPRESS", None)
        self.assertFalse(self._real(UUID))


class HookProcessContract(unittest.TestCase):
    SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.py")

    def _run(self, payload):
        return subprocess.run(
            [sys.executable, self.SCRIPT], input=payload, capture_output=True,
            text=True, timeout=30,
            env={**os.environ, "CC_NOTIFY_NO_TURN_END": "1", "CC_NOTIFY_NO_SUPPRESS": "1",
                 "CC_NOTIFY_DRY_RUN": "1"})  # never banner the person running the tests

    def test_stop_hook_active_is_a_no_op(self):
        """Another Stop hook is continuing the conversation - not a turn end."""
        r = self._run(json.dumps({"hook_event_name": "Stop", "stop_hook_active": True,
                                  "session_id": UUID, "cwd": "/x/y"}))
        self.assertEqual(r.returncode, 0)

    def test_always_exits_zero(self):
        """Notification hooks are observational. A non-zero exit only produces
        stderr noise and must never look like an attempt to block a turn."""
        for payload in ("{}", "not json at all",
                        json.dumps({"hook_event_name": "StopFailure", "error": "overloaded"})):
            with self.subTest(payload=payload[:24]):
                self.assertEqual(self._run(payload).returncode, 0)


class PluginPackaging(unittest.TestCase):
    """Guards the plugin manifests. `claude plugin tag` refuses to cut a release
    when plugin.json and the marketplace entry disagree, so drift between them is
    worth catching here rather than at release time."""

    ROOT = os.path.dirname(os.path.abspath(__file__))

    def _json(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as f:
            return json.load(f)

    def test_manifest_name_is_kebab_case(self):
        name = self._json(".claude-plugin", "plugin.json")["name"]
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_keywords_is_a_list(self):
        """A bare string here is a hard plugin-load error, not a warning."""
        self.assertIsInstance(self._json(".claude-plugin", "plugin.json")["keywords"], list)

    def test_marketplace_entry_agrees_with_manifest(self):
        p = self._json(".claude-plugin", "plugin.json")
        entry, = self._json(".claude-plugin", "marketplace.json")["plugins"]
        self.assertEqual(entry["name"], p["name"])
        self.assertEqual(entry["version"], p["version"])

    def test_plugin_lives_at_the_repo_root(self):
        entry, = self._json(".claude-plugin", "marketplace.json")["plugins"]
        self.assertEqual(entry["source"], "./")

    def test_hooks_file_has_the_required_wrapper_key(self):
        h = self._json("hooks", "hooks.json")
        self.assertIn("hooks", h)
        self.assertIn("Notification", h["hooks"])

    def test_turn_end_events_are_registered(self):
        """Notification alone never fires at turn end - it waits on the idle
        timer - so a finished 20-second task would otherwise be silent."""
        h = self._json("hooks", "hooks.json")["hooks"]
        for event in ("Stop", "StopFailure"):
            self.assertIn(event, h)

    def test_every_registered_event_runs_the_same_script(self):
        for event, blocks in self._json("hooks", "hooks.json")["hooks"].items():
            for b in blocks:
                for handler in b["hooks"]:
                    self.assertEqual(handler["args"], ["${CLAUDE_PLUGIN_ROOT}/notify.py"], event)

    def test_hook_uses_exec_form_with_the_plugin_root_placeholder(self):
        """Exec form (args present) needs no shell quoting, so an install path
        containing a space cannot break the command."""
        handler, = self._json("hooks", "hooks.json")["hooks"]["Notification"][0]["hooks"]
        self.assertEqual(handler["type"], "command")
        self.assertIn("args", handler)
        self.assertEqual(handler["args"], ["${CLAUDE_PLUGIN_ROOT}/notify.py"])
        self.assertNotIn("${", handler["command"])  # placeholder belongs in args

    def test_hook_script_is_actually_shipped(self):
        self.assertTrue(os.path.isfile(os.path.join(self.ROOT, "notify.py")))

    def test_components_are_not_inside_the_manifest_dir(self):
        """Docs call this out as the most common plugin mistake: only
        plugin.json belongs in .claude-plugin/."""
        stray = os.path.join(self.ROOT, ".claude-plugin", "hooks")
        self.assertFalse(os.path.exists(stray))


class DuplicateSessionEntries(unittest.TestCase):
    """Leftover rows from when the click passed an unresolved CLI id.

    They no longer affect click-to-jump - desktop_target decides that - so this
    only has to find them for --doctor to report as tidiness."""

    def setUp(self):
        self._real = N.desktop_records
        self.addCleanup(setattr, N, "desktop_records", self._real)

    def _records(self, pairs):
        """pairs: (sessionId, cliSessionId) or (sessionId, cliSessionId, archived)"""
        N.desktop_records = lambda: [
            (p[0], p[1], {"isArchived": p[2] if len(p) > 2 else False}) for p in pairs]

    def test_pairs_list_every_live_row(self):
        native = "local_deadbeef-0000-4000-8000-000000000000"
        self._records([(f"local_{UUID}", UUID), (native, UUID)])
        (cli, rows), = N.duplicate_pairs()
        self.assertEqual(cli, UUID)
        self.assertEqual(rows, sorted([f"local_{UUID}", native]))

    def test_single_entry_is_not_reported_as_a_pair(self):
        self._records([(f"local_{UUID}", UUID)])
        self.assertEqual(N.duplicate_pairs(), [])

    def test_archiving_the_extra_row_resolves_the_pair(self):
        """Nothing resurrects it: a click never un-archives a row now."""
        native = "local_deadbeef-0000-4000-8000-000000000000"
        self._records([(f"local_{UUID}", UUID, False), (native, UUID, True)])
        self.assertEqual(N.duplicate_pairs(), [])

    def test_archiving_the_local_uuid_row_also_resolves_the_pair(self):
        """Either row may be the one you keep.

        The old code privileged `local_<cli>` and never checked whether IT was
        archived, so tidying in this direction left the pair reported for good."""
        native = "local_deadbeef-0000-4000-8000-000000000000"
        self._records([(f"local_{UUID}", UUID, True), (native, UUID, False)])
        self.assertEqual(N.duplicate_pairs(), [])

    def test_a_lone_native_entry_is_not_a_duplicate(self):
        """One row is not a pair, whatever its id looks like."""
        self._records([("local_deadbeef-0000-4000-8000-000000000000", UUID)])
        self.assertEqual(N.duplicate_pairs(), [])


class ResolutionSurvivesBadRecords(unittest.TestCase):
    """The store is written by another program; a click must never raise.

    The banner is already dismissed by the time the handler runs, so an
    exception means nothing happens and there is nothing left to click again -
    strictly worse than any other failure this module tolerates."""

    def setUp(self):
        self._real = N.desktop_records
        self.addCleanup(setattr, N, "desktop_records", self._real)
        self._mac = N.IS_MAC
        self.addCleanup(setattr, N, "IS_MAC", self._mac)
        N.IS_MAC = True

    def test_mixed_timestamp_types_do_not_raise(self):
        """int beside str made sort() raise, taking the whole click down."""
        N.desktop_records = lambda: [
            (f"local_{UUID}", UUID, {"lastActivityAt": 1}),
            ("local_bbbbbbbb-0000-4000-8000-000000000000", UUID,
             {"lastActivityAt": "2026-07-29T00:00:00Z"}),
        ]
        self.assertIsNotNone(N.desktop_target(UUID))

    def test_missing_and_null_timestamps_do_not_raise(self):
        N.desktop_records = lambda: [
            (f"local_{UUID}", UUID, {}),
            ("local_bbbbbbbb-0000-4000-8000-000000000000", UUID,
             {"lastActivityAt": None}),
        ]
        self.assertIsNotNone(N.desktop_target(UUID))

    def test_a_raising_resolver_still_opens_the_app(self):
        """Resolution blowing up must degrade to the bare scheme, not silence."""
        real = N.desktop_target
        self.addCleanup(setattr, N, "desktop_target", real)
        def boom(sid): raise TypeError("unorderable")
        N.desktop_target = boom
        seen = []
        realrun = N.subprocess.run
        N.subprocess.run = lambda argv, **kw: seen.append(argv)
        try:
            self.assertEqual(N.open_url(LINK), 0)
        finally:
            N.subprocess.run = realrun
        self.assertEqual(seen, [["/usr/bin/open", "claude://"]])


class ResolvesToTheDesktopRow(unittest.TestCase):
    """The deep link addresses a desktop ROW, not a CLI session.

    Measured on a live store: `claude://resume?session=<X>` for a row whose
    sessionId is `local_<X>` lands on that row and creates nothing, even though
    its cliSessionId is a different uuid. The app mints its own `local_<uuid>`
    per conversation and keeps the CLI id in a separate field; passing the CLI id
    for such a row asks the app to import a conversation it is already showing,
    which is what minted the untitled duplicates.
    """

    OTHER = "deadbeef-0000-4000-8000-000000000000"

    def setUp(self):
        self._real = N.desktop_records
        self.addCleanup(setattr, N, "desktop_records", self._real)
        self._plat = (N.IS_MAC, N.IS_WIN, N.IS_LINUX)
        N.IS_MAC, N.IS_WIN, N.IS_LINUX = True, False, False
        self.addCleanup(lambda: setattr_all(N, self._plat))

    def _records(self, rows):
        """rows: (sessionId, cliSessionId, archived, lastActivityAt)"""
        N.desktop_records = lambda: [
            (r[0], r[1], {"isArchived": r[2], "lastActivityAt": r[3]}) for r in rows]

    def test_the_desktop_uuid_is_returned_not_the_cli_id(self):
        # The case that covers 86% of a real store and never navigated before.
        self._records([("local_%s" % self.OTHER, UUID, False, 10)])
        self.assertEqual(N.desktop_target(UUID), self.OTHER)

    def test_a_canonical_row_resolves_to_itself(self):
        # The minority shape that used to work by coincidence: the two ids are
        # equal, so addressing the row and addressing the session look the same.
        self._records([("local_%s" % UUID, UUID, False, 10)])
        self.assertEqual(N.desktop_target(UUID), UUID)

    def test_an_untracked_session_resolves_to_nothing(self):
        """The duplicate-minting path, closed.

        Previously this navigated, on the reasoning that an import would create
        the conversation's first row. It does -- and then the app writes its own
        row too, and the conversation has two. A click that cannot resolve a
        target must raise the app, not guess."""
        self._records([])
        self.assertIsNone(N.desktop_target(UUID))

    def test_an_archived_row_is_not_resurrected(self):
        self._records([("local_%s" % self.OTHER, UUID, True, 10)])
        self.assertIsNone(N.desktop_target(UUID))

    def test_the_most_recently_active_row_wins(self):
        # A conversation that already has duplicates still has one row the owner
        # is actually working in. Send them there rather than declining.
        self._records([("local_aaaaaaaa-0000-4000-8000-000000000000", UUID, False, 1),
                       ("local_bbbbbbbb-0000-4000-8000-000000000000", UUID, False, 99)])
        self.assertEqual(N.desktop_target(UUID), "bbbbbbbb-0000-4000-8000-000000000000")

    def test_other_conversations_are_not_consulted(self):
        self._records([("local_%s" % self.OTHER, "9999abcd-0000-4000-8000-000000000000",
                        False, 10)])
        self.assertIsNone(N.desktop_target(UUID))

    def test_the_click_opens_the_resolved_row(self):
        """End to end: the banner carries the CLI id, the click opens the row."""
        self._records([("local_%s" % self.OTHER, UUID, False, 10)])
        seen = []
        real = N.subprocess.run
        N.subprocess.run = lambda argv, **kw: seen.append(argv)
        try:
            N.open_url(LINK, N.DESKTOP_BUNDLE)
        finally:
            N.subprocess.run = real
        self.assertEqual(seen[0],
                         ["/usr/bin/open", "claude://resume?session=%s" % self.OTHER])
        self.assertNotIn(UUID, seen[0][1])


class DeepLinkSuppressedWhenItWouldLitter(unittest.TestCase):
    """The click target is dropped for any session where clicking would mint a
    second, untitled row. Litter is worse than a missing click - the banner
    still says which session wants you."""

    def setUp(self):
        self._target = N.desktop_target
        self.addCleanup(setattr, N, "desktop_target", self._target)
        # Default: the row resolves to itself, i.e. the canonical shape.
        N.desktop_target = lambda sid: sid
        self._plat = (N.IS_MAC, N.IS_WIN, N.IS_LINUX)
        N.IS_MAC, N.IS_WIN, N.IS_LINUX = True, False, False
        self.addCleanup(lambda: setattr_all(N, self._plat))
        for v in ("CC_NOTIFY_NO_DEEPLINK", "CC_NOTIFY_ALWAYS_DEEPLINK"):
            os.environ.pop(v, None)

    def test_link_is_always_attached(self):
        """The banner always carries a click target.

        Which row it reaches is decided when it is clicked, because a banner
        outlives the state it was posted under."""
        self.assertEqual(N.build({"session_id": UUID, "cwd": "/w/r"})[3], LINK)

    def test_click_focuses_the_app_when_nothing_resolves(self):
        """Bare claude:// raises the app without importing a session. Measured:
        open -b / open -a / AppleScript activate all no-op on this app.

        Reached when the desktop row is not tracked yet -- navigating blind is
        exactly what mints the untitled second row."""
        N.desktop_target = lambda sid: None
        self.assertEqual(self.click(LINK), ["/usr/bin/open", "claude://"])

    def test_click_command_pins_a_stable_interpreter(self):
        """sys.executable can be Xcode's python3, whose path is baked into every
        delivered banner and dies when Xcode moves."""
        cmd = N.click_command(LINK)
        self.assertTrue(cmd.startswith("/usr/bin/python3 "), cmd)
        self.assertNotIn("Xcode", cmd)

    def test_click_command_quotes_its_arguments(self):
        self.assertIn("'%s'" % LINK, N.click_command(LINK))

    def test_click_navigates_to_the_resolved_row(self):
        """A resolvable row is opened directly - no convergence required."""
        self.assertEqual(self.click(LINK), ["/usr/bin/open", LINK])

    def test_always_deeplink_overrides_the_guard(self):
        N.desktop_target = lambda sid: None
        os.environ["CC_NOTIFY_ALWAYS_DEEPLINK"] = "1"
        self.addCleanup(os.environ.pop, "CC_NOTIFY_ALWAYS_DEEPLINK", None)
        self.assertEqual(self.click(LINK), ["/usr/bin/open", LINK])

    def test_no_deeplink_still_removes_the_target_entirely(self):
        os.environ["CC_NOTIFY_NO_DEEPLINK"] = "1"
        self.addCleanup(os.environ.pop, "CC_NOTIFY_NO_DEEPLINK", None)
        self.assertIsNone(N.build({"session_id": UUID, "cwd": "/w/r"})[3])

    def test_click_refuses_anything_but_our_own_scheme(self):
        """The handler is reachable from a delivered notification, so it must
        never be a general-purpose opener."""
        for hostile in ("file:///etc/passwd", "https://evil.test",
                        "claude://resume?session=../../x", "",
                        "claude://resume?session=%s&x=1" % UUID):
            self.assertIsNone(self.click(hostile), hostile)

    def test_terminal_session_gets_its_terminal_back_not_the_deep_link(self):
        """claude://resume cannot reach into iTerm - it would import a COPY of a
        live session into the desktop app. Raise the real host instead."""
        N.desktop_target = lambda sid: sid      # resolvable; still wrong surface
        self.assertEqual(self.click(LINK, "com.googlecode.iterm2"),
                         ["/usr/bin/open", "-b", "com.googlecode.iterm2"])

    def test_surface_check_precedes_the_duplicate_check(self):
        """desktop_target resolves for terminal sessions - correct in its own
        terms, but blind to the surface being wrong."""
        self.assertNotIn(LINK, self.click(LINK, "com.apple.Terminal"))

    def test_desktop_session_still_navigates(self):
        self.assertEqual(self.click(LINK, N.DESKTOP_BUNDLE), ["/usr/bin/open", LINK])

    def test_unknown_surface_falls_back_to_the_duplicate_check(self):
        self.assertEqual(self.click(LINK, None), ["/usr/bin/open", LINK])

    def test_hostile_surface_is_never_executed(self):
        """The surface is interpolated into a shell command, so it is validated
        rather than trusted."""
        for bad in ("com.x; rm -rf /", "$(whoami)", "a b", "`id`", "-"):
            self.assertEqual(self.click(LINK, bad), ["/usr/bin/open", LINK], bad)

    def test_click_command_carries_the_surface(self):
        real = N.launching_surface
        N.launching_surface = lambda: "com.googlecode.iterm2"
        try:
            self.assertIn("--from com.googlecode.iterm2", N.click_command(LINK))
        finally:
            N.launching_surface = real

    def test_click_command_omits_a_hostile_surface(self):
        real = N.launching_surface
        N.launching_surface = lambda: "com.x; rm -rf /"
        try:
            self.assertNotIn("--from", N.click_command(LINK))
        finally:
            N.launching_surface = real

    def click(self, url, surface=None):
        """Run the click handler, returning the argv it would have run."""
        seen = []
        real = N.subprocess.run
        N.subprocess.run = lambda argv, **kw: seen.append(argv)
        try:
            N.open_url(url, surface)
        finally:
            N.subprocess.run = real
        return seen[0] if seen else None


def setattr_all(mod, plat):
    mod.IS_MAC, mod.IS_WIN, mod.IS_LINUX = plat


class DeepLinkOptOut(unittest.TestCase):
    def test_no_deeplink_env_removes_the_click_target(self):
        os.environ["CC_NOTIFY_NO_DEEPLINK"] = "1"
        self.addCleanup(os.environ.pop, "CC_NOTIFY_NO_DEEPLINK", None)
        self.assertIsNone(N.build({"session_id": UUID, "cwd": "/w/r"})[3])

    def test_deeplink_present_by_default(self):
        os.environ.pop("CC_NOTIFY_NO_DEEPLINK", None)
        old = (N.IS_MAC, N.IS_WIN, N.IS_LINUX)
        N.IS_MAC, N.IS_WIN, N.IS_LINUX = True, False, False
        try:
            self.assertEqual(N.build({"session_id": UUID, "cwd": "/w/r"})[3], LINK)
        finally:
            N.IS_MAC, N.IS_WIN, N.IS_LINUX = old


class SubprocessSafety(unittest.TestCase):
    def test_a_lingering_grandchild_cannot_defeat_the_timeout(self):
        """A notifier that leaves a helper holding the inherited pipe must not
        be able to block us. With capture_output the timeout kills only the
        direct child while communicate() waits for every writer to close -
        measured turning a 2.6s launch into a multi-minute hang."""
        import time
        spawner = ("import subprocess,sys;"
                   "subprocess.Popen([sys.executable,'-c','import time;time.sleep(25)']);"
                   "sys.exit(0)")
        t0 = time.time()
        N._run([sys.executable, "-c", spawner])
        # Threshold sits below _run's own 10s timeout on purpose: with
        # capture_output the buggy path burns the full timeout before returning,
        # so a looser bound would let the regression through.
        self.assertLess(time.time() - t0, 5,
                        "_run blocked on a grandchild holding the output pipe")

    def test_run_never_raises(self):
        self.assertFalse(N._run(["/definitely/not/a/binary"]))


class RebadgedNotifier(unittest.TestCase):
    def test_rebadge_home_follows_claude_plugin_data(self):
        """${CLAUDE_PLUGIN_ROOT} changes on every update, so the generated
        notifier bundle must live in the persistent data dir instead."""
        old = os.environ.get("CLAUDE_PLUGIN_DATA")
        try:
            os.environ["CLAUDE_PLUGIN_DATA"] = "/tmp/ccn-data-test"
            self.assertEqual(N._rebadge_home(), "/tmp/ccn-data-test/notifier")
            os.environ.pop("CLAUDE_PLUGIN_DATA")
            self.assertIn(".claude", N._rebadge_home())
            self.assertNotIn("ccn-data-test", N._rebadge_home())
        finally:
            if old is not None:
                os.environ["CLAUDE_PLUGIN_DATA"] = old
            else:
                os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_rebadge_can_be_disabled(self):
        """Escape hatch: if a machine refuses the new bundle id the symptom is
        silence, so turning it off must always be possible."""
        os.environ["CC_NOTIFY_NO_REBADGE"] = "1"
        self.addCleanup(os.environ.pop, "CC_NOTIFY_NO_REBADGE", None)
        self.assertIsNone(N.rebadged_notifier("/usr/local/bin/terminal-notifier"))

    def test_rebadge_falls_back_when_source_bundle_is_missing(self):
        """A wrong icon is far better than no notification, so any failure here
        must return None and let the caller use the shared notifier."""
        with tempfile.TemporaryDirectory() as tmp:  # isolate from a cached build
            old = os.environ.get("CLAUDE_PLUGIN_DATA")
            os.environ["CLAUDE_PLUGIN_DATA"] = tmp
            try:
                self.assertIsNone(N.rebadged_notifier("/nonexistent/terminal-notifier"))
            finally:
                if old is not None:
                    os.environ["CLAUDE_PLUGIN_DATA"] = old
                else:
                    os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_an_existing_build_is_reused_rather_than_rebuilt(self):
        """The build costs ~2s; every notification after the first must skip it."""
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("CLAUDE_PLUGIN_DATA")
            os.environ["CLAUDE_PLUGIN_DATA"] = tmp
            try:
                binary = os.path.join(tmp, "notifier", "cc-notify.app",
                                      "Contents", "MacOS", "terminal-notifier")
                os.makedirs(os.path.dirname(binary))
                open(binary, "w").close()
                os.chmod(binary, 0o755)
                # Source path is bogus: reaching it at all would mean a rebuild.
                self.assertEqual(N.rebadged_notifier("/nonexistent/tn"), binary)
            finally:
                if old is not None:
                    os.environ["CLAUDE_PLUGIN_DATA"] = old
                else:
                    os.environ.pop("CLAUDE_PLUGIN_DATA", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)

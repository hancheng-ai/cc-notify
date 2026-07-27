#!/usr/bin/env python3
"""Tests for claude-code-notify.

The command builders are pure functions precisely so that the Linux and Windows
backends can be verified without a Linux or Windows desktop: these assert the
exact argv / script text each platform would emit, including the escaping of
hostile session titles.

    python3 test_backends.py
"""
import json, os, sys, tempfile, unittest

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


class MacBackend(unittest.TestCase):
    def test_full_invocation(self):
        a = N.macos_argv("/tn", "Title", "repo · plan", "msg", LINK, UUID, "file:///i.png")
        self.assertEqual(a[0], "/tn")
        for flag, val in (("-title", "Title"), ("-subtitle", "repo · plan"),
                          ("-message", "msg"), ("-open", LINK),
                          ("-group", UUID), ("-appIcon", "file:///i.png")):
            self.assertIn(flag, a)
            self.assertEqual(a[a.index(flag) + 1], val)

    def test_optional_flags_are_omitted(self):
        a = N.macos_argv("/tn", "T", "", "m", None, None, None)
        for flag in ("-subtitle", "-open", "-group", "-appIcon"):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

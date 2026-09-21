"""Unit tests for the pure tmux protocol helpers (recorded tmux output)."""

import unittest

from pairshell import tmuxops as T


class NormalizeTests(unittest.TestCase):
    def test_normalize_strips_path_and_login_dash(self):
        self.assertEqual(T.normalize_command("/usr/bin/-tcsh"), "tcsh")
        self.assertEqual(T.normalize_command("-bash"), "bash")
        self.assertEqual(T.normalize_command("  zsh "), "zsh")
        self.assertEqual(T.normalize_command(""), "")

    def test_is_shell(self):
        for name in ("bash", "-tcsh", "/bin/zsh", "fish", "sh", "dash", "ksh"):
            self.assertTrue(T.is_shell(name), name)
        for name in ("vim", "python3", "ssh", "less", "sleep", "", "bashful"):
            self.assertFalse(T.is_shell(name), name)

    def test_shell_family(self):
        self.assertEqual(T.shell_family("tcsh"), "csh")
        self.assertEqual(T.shell_family("/bin/csh"), "csh")
        self.assertEqual(T.shell_family("fish"), "fish")
        self.assertEqual(T.shell_family("bash"), "sh")
        self.assertEqual(T.shell_family("zsh"), "sh")


class SentinelTests(unittest.TestCase):
    def test_sentinel_uses_status_for_csh_and_fish(self):
        self.assertEqual(T.sentinel_text("csh", "abcd1234"), 'echo __DONE_"$status"_abcd1234__')
        self.assertEqual(T.sentinel_text("fish", "abcd1234"), 'echo __DONE_"$status"_abcd1234__')
        self.assertEqual(T.sentinel_text("sh", "abcd1234"), 'echo __DONE_"$?"_abcd1234__')

    def test_build_command_line_separator(self):
        self.assertEqual(T.build_command_line("pwd", "sh", "n0nce123"), 'pwd ; echo __DONE_"$?"_n0nce123__')
        self.assertEqual(T.build_command_line("sleep 5 &  ", "sh", "n0nce123"), 'sleep 5 & echo __DONE_"$?"_n0nce123__')
        self.assertEqual(T.build_command_line("ls >& /dev/null", "csh", "n"), 'ls >& /dev/null ; echo __DONE_"$status"_n__')

    def test_done_pattern_ignores_the_echoed_command(self):
        pat = T.done_pattern("abcd1234")
        self.assertIsNone(pat.search('user@host$ pwd ; echo __DONE_"$?"_abcd1234__'))
        self.assertIsNone(pat.search('echo __DONE_"$status"_abcd1234__'))
        m = pat.search("__DONE_17_abcd1234__")
        self.assertEqual(m.group(1), "17")
        self.assertIsNone(pat.search("__DONE_17_ffffffff__"))

    def test_find_done_tolerates_wrapping(self):
        lines = ["x" * 190 + "__DONE_", "0_abcd1234__", "prompt$ "]
        self.assertEqual(T.find_done(lines, "abcd1234"), 0)
        self.assertIsNone(T.find_done(['pwd ; echo __DONE_"$?"_abcd1234__', "/tmp"], "abcd1234"))
        self.assertEqual(T.find_done(["abc__DONE_3_abcd1234__"], "abcd1234"), 3)

    def test_b64_shell_arg_round_trip(self):
        import base64, re

        text = "echo \"it's\" $HOME `x` ; rm -rf --no ; \\ \t ☃"
        arg = T.b64_shell_arg(text)
        m = re.match(r'^"\$\(printf %s \'([A-Za-z0-9+/=]+)\' \| base64 -d\)"$', arg)
        self.assertIsNotNone(arg and m)
        self.assertEqual(base64.b64decode(m.group(1)).decode(), text)

    def test_literal_for_send_keys_escapes_trailing_semicolon(self):
        self.assertEqual(T.literal_for_send_keys("echo hi;"), "echo hi\;")
        self.assertEqual(T.literal_for_send_keys("a;b"), "a;b")
        self.assertEqual(T.literal_for_send_keys(";"), "\;")


class ValidationTests(unittest.TestCase):
    def test_key_names(self):
        for ok in ("C-c", "Enter", "M-x", "^C", "F1", "q", "Up", "BSpace", "C-M-a", "_"):
            self.assertEqual(T.validate_key_name(ok), ok)
        for bad in (":", "a b", "", ":wq", "a;b", "é", "/"):
            with self.assertRaises(ValueError):
                T.validate_key_name(bad)

    def test_literal_rejects_newlines(self):
        self.assertEqual(T.validate_literal(":wq"), ":wq")
        with self.assertRaises(ValueError):
            T.validate_literal("a\nb")
        with self.assertRaises(ValueError):
            T.validate_literal("a\r")

    def test_exec_command_one_line(self):
        self.assertEqual(T.validate_exec_command("ls -la"), "ls -la")
        with self.assertRaises(ValueError):
            T.validate_exec_command("ls\npwd")
        with self.assertRaises(ValueError):
            T.validate_exec_command("   ")
        with self.assertRaises(ValueError):
            T.validate_exec_command("ls\t-la")
        with self.assertRaises(ValueError):
            T.validate_literal("a\tb")

    def test_custom_prompt_regex(self):
        import re

        st = T.parse_pane_state(RECORDED)
        st.lines[3] = "alice ~/src \u276f                                   12:34"  # zsh RPROMPT
        self.assertIsNotNone(T.idle_reason(st))
        custom = T.compile_prompt_regex(r"\u276f\s+\d\d:\d\d$")
        self.assertIsNone(T.idle_reason(st, custom))
        self.assertIs(T.compile_prompt_regex(""), T.PROMPT_RE)
        with self.assertRaises(ValueError):
            T.compile_prompt_regex("(")

    def test_session_names(self):
        self.assertEqual(T.validate_session_name("lab-1_x"), "lab-1_x")
        for bad in ("a.b", "a:b", "", "-x", "a b"):
            with self.assertRaises(ValueError):
                T.validate_session_name(bad)


RECORDED = (
    "1234 3 1 200 50 0 0 0 bash\n"
    + T.DISPLAY_SEPARATOR
    + "\nalice@example-host:~$ ls\nfile1  file2\n\nalice@example-host:~$ \n\n\n"
)


class PaneStateTests(unittest.TestCase):
    def test_parse_pane_state(self):
        st = T.parse_pane_state(RECORDED)
        self.assertEqual((st.history_size, st.cursor_y, st.attached, st.width, st.height), (1234, 3, 1, 200, 50))
        self.assertEqual(st.foreground, "bash")
        self.assertEqual(st.lines[0], "alice@example-host:~$ ls")
        self.assertEqual(st.cursor_line, "alice@example-host:~$")
        self.assertEqual(st.start_line, 1237)

    def test_parse_pane_state_with_path_in_command(self):
        st = T.parse_pane_state("0 0 0 80 24 1 2 1 /usr/bin/-tcsh\n" + T.DISPLAY_SEPARATOR + "\n% \n")
        self.assertEqual(st.foreground, "/usr/bin/-tcsh")
        self.assertEqual(st.in_mode, 1)
        self.assertEqual((st.window_index, st.pane_index), (1, 2))

    def test_idle_detection(self):
        st = T.parse_pane_state(RECORDED)
        self.assertIsNone(T.idle_reason(st))
        st.foreground = "vim"
        self.assertIn("vim", T.idle_reason(st))
        st.foreground = "-tcsh"
        st.lines[3] = "alice@example-host:~$ git st"  # user mid-typing
        self.assertIn("typing", T.idle_reason(st))
        st.lines[3] = "example-host% "
        self.assertIsNone(T.idle_reason(st))
        st.lines[3] = "> "
        self.assertIsNone(T.idle_reason(st))
        st.lines[3] = "Password: "
        self.assertIsNotNone(T.idle_reason(st))
        st.lines[3] = "alice@example-host:~$"
        st.in_mode = 1
        self.assertIn("copy", T.idle_reason(st))
        st.in_mode = 0
        st.cursor_y = 40  # beyond captured lines -> empty cursor line
        self.assertIsNotNone(T.idle_reason(st))

    def test_prompt_regex(self):
        for p in ("$ ", "# ", "% ", "> ", "user@h:~$", "[u@h ~]$   ", "h:~ u%", "~/src \u276f ", "\u279c  proj git:(main) \u279c"):
            self.assertTrue(T.PROMPT_RE.search(p), p)
        for p in ("", "Password:", "running...", "$x"):
            self.assertFalse(T.PROMPT_RE.search(p), p)


class CaptureMathTests(unittest.TestCase):
    def test_compute_capture_start(self):
        self.assertEqual(T.compute_capture_start(1237, 1240), -3)
        self.assertEqual(T.compute_capture_start(1237, 1237), 0)
        self.assertEqual(T.compute_capture_start(15, 10), 5)
        # history was trimmed by history-limit: clamp to the oldest line
        self.assertEqual(T.compute_capture_start(0, 10), -10)
        self.assertEqual(T.compute_capture_start(100, 50100), -50000)


class ExtractTests(unittest.TestCase):
    N = "abcd1234"

    def test_basic(self):
        cap = ['alice@h:~$ pwd ; echo __DONE_"$?"_abcd1234__', "/home/alice", "__DONE_0_abcd1234__", "alice@h:~$ "]
        ext = T.extract_output(cap, self.N)
        self.assertEqual((ext.lines, ext.rc, ext.found_echo, ext.found_done), (["/home/alice"], 0, True, True))

    def test_no_trailing_newline(self):
        cap = ['alice@h:~$ printf abc ; echo __DONE_"$?"_abcd1234__', "abc__DONE_0_abcd1234__", "alice@h:~$ "]
        ext = T.extract_output(cap, self.N)
        self.assertEqual((ext.lines, ext.rc), (["abc"], 0))

    def test_nonzero_and_empty_output(self):
        cap = ['$ false ; echo __DONE_"$?"_abcd1234__', "__DONE_1_abcd1234__", "$ "]
        ext = T.extract_output(cap, self.N)
        self.assertEqual((ext.lines, ext.rc), ([], 1))

    def test_multiline_keeps_blank_lines_inside(self):
        cap = ['$ cat f ; echo __DONE_"$?"_abcd1234__', "a", "", "b   ", "__DONE_0_abcd1234__", "$ "]
        ext = T.extract_output(cap, self.N)
        self.assertEqual(ext.lines, ["a", "", "b"])

    def test_missing_echo_line(self):
        cap = ["$ ", "out", "__DONE_2_abcd1234__", "$ "]
        ext = T.extract_output(cap, self.N)
        self.assertEqual((ext.lines, ext.rc, ext.found_echo), (["out"], 2, False))

    def test_history_wiped_sentinel_first(self):
        ext = T.extract_output(["__DONE_0_abcd1234__", "$ "], self.N)
        self.assertEqual((ext.lines, ext.rc), ([], 0))

    def test_nonce_inside_output_is_not_the_echo_line(self):
        cap = [
            '$ cat ~/.pairshell/s.log ; echo __DONE_"$?"_abcd1234__',
            "old stuff",
            '$ cat ~/.pairshell/s.log ; echo __DONE_"$?"_abcd1234__',
            "__DONE_0_abcd1234__",
            "$ ",
        ]
        ext = T.extract_output(cap, self.N)
        self.assertEqual(ext.rc, 0)
        self.assertEqual(ext.lines, ["old stuff", '$ cat ~/.pairshell/s.log ; echo __DONE_"$?"_abcd1234__'])

    def test_clamped_window_keeps_first_line(self):
        cap = ["950", "951", "__DONE_0_abcd1234__", "$ "]
        ext = T.extract_output(cap, self.N, clamped=True)
        self.assertEqual((ext.lines, ext.rc, ext.found_echo), (["950", "951"], 0, False))

    def test_partial_when_not_done(self):
        cap = ['$ sleep 30 ; echo __DONE_"$?"_abcd1234__', "working", "", "", ""]
        ext = T.extract_output(cap, self.N)
        self.assertEqual((ext.lines, ext.rc, ext.found_done), (["working"], None, False))

    def test_truncate(self):
        lines = [str(i) for i in range(1200)]
        kept, omitted = T.truncate_lines(lines, 500)
        self.assertEqual((len(kept), omitted, kept[0], kept[-1]), (500, 700, "700", "1199"))
        self.assertEqual(T.truncate_lines(lines, 0), (lines, 0))
        self.assertEqual(T.truncate_lines(["a"], 5), (["a"], 0))

    def test_screen_tail(self):
        self.assertEqual(T.screen_tail(["a", "b", "c", "", ""], 2), ["b", "c"])


class KeysArgvTests(unittest.TestCase):
    def test_parse_key_items(self):
        from pairshell.cli import parse_key_items

        items, target = parse_key_items(["--literal", ":wq", "Enter"])
        self.assertEqual((items, target), ([("literal", ":wq"), ("key", "Enter")], None))
        items, target = parse_key_items(["--", "q", "Enter", "--to", "lab1"])
        self.assertEqual((items, target), ([("key", "q"), ("key", "Enter")], "lab1"))
        items, _ = parse_key_items(["--literal=echo hi;", "Enter"])
        self.assertEqual(items[0], ("literal", "echo hi;"))
        with self.assertRaises(ValueError):
            parse_key_items([])
        with self.assertRaises(ValueError):
            parse_key_items([":wq"])
        with self.assertRaises(ValueError):
            parse_key_items(["--literal"])

    def test_split_keys_argv(self):
        from pairshell.cli import _split_keys_argv

        self.assertEqual(_split_keys_argv(["keys", "--literal", "x", "Enter"]), ["keys", "--", "--literal", "x", "Enter"])
        self.assertEqual(_split_keys_argv(["keys", "--to", "p", "C-c"]), ["keys", "--to", "p", "--", "C-c"])
        self.assertEqual(_split_keys_argv(["exec", "ls"]), ["exec", "ls"])


if __name__ == "__main__":
    unittest.main()

"""Exercise actual terminal I/O, including shared stdin/stdout file flags.

Run after building: python3 scripts/tui_latency.py target/release/utop
Uses only the Python standard library; runs on Linux and macOS.
"""

import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
import unittest


BINARY = sys.argv.pop(1) if len(sys.argv) > 1 else "target/release/utop"


class TerminalSession:
    def __init__(self, rows=40, cols=160, nonblocking=False):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        if nonblocking:
            fcntl.fcntl(self.slave, fcntl.F_SETFL, os.O_NONBLOCK)
        self.flags = fcntl.fcntl(self.slave, fcntl.F_GETFL)
        self.attributes = termios.tcgetattr(self.slave)
        self.started = time.monotonic()
        # Deliberately share the open file description, just as a shell can.
        self.process = subprocess.Popen(
            [BINARY], stdin=self.slave, stdout=self.slave, stderr=self.slave
        )

    def expect(self, token, timeout=5):
        data = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], max(0, deadline - time.monotonic()))[0]:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                data += chunk
                if token in data:
                    return data
        raise AssertionError(f"terminal did not render {token!r}; received {len(data)} bytes")

    def key_latency(self, keys, token):
        start = time.monotonic()
        os.write(self.master, keys)
        self.expect(token)
        return (time.monotonic() - start) * 1000

    def quit(self):
        os.write(self.master, b"\x03")
        self.expect(b"\x1b[?1049l", timeout=2)
        self.process.wait(timeout=2)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
        os.close(self.master)
        os.close(self.slave)


class TerminalTests(unittest.TestCase):
    def session(self, **kwargs):
        terminal = TerminalSession(**kwargs)
        self.addCleanup(terminal.close)
        return terminal

    def test_startup_and_search_latency(self):
        terminal = self.session()
        terminal.expect(b"\x1b[J")  # End of the first painted table.
        startup = (time.monotonic() - terminal.started) * 1000
        query = b"utop_no_matching_process_"
        search = terminal.key_latency(b"/" + query, b"Filter: /" + query + b"_")
        edits = []
        for key in b"123456789":
            query += bytes([key])
            edits.append(terminal.key_latency(bytes([key]), b"Filter: /" + query + b"_"))
        backspace = terminal.key_latency(b"\x7f", b"Filter: /" + query[:-1] + b"_")
        escape = terminal.key_latency(b"\x1b", b"[NORMAL]")
        terminal.quit()
        print(f"\nstartup={startup:.1f}ms search={search:.1f}ms "
              f"worst_edit={max(edits):.1f}ms backspace={backspace:.1f}ms escape={escape:.1f}ms")
        self.assertLess(startup, 500)
        self.assertLess(max([search, backspace, escape] + edits), 250)

    def test_input_does_not_make_output_nonblocking(self):
        terminal = self.session()
        terminal.expect(b"\x1b[J")
        self.assertEqual(fcntl.fcntl(terminal.slave, fcntl.F_GETFL) & os.O_NONBLOCK, 0)

    def test_terminal_state_restored_including_inherited_flags(self):
        for nonblocking in (False, True):
            with self.subTest(nonblocking=nonblocking):
                terminal = self.session(nonblocking=nonblocking)
                terminal.expect(b"\x1b[J")
                self.assertEqual(fcntl.fcntl(terminal.slave, fcntl.F_GETFL) & os.O_NONBLOCK, 0)
                terminal.quit()
                # Darwin adds an internal "was written" flag after output;
                # compare the public status flags that the application owns.
                mask = os.O_NONBLOCK | os.O_APPEND | os.O_ACCMODE
                self.assertEqual(fcntl.fcntl(terminal.slave, fcntl.F_GETFL) & mask,
                                 terminal.flags & mask)
                self.assertEqual(termios.tcgetattr(terminal.slave), terminal.attributes)

    def test_large_frames_survive_output_backpressure(self):
        terminal = self.session(rows=120, cols=300)
        # Let the terminal buffer fill before reading. A nonblocking flush that
        # ignores EAGAIN leaves frame fragments queued behind subsequent frames.
        time.sleep(0.15)
        terminal.expect(b"\x1b[J")
        latency = terminal.key_latency(b"/buffer_probe", b"Filter: /buffer_probe_")
        self.assertLess(latency, 250)
        terminal.quit()


if __name__ == "__main__":
    unittest.main()

"""serve/test_turn_wait_recovery.py - the single turn names its holder, and a holder whose thread is gone is taken
back (the fork's fix in commit 0f6cc63b; no process, no GPU, no downloads).

A request whose client went away used to leave the engine's fifo held until the GC freed its generator: `/status`
then answered busy=false with the requests queued, the engine sat idle, and neither log got a line for minutes
(measured 03.11.2026 on 0.1.55.11: a 146 s wait that ran in 46 ms once the turn came back).

The turn is taken with `Service._turn_take` (the owner thread + a token naming that taking) and given back with
`Service._turn_give`, which frees only the taking it is told about.  A request that has waited `TURN_WAIT_REPORT_S`
(120 s) reports the holder once, with every thread's stack; if the holder's thread is gone the turn is taken back so
the queue moves on.  Untracked holders (an image encode, a session save) leave `turn_owner` None: they are reported,
never released.  The fake engine is `serve.server.MockEngine`.

    python -m unittest serve.test_turn_wait_recovery -v
"""
import contextlib
import io
import json
import tempfile
import http.client
import threading
import unittest
from pathlib import Path
from unittest import mock

import serve.server as server
from serve.frontend import ChatTemplate
from serve.server import ByteTokenizer, MockEngine, Service, serve

TEMPLATE = Path(server.__file__).parent / "chat_template.jinja"


def service(script="ok"):
    """A Service with the fake engine and no process: the turn's bookkeeping is what these tests read."""
    tok = ByteTokenizer()
    return Service(MockEngine(tok, script), tok, ChatTemplate(TEMPLATE))


def body(prompt: str, stream: bool = False):
    return json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": 50,
                       "reasoning_effort": "none", "stream": stream}).encode()


def post(port: int, payload: bytes):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("POST", "/v1/chat/completions", payload, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        out = resp.read().decode(), resp.status
    finally:
        conn.close()
    return out


def holder_that_leaves(svc: Service) -> threading.Thread:
    """A thread that takes the engine's turn and ends without giving it back: the state a request whose client
    went away leaves, with its generator closed only later by the GC.  The turn stays held, `turn_owner` names a
    thread that is no longer alive, and the release is registered on an `ExitStack` the way the request loop does."""
    stack = contextlib.ExitStack()
    t = threading.Thread(target=lambda: stack.callback(svc._turn_give, svc._turn_take()))
    t.start()
    t.join()
    return t


class TurnOwnership(unittest.TestCase):
    def test_the_turn_names_the_thread_that_took_it(self):
        svc = service()
        token = {}

        def take():
            token["token"] = svc._turn_take()
        t = threading.Thread(target=take)
        t.start()
        t.join()
        self.assertEqual(svc.turn_owner, t.ident, "the holder is the thread that took the turn")
        self.assertFalse(svc.turn_thread().is_alive(), "the keeper is the thread itself, not its reused id")
        self.assertTrue(svc.fifo.locked())
        svc._turn_give(token["token"])
        self.assertFalse(svc.fifo.locked(), "the turn goes back to the queue")
        self.assertIsNone(svc.turn_owner)
        self.assertIsNone(svc.turn_token)

    def test_an_old_token_does_not_release_a_later_holder(self):
        """A request that was forced out closes its generator later; it passes its own old token and must leave the
        turn of the request that took it afterwards."""
        svc = service()
        first = svc._turn_take()
        svc._turn_give(first)                       # the normal release: the turn is free
        second = svc._turn_take()                   # the next request takes it
        svc._turn_give(first)                       # the old one must not free this one
        self.assertTrue(svc.fifo.locked(), "a stale token released a turn it never took")
        self.assertEqual(svc.turn_token, second)
        self.assertEqual(svc.turn_owner, threading.get_ident())
        svc._turn_give(second)
        self.assertFalse(svc.fifo.locked())

    def test_a_closed_generator_gives_back_only_its_own_taking(self):
        """The release the request loop registers (`turn.callback(self._turn_give, got)`, server.py:6443) runs when
        the generator is closed: a forced-out request closing late must not free the turn of the request after it."""
        svc = service()
        stack = contextlib.ExitStack()
        mine = svc._turn_take()
        stack.callback(svc._turn_give, mine)        # what the request loop registers
        svc._turn_give(mine)                        # this request ends normally
        other = svc._turn_take()                    # the next request takes the turn
        stack.close()                               # the abandoned generator is closed at last: its callback runs
        self.assertTrue(svc.fifo.locked(), "the late release freed a turn it had already given up")
        self.assertEqual(svc.turn_token, other)
        svc._turn_give(other)


class TurnReport(unittest.TestCase):
    def test_a_holder_whose_thread_is_gone_is_taken_back(self):
        svc = service()
        t = holder_that_leaves(svc)
        self.assertTrue(svc.fifo.locked(), "the turn is still held by a thread that ended")
        with capture() as cap:
            svc.turn_wait_report("dead00000000", 120.0)
        text = cap["log"]
        self.assertIn(f"thread {t.ident}", text)
        self.assertIn("GONE", text, "the holder's thread must be named as gone")
        self.assertIn("taking the turn back so the queue moves", text)
        self.assertFalse(svc.fifo.locked(), "the queue is stopped by a holder that is not there")
        self.assertIsNone(svc.turn_owner)
        # the dead path prints its two lines and returns before the stack dump: a thread that is gone has no stack
        self.assertEqual(cap["stacks"], "")

    def test_the_take_back_leaves_a_live_holder_that_came_in_between(self):
        """The GC can free the abandoned generator between the report's look at the holder and its take-back: the
        turn is given back, the next request takes it, and the report (which named a dead thread) must not free that
        live turn.  The take-back is named by the holder it saw, `owner`, not by "whoever holds it now"."""
        svc = service()
        t = holder_that_leaves(svc)
        old_owner, old_token = svc.turn_owner, svc.turn_token
        svc._turn_give(old_token)                   # the GC closes the generator: the turn is free
        new = svc._turn_take()                      # the next request takes it, in this (live) thread
        svc._turn_give(owner=old_owner)             # the report's take-back, named by the dead holder
        self.assertTrue(svc.fifo.locked(), "the report took a live holder's turn")
        self.assertEqual(svc.turn_token, new)
        self.assertEqual(svc.turn_owner, threading.get_ident())
        svc._turn_give(new)

    def test_an_alive_holder_is_reported_and_left_alone(self):
        """The report is a sign, not a release: a live holder is working, and taking the turn from it would run two
        requests on one engine."""
        svc = service()
        token = svc._turn_take()                    # this thread is the holder, and it is alive
        try:
            with capture() as cap:
                svc.turn_wait_report("alive0000000", 120.0)
            text, stacks = cap["log"], cap["stacks"]
            self.assertIn("has waited 120 s for the engine's turn", text)
            self.assertIn("alive", text)
            self.assertNotIn("GONE", text)
            self.assertTrue(svc.fifo.locked(), "a live holder keeps its turn")
            self.assertEqual(svc.turn_owner, threading.get_ident())
            self.assertIn("current thread 0x", stacks.lower())
        finally:
            svc._turn_give(token)

    def test_an_untracked_holder_is_only_reported(self):
        """An image encode or a session save takes the fifo without a token: `turn_owner` stays None, and the report
        says no generation holds the turn.  Nothing here may release a turn that is not a request's."""
        svc = service()
        svc.fifo.acquire()                          # an untracked holder
        try:
            with capture() as cap:
                svc.turn_wait_report("untracked00000", 120.0)
            text, stacks = cap["log"], cap["stacks"]
            self.assertIn("no generation holds", text)
            self.assertIsNone(svc.turn_owner)
            self.assertTrue(svc.fifo.locked(), "the untracked holder is not given back")
            self.assertIn("current thread 0x", stacks.lower())
        finally:
            svc.fifo.release()


@contextlib.contextmanager
def capture():
    """The report's stdout in memory and its stderr to a REAL file: `faulthandler.dump_traceback` writes through the
    file's descriptor, so a `StringIO` there raises `UnsupportedOperation: fileno`.  Yields a dict that carries
    `log` (the printed lines) and `stacks` (the dumped stacks), filled when the block ends."""
    out = io.StringIO()
    d = tempfile.TemporaryDirectory()
    path = Path(d.name) / "stacks.txt"
    result: dict[str, str] = {}
    stream = open(path, "w", encoding="utf-8")
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(stream):
            yield result
    finally:
        stream.close()
        result["log"] = out.getvalue()
        result["stacks"] = path.read_text(encoding="utf-8") if path.exists() else ""
        d.cleanup()


class QueueMovesOn(unittest.TestCase):
    """The whole path: the turn is held by a thread that is gone, the next request waits, reports it once, and the
    queue moves.  The thresholds are shortened so the test does not wait 120 s."""

    def test_a_request_after_an_abandoned_holder_is_answered(self):
        svc = service("ok")
        httpd = serve(svc, port=0)
        try:
            holder_that_leaves(svc)
            self.assertTrue(svc.fifo.locked())
            with mock.patch.object(server, "TURN_WAIT_REPORT_S", 0.5), \
                 mock.patch.object(server, "QUEUE_BEAT_S", 0.2), capture() as cap:
                reply, status = post(httpd.server_address[1], body("go"))
            self.assertEqual(status, 200, reply)
            self.assertEqual(json.loads(reply)["choices"][0]["message"]["content"], "ok")
            log = cap["log"]
            self.assertIn("has waited", log)
            self.assertIn("GONE", log, "the abandoned holder is named")
            self.assertIn("taking the turn back", log)
            # once: the loop reports one request's long wait a single time (server.py sets `reported`)
            self.assertEqual(log.count("has waited"), 1)
            self.assertFalse(svc.fifo.locked(), "the queue moved on")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_a_request_that_is_not_queued_never_reports(self):
        """Nothing is reported when the turn is free: a report line in the log means a request really waited."""
        svc = service("ok")
        httpd = serve(svc, port=0)
        try:
            with mock.patch.object(server, "TURN_WAIT_REPORT_S", 0.5), capture() as cap:
                reply, status = post(httpd.server_address[1], body("go"))
            self.assertEqual(status, 200, reply)
            self.assertNotIn("has waited", cap["log"])
            self.assertFalse(svc.fifo.locked())
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()

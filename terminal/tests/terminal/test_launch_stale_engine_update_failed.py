"""#5038, in this fork's shape: `strata launch` with an installed engine older than the model wants.

Upstream's case (#5038) was an engine update whose test start fails: the reporter's 0.2.6 counted the old engine as
absent and gave up the whole install. Upstream answers by saying the update did not finish and telling the person to
run `strata update`. This fork never spawns `strata update` from a launch at all — the engine that runs is the package
built in the checkout (`tools/packaging/build_engine_win_local.py`) — so the same situation is answered with the one
line that names the build step, the old engine keeps serving, and the launch goes on (never refuses, never updates).
"""
from __future__ import annotations

import unittest
from unittest import mock

from strata_cli.launch import settings as S
from strata_cli.launch.runner import Runner
from tests.terminal.launch_support import HW_12, Home, catalog

OLD = {"usable": True, "engine": {"version": "0.1.45", "backend": "cuda"}}


def runner(yes: bool) -> tuple[Runner, list]:
    r = Runner(S.LaunchSettings(family="qwen", model="IQ3_XXS", context=32768, vision="off"), catalog=catalog(HW_12))
    r.assume_yes = yes                                       # even the reporter's --yes never starts an update here
    events: list = []
    r.emit = lambda stage, text, pct=None, level="info", **kw: events.append((level, text))
    return r, events


class StaleEngineUpdateFailed(unittest.TestCase):
    def run_engine_step(self, *, yes=True):
        """The stale engine with a model that needs newer: nothing is spawned whatever is answered."""
        r, events = runner(yes)
        from strata_control import engine_component as ec, engine_update
        with mock.patch.object(ec, "status", return_value=OLD), \
                mock.patch.object(engine_update, "running_models", return_value=[]), \
                mock.patch.object(Runner, "_spawn") as spawn, \
                mock.patch.object(Runner, "no_engine") as no_engine:
            r.ensure_engine()                                   # must not raise: the install goes on
        return r, events, spawn, no_engine

    def test_a_stale_engine_is_warned_about_and_the_old_one_keeps_going(self):
        with Home():
            r, events, spawn, no_engine = self.run_engine_step()
        texts = [t for _level, t in events]
        self.assertNotIn("No engine is installed yet. Getting the one for your graphics card…", texts)
        self.assertTrue(any("older than this model needs" in t for t in texts), texts)
        self.assertTrue(any(level == "warn" for level, t in events if "older than this model needs" in t), events)
        spawn.assert_not_called()                              # nothing is downloaded or replaced
        no_engine.assert_not_called()                          # the old engine is not called absent

    def test_the_warning_names_the_build_step_and_never_strata_update(self):
        with Home():
            r, events, _spawn, _ = self.run_engine_step()
        text = next(t for _level, t in events if "older than this model needs" in t)
        self.assertIn("tools/packaging/build_engine_win_local.py", text)
        self.assertNotIn("strata update", text)
        self.assertTrue(any("tools/packaging/build_engine_win_local.py" in n for n in r.notices), r.notices)

    def test_without_yes_the_launch_goes_on_the_same_way(self):
        with Home():
            r, events, spawn, _ = self.run_engine_step(yes=False)
        spawn.assert_not_called()
        self.assertTrue(any(level == "warn" and "older than this model needs" in t for level, t in events), events)


if __name__ == "__main__":
    unittest.main()
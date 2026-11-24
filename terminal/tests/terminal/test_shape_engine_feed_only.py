"""A model-shape engine is fetched from the feed on this PC without the Strata engine (the fork's rule: the Strata engine is the package built in the
checkout, `tools/packaging/build_engine_win_local.py`).

`strata update` on this PC closes the engine step (`updater.engine_skipped`), and that closed the shape step with it, because upstream runs the shape steps
inside the engine step. `STRATA_SHAPE_ENGINE_FROM_FEED=1` opens the shape step alone: an installed shape engine (Inkling-2-Mini, Hy4-Flash) comes to the newest
of the feed beside the older one, `update_engine` is never called, and a running model is not asked to stop. The default keeps the shape step closed exactly
as upstream does, and the Strata engine's files are byte for byte what they were.
"""
from __future__ import annotations

from unittest import mock

from strata_cli import update_cmd
from strata_control import feed as feedmod, shape_engine, signing, updater
from tests.terminal.test_shape_engine import COMP, SHAPE, Base, tree


class ShapeFeedWithoutEngine(Base):
    def installed(self, version: str = "0.1.59") -> None:
        self.publish(version)
        with mock.patch.object(shape_engine, "_test_start", return_value=(True, "x")):
            self.assertIsNotNone(self.ensure())

    def ctx(self):
        return updater.Context(components=self.comp, source=self.url, trusted=signing.load_trusted_keys(),
                               hardware=self.hw, serving=lambda: [], poll=0.05)

    def test_the_shape_step_runs_with_the_engine_step_off(self):
        self.installed()
        self.publish("0.1.59", "0.1.60", sequence=2)
        ctx = self.ctx()
        feed = feedmod.fetch(self.url, ctx.trusted)
        steps = updater._shape_steps(ctx, feed, False, True)
        self.assertEqual([name for name, _ in steps], [COMP])
        with mock.patch.object(shape_engine, "_test_start", return_value=(True, "x")), \
                mock.patch.object(updater, "update_engine", side_effect=AssertionError("the Strata engine must not be fetched")) as engine_step:
            out = updater.run_update(ctx, control=False, engine=False, shape_engine=True)
        row = next(r for r in out["results"] if r.get("component") == COMP)
        self.assertEqual((row["status"], row["installed_before"], row["installed_after"], row["shape"]),
                         ("updated", "0.1.59", "0.1.60", SHAPE))
        self.assertEqual(out["exit_code"], 0)
        self.assertFalse(engine_step.called, "the Strata engine step is off with the shape step on")
        self.assertEqual(self.strata_before, tree(self.comp / "engine"), "the Strata engine's files are what they were")

    def test_the_shape_engine_lands_beside_a_running_model(self):
        # a running model is not asked to stop: the shape engine is a new folder next to the one that is loaded
        self.installed()
        self.publish("0.1.59", "0.1.60", sequence=2)
        ctx = self.ctx()
        ctx.serving = lambda: [{"model": "inkling-2-mini-sq-15", "port": 8080}]
        with mock.patch.object(shape_engine, "_test_start", return_value=(True, "x")):
            out = updater.run_update(ctx, control=False, engine=False, shape_engine=True)
        self.assertEqual(out["exit_code"], 0)
        self.assertEqual(self.folders(), ["0.1.59", "0.1.60"])

    def test_the_default_keeps_the_shape_step_closed_as_upstream(self):
        self.installed()
        self.publish("0.1.59", "0.1.60", sequence=2)
        ctx = self.ctx()
        feed = feedmod.fetch(self.url, ctx.trusted)
        self.assertEqual(updater._shape_steps(ctx, feed, False), [], "the default is upstream's: no shape step with the engine step off")
        with mock.patch.object(shape_engine, "_test_start", side_effect=AssertionError("no shape engine is fetched by default")):
            out = updater.run_update(ctx, control=False, engine=False)
        self.assertEqual(out["exit_code"], 0)
        self.assertEqual(self.folders(), ["0.1.59"], "the installed shape engine stays as it is")
        self.assertEqual(self.strata_before, tree(self.comp / "engine"))

    def test_the_word_and_the_flags(self):
        # (env word, `--engine`, `--no-engine`) -> (the Strata engine step, the shape step), as one run computes them
        table = [
            (False, False, False, False, False),     # plain `strata update` on this PC: nothing is fetched
            (True, False, False, False, True),       # STRATA_SHAPE_ENGINE_FROM_FEED=1: the shape engines alone
            (False, True, False, True, True),        # `--engine` opens both
            (True, True, False, True, True),
            (True, False, True, False, False),       # `--no-engine` closes both
            (False, True, True, False, False),
        ]
        for word, eng, no_engine, want_engine, want_shape in table:
            env = {update_cmd.ENV_SHAPE_ENGINE_FEED: "1"} if word else {}
            self.assertEqual(update_cmd.feed_steps(eng, no_engine, env), (want_engine, want_shape), (word, eng, no_engine))
        for word in ("1", "true", "yes", "on", "ON"):
            self.assertTrue(update_cmd.shape_engine_from_feed({update_cmd.ENV_SHAPE_ENGINE_FEED: word}), word)
        for word in ("", "0", "off", "none"):
            self.assertFalse(update_cmd.shape_engine_from_feed({update_cmd.ENV_SHAPE_ENGINE_FEED: word}), repr(word))
        self.assertFalse(update_cmd.engine_from_feed({}), "the Strata engine stays off by default on this PC")

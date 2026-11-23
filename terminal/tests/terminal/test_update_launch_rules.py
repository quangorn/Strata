"""A launch, a setup and a model download never update an engine by themselves (owner, 11-01; the same shape as #4823: "downloading a model changed my
engine without asking").

This fork goes further than upstream: `strata launch` never spawns `strata update` at all, whatever is answered, because the engine that
runs is the package built in the checkout (`tools/packaging/build_engine_win_local.py`). So:

* Qwen: an engine older than the model needs is warned about, the line names the build step, and the start goes on with the engine there is.
  Nothing is spawned, `--yes` or not.
* No engine installed at all is a warning, the model download goes on, and nothing is spawned.
* The app's model download for Qwen never fetches an engine (control_engine_step), as before.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from strata_cli.launch import settings as S  # noqa: E402
from strata_cli.launch.runner import Runner  # noqa: E402
from strata_control import engine_component as ec  # noqa: E402
from tests.terminal.test_launch_engine_first import qwen_catalog  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OLD = {"usable": True, "engine": {"version": "0.1.45", "backend": "cuda"}, "notice": None}
NEW = {"usable": True, "engine": {"version": "0.1.50", "backend": "cuda"}, "notice": None}
NONE = {"usable": False, "engine": None, "notice": "No engine for this PC was found"}


class QwenStaleEngine(unittest.TestCase):
    def ensure(self, statuses, *, confirm=None, assume_yes=False):
        spawns, seen = [], []
        r = Runner(S.LaunchSettings(family="qwen", model="Q2_0", context=32768), catalog=qwen_catalog(), emit=seen.append,
                   confirm=confirm, assume_yes=assume_yes)
        r.check_cancel = lambda: None
        with mock.patch.object(ec, "status", side_effect=list(statuses)), \
                mock.patch.object(Runner, "needed_engine", return_value=(0, 1, 50)):
            r.ensure_engine()
        return spawns, r, seen

    def test_an_older_engine_is_never_updated_by_the_launch(self):
        for kwargs in ({"assume_yes": True}, {"confirm": lambda *a: True}, {}):
            spawns, r, _ = self.ensure([OLD], **kwargs)
            self.assertEqual(spawns, [], kwargs)
            self.assertTrue(any("older than this model needs" in n for n in r.notices), r.notices)

    def test_the_warning_names_the_build_step_and_not_an_update(self):
        spawns, r, _ = self.ensure([OLD])
        self.assertEqual(spawns, [])
        self.assertTrue(any("tools/packaging/build_engine_win_local.py" in n for n in r.notices), r.notices)
        self.assertFalse(any("strata update" in n for n in r.notices), r.notices)

    def test_no_engine_at_all_is_not_fetched_either(self):
        spawns, r, seen = self.ensure([NONE])
        self.assertEqual(spawns, [])
        self.assertTrue(any("couldn't find an engine" in e.text for e in seen), [e.text for e in seen])

    def test_an_engine_that_is_new_enough_says_nothing(self):
        spawns, r, seen = self.ensure([NEW])
        self.assertEqual((spawns, r.notices), ([], []))
        self.assertEqual(seen[-1].text, "Engine 0.1.50 (cuda) is installed.")

    def test_plan_says_the_start_uses_this_engine(self):
        r = Runner(S.LaunchSettings(family="qwen", model="Q2_0", context=32768), catalog=qwen_catalog(), emit=lambda e: None)
        with mock.patch.object(ec, "status", return_value=OLD), mock.patch.object(Runner, "needed_engine", return_value=(0, 1, 50)), \
                mock.patch("strata_cli.launch.runner.install_isolation_words", return_value=[]):
            words = r.preflight_words()
        self.assertTrue(any("Starting uses this engine as it is" in w for w in words), words)
        self.assertFalse(any("will ask before updating it" in w for w in words), words)


class SourceRules(unittest.TestCase):
    def calls(self, path, function):
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == function:
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        f = inner.func
                        names.add(f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else "")
        return names

    def test_the_apps_qwen_model_download_never_fetches_an_engine(self):
        names = self.calls("setup.py", "control_engine_step")
        for forbidden in ("engine_from_feed", "get_prebuilt", "get_prebuilt_hip", "update_installed_engine", "run", "urlopen", "check_call", "Popen"):
            self.assertNotIn(forbidden, names)


if __name__ == "__main__":
    unittest.main()
"""Load a script from a skill folder (the folder names have hyphens), and run CLIs in a temp folder."""

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
PETR4 = os.path.join(FIX, "yahoo", "PETR4-60d5m.json.gz")


def load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_%s_%s" % (folder.replace("-", "_"), name), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(mod, argv):
    """mod.main(argv) -> (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class InTemp(object):
    """Run a test inside an empty folder (the tools write to ./trader)."""

    def setUp(self):
        self._old = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="b3-trader-test-")
        os.chdir(self.tmp)
        os.makedirs("trader", exist_ok=True)
        shutil.copy(os.path.join(ROOT, "trader-budget", "config.example.json"), "trader/config.json")

    def tearDown(self):
        os.chdir(self._old)
        shutil.rmtree(self.tmp, ignore_errors=True)

"""NFR-MNT-06 / AC-P1-06: importing any finbot module performs no I/O and changes no state.

Each check runs in a fresh interpreter with a ``sys.addaudithook`` hook, which observes the
real ``open``, ``socket.*`` and ``subprocess`` events at C level (patching ``builtins.open`` alone
would miss ``io.open_code`` and ``pathlib``).
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import finbot

SRC_DIR = Path(finbot.__file__).resolve().parent

_PROBE = textwrap.dedent(
    """
    import json, os, sys
    allowed_roots = tuple(
        os.path.realpath(p) for p in {sys.prefix, sys.base_prefix, sys.exec_prefix, SRC_DIR}
    )
    events = []

    def hook(event, args):
        if event == "open":
            path = args[0]
            if isinstance(path, int):
                return
            real = os.path.realpath(os.fsdecode(path))
            if not real.startswith(allowed_roots):
                events.append(f"open {real}")
        elif event.startswith(("socket.", "subprocess.", "os.system", "os.exec", "os.spawn")):
            events.append(event)

    env_before = dict(os.environ)
    import logging
    handlers_before = list(logging.getLogger().handlers)
    sys.addaudithook(hook)

    import importlib, pkgutil
    import finbot
    modules = ["finbot"]
    for info in pkgutil.walk_packages(finbot.__path__, prefix="finbot."):
        importlib.import_module(info.name)
        modules.append(info.name)

    print(json.dumps({
        "modules": modules,
        "events": events,
        "env_changed": dict(os.environ) != env_before,
        "handlers_changed": list(logging.getLogger().handlers) != handlers_before,
    }))
    """
)


def _run_probe() -> dict[str, object]:
    code = f"SRC_DIR = {str(SRC_DIR)!r}\n{_PROBE}"
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    result: dict[str, object] = json.loads(proc.stdout.strip().splitlines()[-1])
    return result


def test_importing_every_module_has_no_side_effects() -> None:
    result = _run_probe()
    modules = result["modules"]
    assert isinstance(modules, list)
    for expected in ("finbot.config", "finbot.logging", "finbot.__main__", "finbot.domain"):
        assert expected in modules
    assert result["events"] == []
    assert result["env_changed"] is False
    assert result["handlers_changed"] is False

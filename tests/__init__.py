"""Test package. On Windows, every process a test starts is started without a console window (a window opens
for each child when the parent has no console of its own, e.g. when the tests are run from an editor or an agent)."""
import os
import subprocess

if os.name == "nt" and not getattr(subprocess.Popen, "_quiet_windows", False):
    _real_popen_init = subprocess.Popen.__init__

    def _quiet_popen_init(self, *args, **kwargs):
        if not kwargs.get("creationflags"):          # tests that choose their own flags keep them
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        _real_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _quiet_popen_init
    subprocess.Popen._quiet_windows = True

"""Every command in the README's C++ CLI block runs and exits as documented.

The block between ``<!-- cli:start -->`` and ``<!-- cli:end -->`` lists
``$ <command>  # exit N`` lines. Each is run from the repository root through
bash, with ``blackboxrs`` resolved to the built CLI and ``$WORK`` to a
temporary directory, in order (later lines may use what earlier ones wrote).
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from .conftest import BIN, ROOT, needs_cpp

pytestmark = needs_cpp

LINE = re.compile(r"^\$ (?P<cmd>.+?)\s+# exit (?P<code>\d+)\s*$")


def readme_commands() -> list[tuple[str, int]]:
    text = (ROOT / "README.md").read_text()
    block = text[text.index("<!-- cli:start -->"):text.index("<!-- cli:end -->")]
    out = []
    for line in block.splitlines():
        m = LINE.match(line.strip())
        if m:
            out.append((m["cmd"], int(m["code"])))
    return out


def test_block_is_not_empty():
    assert len(readme_commands()) >= 10


def test_every_readme_command(tmp_path):
    env = {**os.environ, "WORK": str(tmp_path)}
    prelude = f'blackboxrs() {{ "{BIN}" "$@"; }}; '
    for cmd, code in readme_commands():
        out = subprocess.run(["bash", "-c", prelude + cmd], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=600)
        if out.returncode != code:
            pytest.fail(f"`{cmd}` exited {out.returncode}, README says {code}\n"
                        f"{out.stdout[-1500:]}\n{out.stderr[-1500:]}")

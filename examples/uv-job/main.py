"""Minimal compute for a uv-lockfile job: write output/result.txt.

Replace the body with your real computation. Same contract as any compute job:
read inputs from checked-out dependencies, write every result under output/.
"""
import pathlib
import platform

import yaml  # a locked dependency, to show the environment is the one in uv.lock

result = {"answer": 42, "python": platform.python_version(), "pyyaml": yaml.__version__}

out = pathlib.Path("output")
out.mkdir(exist_ok=True)
(out / "result.txt").write_text(yaml.safe_dump(result, sort_keys=False))
print("wrote output/result.txt:", result)

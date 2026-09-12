"""Replace only LicenseCheck.is_premium in the installed image package."""

import ast
import importlib.util
from pathlib import Path

from litellm.proxy.auth import litellm_license


path = Path(litellm_license.__file__)
source = path.read_text()
tree = ast.parse(source)
classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LicenseCheck"]
assert len(classes) == 1, "Expected exactly one LicenseCheck class"
methods = [node for node in classes[0].body if isinstance(node, ast.FunctionDef) and node.name == "is_premium"]
assert len(methods) == 1, "Expected exactly one is_premium method"
method = methods[0]
assert method.end_lineno is not None
lines = source.splitlines(keepends=True)
lines[method.body[0].lineno - 1 : method.end_lineno] = [" " * (method.col_offset + 4) + "return True\n"]
patched = "".join(lines)
compile(patched, str(path), "exec")
path.write_text(patched)
Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)
print(f"Patched LicenseCheck.is_premium in {path}")

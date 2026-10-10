# The libclang bindings ship no type information, and their kinds (CursorKind.CLASS_DECL and the
# rest) are attributes registered at import time, so a checker reading the source sees none of
# them. Declaring the module untyped makes every name in it Any, as mypy treats it.
from typing import Any

def __getattr__(name: str) -> Any: ...

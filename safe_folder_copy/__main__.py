"""Start the window, or copy without one: SafeFolderCopy --copy SOURCE DESTINATION.

The windowless form exists for scripting and for the release build's own
end-to-end check. It prints nothing, because a windowed executable has no
console to print to; the result is the exit code and the report file.
Exit codes: 0 everything copied, 1 some files were not copied, 2 stopped or refused.
Here DESTINATION is the exact folder the copy goes into; the window instead
makes a sub-folder named after the source, the way Explorer's copy-paste does.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--copy"]:
        if len(argv) != 3:
            return 2
        from .core import copy_folder

        try:
            result = copy_folder(argv[1], argv[2])
        except ValueError:
            return 2
        if result.stopped_reason or result.cancelled:
            return 2
        return 0 if result.complete else 1

    from .gui import run_app

    run_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

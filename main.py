"""Convenience wrapper: ``python main.py ...`` equals ``python -m accreditation ...``."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from accreditation.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

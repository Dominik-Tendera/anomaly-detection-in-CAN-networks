"""Repository test suites; imports do not depend on the current directory."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from repo_paths import configure_imports
configure_imports()

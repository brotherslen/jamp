import os
import sys
import tempfile
from pathlib import Path

# Tests read the shipped config only.  Without this they would lay whatever
# the person running them keeps in their own user folder over it, and pass or
# fail by what is in that person's library.
os.environ["JAMP_HOME"] = tempfile.mkdtemp(prefix="jamp-home-")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

import fixtures  # noqa: E402
from jamp.config import load_config  # noqa: E402


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def library(tmp_path_factory):
    """The miniature library, built once for the whole session."""
    root = tmp_path_factory.mktemp("library")
    fixtures.build_library(root)
    return root

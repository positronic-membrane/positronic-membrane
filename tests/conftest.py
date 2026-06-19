import pytest

import src.config
from src.database import init_db


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """
    Global auto-use fixture that isolates DB_PATH for every test execution.
    This guarantees that tests never read/write the production database.
    """
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)

    # Initialize schema for testing
    init_db()

    yield

    src.config.DB_PATH = orig_db_path

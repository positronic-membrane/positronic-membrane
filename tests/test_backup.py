import sqlite3
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from scripts.backup_db import run_backup


@pytest.fixture
def temp_backup_env(tmp_path, monkeypatch):
    """Sets up temporary folders and DB paths for testing."""
    test_db = tmp_path / "test_janus.db"

    # Initialize a test DB with tables
    conn = sqlite3.connect(str(test_db))
    conn.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY, name TEXT);")
    conn.execute("INSERT INTO test_table (name) VALUES ('Test Value');")
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(test_db))

    # Point backup script ROOT_DIR to tmp_path
    monkeypatch.setattr("scripts.backup_db.ROOT_DIR", tmp_path)
    monkeypatch.setattr("scripts.backup_db.DB_PATH", str(test_db))

    # Set up a mock Chroma Vector DB directory
    test_vector_db = tmp_path / "data" / "chromadb"
    test_vector_db.mkdir(parents=True, exist_ok=True)

    # Initialize a dummy chroma.sqlite3
    chroma_db = test_vector_db / "chroma.sqlite3"
    conn = sqlite3.connect(str(chroma_db))
    conn.execute("CREATE TABLE chroma_test (id INTEGER PRIMARY KEY, key TEXT);")
    conn.execute("INSERT INTO chroma_test (key) VALUES ('vector_key');")
    conn.commit()
    conn.close()

    # Create a dummy index subdirectory
    index_dir = test_vector_db / "dummy_index_folder"
    index_dir.mkdir(exist_ok=True)
    with open(index_dir / "index.bin", "w") as f:
        f.write("dummy vector index data")

    monkeypatch.setenv("VECTOR_DB_PATH", str(test_vector_db))
    monkeypatch.setattr("scripts.backup_db.VECTOR_DB_PATH", str(test_vector_db))

    backup_dir = tmp_path / "backups"
    monkeypatch.setattr("scripts.backup_db.BACKUP_DIR", backup_dir)

    yield tmp_path, test_db, test_vector_db, backup_dir


def test_local_backup_only(temp_backup_env, monkeypatch):
    """Verify that backup script makes a local copy and does not trigger S3 when config is missing."""
    tmp_path, test_db, test_vector_db, backup_dir = temp_backup_env

    # Make sure AWS env variables are empty
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_S3_BUCKET", raising=False)

    monkeypatch.setattr("scripts.backup_db.AWS_ACCESS_KEY", None)
    monkeypatch.setattr("scripts.backup_db.AWS_SECRET_KEY", None)
    monkeypatch.setattr("scripts.backup_db.AWS_BUCKET", None)

    success, db_backup_file, vector_backup_file = run_backup()

    assert success is True
    assert db_backup_file.exists()
    assert db_backup_file.parent == backup_dir

    # Verify backup DB content matches source
    conn = sqlite3.connect(str(db_backup_file))
    row = conn.execute("SELECT name FROM test_table").fetchone()
    assert row[0] == "Test Value"
    conn.close()

    # Verify vector backup exists and contains correct contents
    assert vector_backup_file.exists()
    assert vector_backup_file.parent == backup_dir

    # Unpack the tar.gz to verify contents
    extract_dir = tmp_path / "extracted_vector_backup"
    extract_dir.mkdir()
    with tarfile.open(vector_backup_file, "r:gz") as tar:
        tar.extractall(path=extract_dir)

    # Verify chroma.sqlite3 was backed up safely
    extracted_chroma_db = extract_dir / "chroma.sqlite3"
    assert extracted_chroma_db.exists()
    conn_chroma = sqlite3.connect(str(extracted_chroma_db))
    row_chroma = conn_chroma.execute("SELECT key FROM chroma_test").fetchone()
    assert row_chroma[0] == "vector_key"
    conn_chroma.close()

    # Verify other files/folders were copied
    assert (extract_dir / "dummy_index_folder" / "index.bin").exists()
    with open(extract_dir / "dummy_index_folder" / "index.bin", "r") as f:
        assert f.read() == "dummy vector index data"


@patch("scripts.backup_db.boto3.client")
def test_s3_upload_and_cleanup(mock_boto, temp_backup_env, monkeypatch):
    """Verify that backup uploads both to S3 and cleans up the local files when S3 parameters are present."""
    tmp_path, test_db, test_vector_db, backup_dir = temp_backup_env

    # Setup mock S3 client
    mock_s3_client = MagicMock()
    mock_boto.return_value = mock_s3_client

    monkeypatch.setattr("scripts.backup_db.AWS_ACCESS_KEY", "mock-key")
    monkeypatch.setattr("scripts.backup_db.AWS_SECRET_KEY", "mock-secret")
    monkeypatch.setattr("scripts.backup_db.AWS_BUCKET", "mock-bucket")
    monkeypatch.setattr("scripts.backup_db.AWS_REGION", "us-east-1")

    success, db_backup_file, vector_backup_file = run_backup()

    assert success is True

    # The local files should be deleted after S3 upload
    assert db_backup_file is None or not db_backup_file.exists()
    assert vector_backup_file is None or not vector_backup_file.exists()

    # Check that boto3 upload_file was called with the local path, bucket name, and S3 key
    mock_boto.assert_called_once_with(
        "s3",
        aws_access_key_id="mock-key",
        aws_secret_access_key="mock-secret",
        region_name="us-east-1"
    )

    # upload_file should be called twice (once for main db, once for vector db)
    assert mock_s3_client.upload_file.call_count == 2

    call_args_list = mock_s3_client.upload_file.call_args_list
    bucket_names = [call[0][1] for call in call_args_list]
    s3_keys = [call[0][2] for call in call_args_list]

    assert all(b == "mock-bucket" for b in bucket_names)
    assert any(k.startswith("janus-backups/janus_backup_") for k in s3_keys)
    assert any(k.startswith("janus-backups/chromadb_backup_") for k in s3_keys)

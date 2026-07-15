import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from cryptography.fernet import Fernet

from scripts.backup_db import (
    _apply_local_retention,
    _get_or_arm_s3_retention_marker,
    compute_retain_set,
    run_backup,
)
from src.security import derive_fernet_key


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

    # upload_file should be called three times (main db, vector db, secrets inventory)
    assert mock_s3_client.upload_file.call_count == 3

    call_args_list = mock_s3_client.upload_file.call_args_list
    bucket_names = [call[0][1] for call in call_args_list]
    s3_keys = [call[0][2] for call in call_args_list]

    assert all(b == "mock-bucket" for b in bucket_names)
    assert any(k.startswith("janus-backups/janus_backup_") for k in s3_keys)
    assert any(k.startswith("janus-backups/chromadb_backup_") for k in s3_keys)
    assert any(k.startswith("janus-backups/secrets_inventory_") for k in s3_keys)


def test_secrets_inventory_lists_keys_not_values(temp_backup_env, monkeypatch):
    """Verify the inventory records which secrets are configured, never their values."""
    tmp_path, test_db, test_vector_db, backup_dir = temp_backup_env

    monkeypatch.setattr("scripts.backup_db.AWS_ACCESS_KEY", None)
    monkeypatch.setattr("scripts.backup_db.AWS_SECRET_KEY", None)
    monkeypatch.setattr("scripts.backup_db.AWS_BUCKET", None)
    monkeypatch.setattr("scripts.backup_db.ROOT_DIR", tmp_path)

    (tmp_path / ".env").write_text("SUPER_SECRET_TOKEN=abcd1234\nEMPTY_VAR=\n# comment=ignored\n")
    keys_dir = tmp_path / ".keys"
    keys_dir.mkdir()
    (keys_dir / "jwt_private.pem").write_text("not a real key")

    run_backup()

    inventory_files = list(backup_dir.glob("secrets_inventory_*.json"))
    assert len(inventory_files) == 1
    inventory = json.loads(inventory_files[0].read_text())

    assert "SUPER_SECRET_TOKEN" in inventory["env_vars_configured"]
    assert "EMPTY_VAR" not in inventory["env_vars_configured"]
    assert "jwt_private.pem" in inventory["keys_dir_files"]
    assert "abcd1234" not in json.dumps(inventory)


def test_backup_encrypted_when_key_configured(temp_backup_env, monkeypatch):
    """Verify local artifacts are encrypted (unreadable as sqlite/tar) when a key is set,
    and that decrypting with the configured key recovers the original content."""
    tmp_path, test_db, test_vector_db, backup_dir = temp_backup_env

    monkeypatch.setattr("scripts.backup_db.AWS_ACCESS_KEY", None)
    monkeypatch.setattr("scripts.backup_db.AWS_SECRET_KEY", None)
    monkeypatch.setattr("scripts.backup_db.AWS_BUCKET", None)

    key = Fernet.generate_key()
    monkeypatch.setattr("scripts.backup_db.BACKUP_ENCRYPTION_KEY", key.decode())

    success, db_backup_file, vector_backup_file = run_backup()

    assert success is True
    assert db_backup_file.name.endswith(".db.enc")
    assert vector_backup_file.name.endswith(".tar.gz.enc")

    import scripts.backup_db as backup_db_module

    fernet = Fernet(derive_fernet_key(key.decode(), backup_db_module._BACKUP_PBKDF2_SALT))
    decrypted_db = fernet.decrypt(db_backup_file.read_bytes())
    decrypted_db_path = tmp_path / "decrypted.db"
    decrypted_db_path.write_bytes(decrypted_db)
    conn = sqlite3.connect(str(decrypted_db_path))
    row = conn.execute("SELECT name FROM test_table").fetchone()
    assert row[0] == "Test Value"
    conn.close()


def test_backup_alerts_on_failure(temp_backup_env, monkeypatch):
    """Verify a failed main-DB backup triggers a webhook alert instead of failing silently."""
    tmp_path, test_db, test_vector_db, backup_dir = temp_backup_env

    monkeypatch.setattr("scripts.backup_db.AWS_ACCESS_KEY", None)
    monkeypatch.setattr("scripts.backup_db.AWS_SECRET_KEY", None)
    monkeypatch.setattr("scripts.backup_db.AWS_BUCKET", None)
    monkeypatch.setattr("scripts.backup_db.DB_PATH", str(tmp_path / "does_not_exist.db"))

    with patch("src.notifications.send_webhook_notification") as mock_notify:
        success, db_backup_file, vector_backup_file = run_backup()

    assert success is False
    mock_notify.assert_called_once()
    assert mock_notify.call_args[0][0] == "backup_failed"


def test_retention_keeps_recent_daily_and_weekly_buckets():
    """Pure policy check: last 7 days kept in full, one-per-week kept for the 4 weeks before that,
    anything older is dropped."""
    # 2026-07-15 is a Wednesday, so days=6..9 back (2026-07-06 Mon .. 2026-07-09 Thu) share
    # one ISO week (28) while staying inside the weekly-retention window.
    now = datetime(2026, 7, 15, 0, 0, 0)
    timestamps = [
        now - timedelta(days=1),   # within daily window
        now - timedelta(days=6),   # within daily window
        now - timedelta(days=9),   # ISO week 28 — older of the pair, should be dropped
        now - timedelta(days=8),   # ISO week 28 — more recent, this one is kept instead
        now - timedelta(days=20),  # a different ISO week — kept
        now - timedelta(days=40),  # older than weekly window — dropped
    ]

    retain = compute_retain_set(timestamps, now=now, daily=7, weekly=4)

    assert now - timedelta(days=1) in retain
    assert now - timedelta(days=6) in retain
    assert now - timedelta(days=40) not in retain
    # Exactly one of the two same-week entries is retained, and it's the more recent one.
    week_pair = {now - timedelta(days=9), now - timedelta(days=8)}
    assert len(retain & week_pair) == 1
    assert (now - timedelta(days=8)) in retain


def test_local_retention_first_run_is_a_safety_net(tmp_path, monkeypatch):
    """The first-ever retention run on a backup directory must never delete pre-existing
    history, even if some of it falls outside the daily/weekly window — it only arms a
    marker and prunes nothing. (Added after a real incident: running retention for the
    first time against a backup prefix with months of accumulated history deleted
    everything outside the new window in one shot.)"""
    monkeypatch.setattr("scripts.backup_db.BACKUP_DIR", tmp_path)
    monkeypatch.setattr("scripts.backup_db.BACKUP_RETENTION_DAILY", 7)
    monkeypatch.setattr("scripts.backup_db.BACKUP_RETENTION_WEEKLY", 4)

    now = datetime(2026, 7, 15, 0, 0, 0)
    old_ts = (now - timedelta(days=90)).strftime("%Y-%m-%d_%H%M%S")
    old_db = tmp_path / f"janus_backup_{old_ts}.db"
    old_db.write_text("dummy")

    deleted = _apply_local_retention(run_timestamp=now.strftime("%Y-%m-%d_%H%M%S"), now=now)

    assert deleted == []
    assert old_db.exists()
    assert (tmp_path / ".retention_armed_since").exists()


def test_local_retention_prunes_only_after_arming(tmp_path, monkeypatch):
    """Once armed (on a prior run), backups created after that point are pruned per the
    daily/weekly policy — but anything that predates the arming marker stays protected
    forever, regardless of age."""
    monkeypatch.setattr("scripts.backup_db.BACKUP_DIR", tmp_path)
    monkeypatch.setattr("scripts.backup_db.BACKUP_RETENTION_DAILY", 7)
    monkeypatch.setattr("scripts.backup_db.BACKUP_RETENTION_WEEKLY", 4)

    now = datetime(2026, 7, 15, 0, 0, 0)
    armed_since = now - timedelta(days=100)
    (tmp_path / ".retention_armed_since").write_text(armed_since.strftime("%Y-%m-%d_%H%M%S"))

    predates_arming_ts = (now - timedelta(days=200)).strftime("%Y-%m-%d_%H%M%S")
    predates_arming = tmp_path / f"janus_backup_{predates_arming_ts}.db"

    post_arming_but_old_ts = (now - timedelta(days=90)).strftime("%Y-%m-%d_%H%M%S")
    post_arming_but_old = tmp_path / f"janus_backup_{post_arming_but_old_ts}.db"

    recent_ts = (now - timedelta(days=1)).strftime("%Y-%m-%d_%H%M%S")
    recent = tmp_path / f"janus_backup_{recent_ts}.db"

    for f in (predates_arming, post_arming_but_old, recent):
        f.write_text("dummy")

    deleted = _apply_local_retention(run_timestamp=now.strftime("%Y-%m-%d_%H%M%S"), now=now)

    assert str(post_arming_but_old) in deleted
    assert not post_arming_but_old.exists()
    # Predates the arming marker — must never be touched, even though it's just as old.
    assert predates_arming.exists()
    assert recent.exists()


def test_cron_invocation_can_import_notifications(tmp_path):
    """Regression test for a real incident: `python scripts/backup_db.py` — the exact
    invocation cron and docs/cloud_operations_guide.md use — puts the script's own directory
    on sys.path, not the repo root, so the lazy `from src.notifications import
    send_webhook_notification` inside _notify_failure raised ModuleNotFoundError on every
    failure, silently swallowing every alert this feature exists to send. Runs the real
    script as a subprocess, exactly as cron does, and forces a failure to confirm the import
    now resolves."""
    repo_root = Path(__file__).resolve().parent.parent
    env = {
        **os.environ,
        "DB_PATH": str(tmp_path / "does_not_exist.db"),
        "VECTOR_DB_PATH": str(tmp_path / "does_not_exist_either"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "AWS_ACCESS_KEY_ID": "",
        "AWS_SECRET_ACCESS_KEY": "",
        "AWS_S3_BUCKET": "",
    }

    result = subprocess.run(
        [sys.executable, "scripts/backup_db.py"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    combined_output = result.stdout + result.stderr
    assert "ModuleNotFoundError" not in combined_output
    assert "No module named" not in combined_output
    assert "BACKUP FAILURE" in combined_output


def test_failed_integrity_check_does_not_leave_plaintext_on_disk(temp_backup_env, monkeypatch):
    """A backup that fails its own integrity check must not linger on disk, where retention
    would otherwise eventually treat it as a legitimate, restorable backup."""
    tmp_path, test_db, test_vector_db, backup_dir = temp_backup_env

    monkeypatch.setattr("scripts.backup_db.AWS_ACCESS_KEY", None)
    monkeypatch.setattr("scripts.backup_db.AWS_SECRET_KEY", None)
    monkeypatch.setattr("scripts.backup_db.AWS_BUCKET", None)
    monkeypatch.setattr("scripts.backup_db._verify_sqlite_integrity", lambda path: False)

    success, db_backup_file, vector_backup_file = run_backup()

    assert success is False
    assert db_backup_file is None
    assert list(backup_dir.glob("janus_backup_*.db")) == []


def test_failed_encryption_does_not_leave_plaintext_on_disk(temp_backup_env, monkeypatch):
    """If encryption fails after a backup already passed its integrity check, the plaintext
    original must still be cleaned up — a half-encrypted run must never leave a plaintext
    copy of conversation history sitting in backups/ for days until retention catches it."""
    tmp_path, test_db, test_vector_db, backup_dir = temp_backup_env

    monkeypatch.setattr("scripts.backup_db.AWS_ACCESS_KEY", None)
    monkeypatch.setattr("scripts.backup_db.AWS_SECRET_KEY", None)
    monkeypatch.setattr("scripts.backup_db.AWS_BUCKET", None)
    monkeypatch.setattr("scripts.backup_db.BACKUP_ENCRYPTION_KEY", "some-secret")
    monkeypatch.setattr(
        "scripts.backup_db._backup_fernet_key",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    success, db_backup_file, vector_backup_file = run_backup()

    assert success is False
    assert db_backup_file is None
    assert list(backup_dir.glob("janus_backup_*.db")) == []


@patch("scripts.backup_db.boto3.client")
def test_s3_client_construction_failure_does_not_crash_run(mock_boto, temp_backup_env, monkeypatch):
    """A broken S3 client (bad botocore config, etc.) must not crash the whole backup run —
    local retention and the failure alert still need to run afterward."""
    tmp_path, test_db, test_vector_db, backup_dir = temp_backup_env

    mock_boto.side_effect = RuntimeError("boto3 config error")

    monkeypatch.setattr("scripts.backup_db.AWS_ACCESS_KEY", "mock-key")
    monkeypatch.setattr("scripts.backup_db.AWS_SECRET_KEY", "mock-secret")
    monkeypatch.setattr("scripts.backup_db.AWS_BUCKET", "mock-bucket")

    with patch("src.notifications.send_webhook_notification") as mock_notify:
        success, db_backup_file, vector_backup_file = run_backup()

    assert success is False
    mock_notify.assert_called_once()
    # Local files should still exist — the S3 client never got constructed to upload them.
    assert db_backup_file.exists()


@patch("scripts.backup_db.boto3.client")
def test_inventory_upload_failure_is_non_fatal(mock_boto, temp_backup_env, monkeypatch):
    """A failed S3 upload of the (non-critical) secrets inventory must not flip
    overall_success or fire the failure webhook — only db/vector upload failures are
    backup-critical."""
    tmp_path, test_db, test_vector_db, backup_dir = temp_backup_env

    mock_s3_client = MagicMock()

    def upload_side_effect(local_path, bucket, key):
        if "secrets_inventory" in key:
            raise RuntimeError("inventory upload failed")

    mock_s3_client.upload_file.side_effect = upload_side_effect
    mock_boto.return_value = mock_s3_client

    monkeypatch.setattr("scripts.backup_db.AWS_ACCESS_KEY", "mock-key")
    monkeypatch.setattr("scripts.backup_db.AWS_SECRET_KEY", "mock-secret")
    monkeypatch.setattr("scripts.backup_db.AWS_BUCKET", "mock-bucket")

    with patch("src.notifications.send_webhook_notification") as mock_notify:
        success, db_backup_file, vector_backup_file = run_backup()

    assert success is True
    mock_notify.assert_not_called()


def test_s3_marker_read_error_does_not_silently_rearm():
    """A transient/real S3 error while reading the retention marker (throttling, permissions)
    must propagate rather than being treated the same as "marker doesn't exist yet" — silently
    resetting an existing older marker to "now" would erase the protection the marker exists
    to provide."""
    mock_s3 = MagicMock()
    mock_s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject"
    )

    with pytest.raises(ClientError):
        _get_or_arm_s3_retention_marker(mock_s3, "2026-07-15_000000")


def test_s3_marker_missing_key_arms_normally():
    """A genuinely missing marker (NoSuchKey) is the expected first-run case and should arm
    normally rather than propagate."""
    mock_s3 = MagicMock()
    mock_s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject"
    )

    armed_since, is_first_run = _get_or_arm_s3_retention_marker(mock_s3, "2026-07-15_000000")

    assert is_first_run is True
    assert armed_since == datetime(2026, 7, 15, 0, 0, 0)
    mock_s3.put_object.assert_called_once()

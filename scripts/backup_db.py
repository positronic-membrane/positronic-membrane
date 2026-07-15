import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from cryptography.fernet import Fernet
from dotenv import load_dotenv

# Load configuration
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

# This script is invoked directly (`python scripts/backup_db.py`, per cron and the
# docs), not via `-m`. In that mode Python puts the script's own directory
# (scripts/) on sys.path, not the repo root — the editable install's .pth only
# adds src/, not the repo root itself — so `import src.notifications` below would
# raise ModuleNotFoundError unless the repo root is added explicitly.
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

DB_PATH = os.getenv("DB_PATH", str(ROOT_DIR / "janus.db"))
VECTOR_DB_PATH = os.getenv("VECTOR_DB_PATH", str(ROOT_DIR / "data" / "chromadb"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(ROOT_DIR / "backups")))
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# S3 Configuration
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_BUCKET = os.getenv("AWS_S3_BUCKET")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
S3_PREFIX = "janus-backups/"

# Encryption: any high-entropy secret string, PBKDF2-derived into a Fernet key via the same
# mechanism src/security.py uses for JANUS_ENCRYPTION_KEY (separate salt — a different key
# domain, deliberately not shared with JANUS_ENCRYPTION_KEY). If unset, backups are written
# unencrypted — set this in production; the archive contains conversation history.
BACKUP_ENCRYPTION_KEY = os.getenv("BACKUP_ENCRYPTION_KEY")
_BACKUP_PBKDF2_SALT = b"janus-backup-v1-fernet-salt"


@lru_cache(maxsize=1)
def _backup_fernet_key() -> bytes:
    from src.security import derive_fernet_key

    return derive_fernet_key(BACKUP_ENCRYPTION_KEY, _BACKUP_PBKDF2_SALT)

# Retention: keep every backup from the last N days, plus one backup per ISO week for the
# M weeks before that. Anything older is pruned.
BACKUP_RETENTION_DAILY = int(os.getenv("BACKUP_RETENTION_DAILY", "7"))
BACKUP_RETENTION_WEEKLY = int(os.getenv("BACKUP_RETENTION_WEEKLY", "4"))

TIMESTAMP_FORMAT = "%Y-%m-%d_%H%M%S"
TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{6})")


def _notify_failure(message: str) -> None:
    """Best-effort alert on backup failure. A silently-failing backup job is worse than none,
    so every failure path below routes through here — but a broken notification channel must
    never mask the underlying backup failure, hence the broad except."""
    print(f"BACKUP FAILURE: {message}")
    try:
        from src.notifications import send_webhook_notification

        send_webhook_notification("backup_failed", message)
    except Exception as e:
        print(f"Additionally failed to send backup failure notification: {e}")


def _verify_sqlite_integrity(path) -> bool:
    """Runs PRAGMA integrity_check against a standalone sqlite file. Backups are produced via
    the sqlite3 online backup API, so the file being checked here already *is* a restored copy,
    not the live database."""
    try:
        conn = sqlite3.connect(str(path))
        try:
            result = conn.execute("PRAGMA integrity_check;").fetchone()
        finally:
            conn.close()
        return bool(result) and result[0] == "ok"
    except Exception as e:
        print(f"Integrity check failed for {path}: {e}")
        return False


def _verify_tar_integrity(path) -> bool:
    """Sanity-checks a tar.gz archive: it must open cleanly and contain at least one non-empty
    file member. This is the "checksum + size sanity check" fallback for the vector DB archive,
    which mixes a sqlite file with opaque index files that PRAGMA integrity_check can't cover."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            members = tar.getmembers()
            return any(m.isfile() and m.size > 0 for m in members)
    except Exception as e:
        print(f"Integrity check failed for {path}: {e}")
        return False


def _encrypt_file(path: Path) -> Path:
    """Encrypts `path` in place with Fernet (symmetric) when BACKUP_ENCRYPTION_KEY is configured,
    replacing it with a `.enc`-suffixed sibling and removing the plaintext. Returns `path`
    unchanged if no key is configured. Raises on a misconfigured key rather than silently
    falling back to unencrypted output — a backup that's silently unencrypted defeats the point
    of setting the key at all. Callers must treat any exception here as "the plaintext at
    `path` may still exist and needs explicit cleanup" — this function does not guarantee the
    plaintext is gone unless it returns normally."""
    if not BACKUP_ENCRYPTION_KEY:
        return path
    fernet = Fernet(_backup_fernet_key())
    encrypted_path = path.with_name(path.name + ".enc")
    with open(path, "rb") as f:
        token = fernet.encrypt(f.read())
    with open(encrypted_path, "wb") as f:
        f.write(token)
    path.unlink()
    return encrypted_path


def _cleanup_local_artifact(path) -> None:
    """Best-effort removal of a local backup artifact that failed verification or encryption.
    A half-produced or unverified artifact must never be left for the retention pass to
    eventually catch — for an encrypted-at-rest guarantee, a plaintext copy that failed
    specifically because it couldn't be trusted/encrypted must not linger on disk in the
    meantime (retention only prunes past the daily/weekly window, not immediately)."""
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
    except Exception as e:
        print(f"Failed to clean up incomplete backup artifact {path}: {e}")


def _build_secrets_inventory() -> dict:
    """Lists which secret-bearing env vars and .keys/ files are *configured*, without capturing
    any values — this is a restore checklist (what needs to be reconstructed), not a secrets
    export. Never put actual credentials in the backup archive."""
    env_path = ROOT_DIR / ".env"
    configured_env_vars = []
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if value.strip():
                configured_env_vars.append(key.strip())

    keys_dir = ROOT_DIR / ".keys"
    key_files = sorted(p.name for p in keys_dir.glob("*")) if keys_dir.is_dir() else []

    return {
        "generated_at": datetime.now().isoformat(),
        "env_vars_configured": sorted(configured_env_vars),
        "keys_dir_files": key_files,
    }


def compute_retain_set(timestamps, now=None, daily=7, weekly=4):
    """Given the distinct backup timestamps present, returns the subset to keep: every timestamp
    within the last `daily` days, plus the single most recent timestamp per ISO week for the
    `weekly` weeks before that. Pure function (no I/O) so the policy is unit-testable on its own."""
    if now is None:
        now = datetime.now()
    daily_cutoff = now - timedelta(days=daily)
    weekly_cutoff = now - timedelta(days=daily + weekly * 7)

    retain = {ts for ts in timestamps if ts >= daily_cutoff}

    week_buckets = {}
    for ts in timestamps:
        if weekly_cutoff <= ts < daily_cutoff:
            bucket_key = ts.isocalendar()[:2]  # (iso_year, iso_week)
            if bucket_key not in week_buckets or ts > week_buckets[bucket_key]:
                week_buckets[bucket_key] = ts
    retain.update(week_buckets.values())

    return retain


LOCAL_RETENTION_MARKER_NAME = ".retention_armed_since"
S3_RETENTION_MARKER_KEY = f"{S3_PREFIX}{LOCAL_RETENTION_MARKER_NAME}"


def _get_or_arm_local_retention_marker(run_timestamp: str) -> tuple:
    """Returns (armed_since, is_first_run). The first time retention ever runs on this
    backup directory there is no marker file yet — rather than pruning against whatever
    history already happens to be sitting there (which could be months of backups that
    predate this feature), it plants a marker at the current run's timestamp and reports
    is_first_run=True so the caller skips pruning entirely for this run. From then on,
    only backups created at/after the marker are ever eligible for deletion — pre-existing
    history is permanently out of reach of automatic pruning."""
    marker_path = BACKUP_DIR / LOCAL_RETENTION_MARKER_NAME
    if marker_path.exists():
        try:
            return datetime.strptime(marker_path.read_text().strip(), TIMESTAMP_FORMAT), False
        except ValueError:
            pass  # corrupt marker — re-arm below rather than guess
    armed_since = datetime.strptime(run_timestamp, TIMESTAMP_FORMAT)
    marker_path.write_text(run_timestamp)
    return armed_since, True


def _get_or_arm_s3_retention_marker(s3_client, run_timestamp: str) -> tuple:
    """S3 counterpart to `_get_or_arm_local_retention_marker` — same first-run safeguard,
    backed by a marker object instead of a marker file. Only a genuinely missing/corrupt
    marker is treated as "arm it" — a transient S3 error (throttling, a permissions hiccup)
    must propagate instead of silently resetting an existing older marker to "now", which
    would erase the very history-protection floor this mechanism exists to provide."""
    body = None
    try:
        obj = s3_client.get_object(Bucket=AWS_BUCKET, Key=S3_RETENTION_MARKER_KEY)
        body = obj["Body"].read().decode().strip()
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code not in ("NoSuchKey", "404"):
            raise  # a real S3 error — don't silently re-arm over it

    if body:
        try:
            return datetime.strptime(body, TIMESTAMP_FORMAT), False
        except (ValueError, TypeError):
            pass  # corrupt marker content — re-arm below rather than guess

    armed_since = datetime.strptime(run_timestamp, TIMESTAMP_FORMAT)
    s3_client.put_object(Bucket=AWS_BUCKET, Key=S3_RETENTION_MARKER_KEY, Body=run_timestamp.encode())
    return armed_since, True


def _apply_local_retention(run_timestamp=None, now=None) -> list:
    """Prunes local backup files (db/vector/inventory, encrypted or not) that fall outside the
    retention policy. Returns the list of deleted file paths. Never deletes anything older than
    the retention marker — see `_get_or_arm_local_retention_marker`."""
    if run_timestamp is None:
        run_timestamp = (now or datetime.now()).strftime(TIMESTAMP_FORMAT)
    armed_since, is_first_run = _get_or_arm_local_retention_marker(run_timestamp)
    if is_first_run:
        print("Retention policy armed for the first time on this backup directory — "
              "skipping pruning this run so no pre-existing history is touched.")
        return []

    groups = {}
    for f in BACKUP_DIR.glob("*"):
        if not f.is_file() or f.name == LOCAL_RETENTION_MARKER_NAME:
            continue
        m = TIMESTAMP_RE.search(f.name)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), TIMESTAMP_FORMAT)
        except ValueError:
            continue
        groups.setdefault(ts, []).append(f)

    retain = compute_retain_set(
        list(groups.keys()), now=now, daily=BACKUP_RETENTION_DAILY, weekly=BACKUP_RETENTION_WEEKLY
    )
    deleted = []
    for ts, files in groups.items():
        if ts in retain or ts < armed_since:
            continue
        for f in files:
            try:
                f.unlink()
                deleted.append(str(f))
            except Exception as e:
                print(f"Failed to prune old local backup {f}: {e}")
    return deleted


def _apply_s3_retention(s3_client, run_timestamp=None, now=None) -> list:
    """Prunes S3 backup objects under the janus-backups/ prefix that fall outside the retention
    policy. Returns the list of deleted keys. Bounded to a single list_objects_v2 page (1000
    objects) — ample for a nightly cron against a 7-daily/4-weekly policy. Never deletes
    anything older than the retention marker — see `_get_or_arm_s3_retention_marker`. Raises on
    failure rather than self-reporting — the caller (run_backup) is responsible for collecting
    and reporting failures uniformly, matching `_apply_local_retention`'s contract."""
    if run_timestamp is None:
        run_timestamp = (now or datetime.now()).strftime(TIMESTAMP_FORMAT)
    armed_since, is_first_run = _get_or_arm_s3_retention_marker(s3_client, run_timestamp)
    if is_first_run:
        print("Retention policy armed for the first time on this S3 prefix — "
              "skipping pruning this run so no pre-existing history is touched.")
        return []

    response = s3_client.list_objects_v2(Bucket=AWS_BUCKET, Prefix=S3_PREFIX)
    keys_by_ts = {}
    for obj in response.get("Contents", []):
        if obj["Key"] == S3_RETENTION_MARKER_KEY:
            continue
        m = TIMESTAMP_RE.search(obj["Key"])
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), TIMESTAMP_FORMAT)
        except ValueError:
            continue
        keys_by_ts.setdefault(ts, []).append(obj["Key"])

    retain = compute_retain_set(
        list(keys_by_ts.keys()), now=now, daily=BACKUP_RETENTION_DAILY, weekly=BACKUP_RETENTION_WEEKLY
    )
    to_delete = [
        key for ts, keys in keys_by_ts.items() if ts not in retain and ts >= armed_since for key in keys
    ]
    if to_delete:
        s3_client.delete_objects(
            Bucket=AWS_BUCKET,
            Delete={"Objects": [{"Key": k} for k in to_delete]},
        )
    return to_delete


def run_backup():
    print("Initiating database backups (SQLite main DB & Chroma Vector DB)...")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
    failures = []

    local_db_backup_path = BACKUP_DIR / f"janus_backup_{timestamp}.db"
    local_vector_backup_path = BACKUP_DIR / f"chromadb_backup_{timestamp}.tar.gz"

    db_success = False
    vector_success = False

    # 1. Perform main SQLite Online Backup (safely copying WAL active database)
    try:
        if os.path.exists(DB_PATH):
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(local_db_backup_path)
            with dst:
                src.backup(dst)
            dst.close()
            src.close()
            if _verify_sqlite_integrity(local_db_backup_path):
                print(f"Local main DB backup created and verified: {local_db_backup_path}")
                db_success = True
            else:
                failures.append("main DB backup failed integrity check")
                _cleanup_local_artifact(local_db_backup_path)
        else:
            failures.append(f"main database path does not exist: {DB_PATH}")
    except Exception as e:
        failures.append(f"main SQLite online backup error: {e}")

    if db_success:
        try:
            local_db_backup_path = _encrypt_file(local_db_backup_path)
        except Exception as e:
            failures.append(f"main DB backup encryption failed: {e}")
            db_success = False
            _cleanup_local_artifact(local_db_backup_path)

    # 2. Perform Chroma Vector DB safe backup and archiving
    temp_vector_backup_dir = BACKUP_DIR / f"chromadb_temp_{timestamp}"
    try:
        if os.path.exists(VECTOR_DB_PATH) and os.path.isdir(VECTOR_DB_PATH):
            print(f"Backing up Chroma Vector DB from {VECTOR_DB_PATH}...")
            os.makedirs(temp_vector_backup_dir, exist_ok=True)

            # Safe copy of chroma.sqlite3
            chroma_db_src = os.path.join(VECTOR_DB_PATH, "chroma.sqlite3")
            chroma_db_dst = os.path.join(temp_vector_backup_dir, "chroma.sqlite3")
            if os.path.exists(chroma_db_src):
                src_conn = sqlite3.connect(chroma_db_src)
                dst_conn = sqlite3.connect(chroma_db_dst)
                with dst_conn:
                    src_conn.backup(dst_conn)
                dst_conn.close()
                src_conn.close()
                print("Chroma SQLite backup created successfully.")

            # Copy all other folders (indexes) and files (skipping chroma.sqlite3 itself)
            for item in os.listdir(VECTOR_DB_PATH):
                s = os.path.join(VECTOR_DB_PATH, item)
                d = os.path.join(temp_vector_backup_dir, item)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                elif os.path.isfile(s):
                    if item.startswith("chroma.sqlite3"):
                        continue
                    shutil.copy2(s, d)

            # Create compressed archive of the staging directory
            shutil.make_archive(
                base_name=str(BACKUP_DIR / f"chromadb_backup_{timestamp}"),
                format="gztar",
                root_dir=str(temp_vector_backup_dir),
            )
            if _verify_tar_integrity(local_vector_backup_path):
                print(f"Local Chroma Vector DB archive created and verified: {local_vector_backup_path}")
                vector_success = True
            else:
                failures.append("vector DB archive failed integrity check")
                _cleanup_local_artifact(local_vector_backup_path)
        else:
            print(f"Vector DB path does not exist or is not a directory: {VECTOR_DB_PATH}")
    except Exception as e:
        failures.append(f"Chroma Vector DB backup error: {e}")
    finally:
        if os.path.exists(temp_vector_backup_dir):
            shutil.rmtree(temp_vector_backup_dir)

    if vector_success:
        try:
            local_vector_backup_path = _encrypt_file(local_vector_backup_path)
        except Exception as e:
            failures.append(f"vector DB archive encryption failed: {e}")
            vector_success = False
            _cleanup_local_artifact(local_vector_backup_path)

    # 3. Write a secrets *inventory* (which env vars / key files exist, never their values).
    # Best-effort and non-fatal throughout — it's a restore convenience, not a critical artifact,
    # so failures here are logged but never flip overall_success or fire the failure webhook.
    inventory_success = False
    local_inventory_path = BACKUP_DIR / f"secrets_inventory_{timestamp}.json"
    try:
        local_inventory_path.write_text(json.dumps(_build_secrets_inventory(), indent=2))
        local_inventory_path = _encrypt_file(local_inventory_path)
        inventory_success = True
    except Exception as e:
        print(f"Secrets inventory generation failed (non-fatal): {e}")
        _cleanup_local_artifact(local_inventory_path)

    # Resolve paths to return
    db_file_result = local_db_backup_path if db_success else None
    vector_file_result = local_vector_backup_path if vector_success else None
    inventory_file_result = local_inventory_path if inventory_success else None

    # We require the main DB to succeed. If vector DB exists, it should succeed too.
    vector_db_exists = os.path.exists(VECTOR_DB_PATH) and os.path.isdir(VECTOR_DB_PATH)
    overall_success = db_success and (not vector_db_exists or vector_success)

    # 4. Upload to S3 if AWS configuration is present
    if AWS_ACCESS_KEY and AWS_SECRET_KEY and AWS_BUCKET:
        print(f"Uploading backups to S3 bucket '{AWS_BUCKET}'...")
        try:
            s3_client = boto3.client(
                "s3",
                aws_access_key_id=AWS_ACCESS_KEY,
                aws_secret_access_key=AWS_SECRET_KEY,
                region_name=AWS_REGION,
            )
        except Exception as e:
            s3_client = None
            failures.append(f"S3 client initialization failed: {e}")
            overall_success = False

        if s3_client is not None:
            for label, local_path, is_result_attr in (
                ("main DB", db_file_result, "db"),
                ("vector DB", vector_file_result, "vector"),
                ("secrets inventory", inventory_file_result, "inventory"),
            ):
                if not local_path or not os.path.exists(local_path):
                    continue
                try:
                    s3_key = f"{S3_PREFIX}{Path(local_path).name}"
                    s3_client.upload_file(str(local_path), AWS_BUCKET, s3_key)
                    print(f"S3 upload for {label} completed successfully! Key: {s3_key}")
                    os.remove(local_path)
                    if is_result_attr == "db":
                        db_file_result = None
                    elif is_result_attr == "vector":
                        vector_file_result = None
                    else:
                        inventory_file_result = None
                except Exception as e:
                    if is_result_attr in ("db", "vector"):
                        failures.append(f"S3 upload failed for {label}: {e}")
                        overall_success = False
                    else:
                        # Inventory is a restore convenience, not critical — non-fatal,
                        # matches the inventory-generation failure path above.
                        print(f"S3 upload failed for {label} (non-fatal): {e}")

            try:
                deleted = _apply_s3_retention(s3_client, run_timestamp=timestamp)
                if deleted:
                    print(f"Pruned {len(deleted)} old S3 backup object(s) per retention policy.")
            except Exception as e:
                failures.append(f"S3 retention cleanup error: {e}")
    else:
        print("AWS credentials not configured. Backups kept locally.")

    try:
        deleted_local = _apply_local_retention(run_timestamp=timestamp)
        if deleted_local:
            print(f"Pruned {len(deleted_local)} old local backup file(s) per retention policy.")
    except Exception as e:
        failures.append(f"local retention cleanup error: {e}")

    if not overall_success or failures:
        _notify_failure("; ".join(failures) if failures else "backup run reported failure")

    return overall_success, db_file_result, vector_file_result


if __name__ == "__main__":
    run_backup()

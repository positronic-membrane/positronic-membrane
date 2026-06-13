import sqlite3
import os
from datetime import datetime
import boto3
from pathlib import Path
from dotenv import load_dotenv

# Load configuration
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

DB_PATH = os.getenv("DB_PATH", str(ROOT_DIR / "janus.db"))
VECTOR_DB_PATH = os.getenv("VECTOR_DB_PATH", str(ROOT_DIR / "data" / "chromadb"))
BACKUP_DIR = ROOT_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)

# S3 Configuration
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_BUCKET = os.getenv("AWS_S3_BUCKET")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

def run_backup():
    print("Initiating database backups (SQLite main DB & Chroma Vector DB)...")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    
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
            print(f"Local main DB backup created successfully: {local_db_backup_path}")
            db_success = True
        else:
            print(f"Main database path does not exist: {DB_PATH}")
    except Exception as e:
        print(f"Error during main SQLite online backup: {e}")

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
            import shutil
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
                root_dir=str(temp_vector_backup_dir)
            )
            print(f"Local Chroma Vector DB archive created successfully: {local_vector_backup_path}")
            vector_success = True
        else:
            print(f"Vector DB path does not exist or is not a directory: {VECTOR_DB_PATH}")
    except Exception as e:
        print(f"Error during Chroma Vector DB backup: {e}")
    finally:
        if os.path.exists(temp_vector_backup_dir):
            import shutil
            shutil.rmtree(temp_vector_backup_dir)

    # Resolve paths to return
    db_file_result = local_db_backup_path if db_success else None
    vector_file_result = local_vector_backup_path if vector_success else None
    
    # We require the main DB to succeed. If vector DB exists, it should succeed too.
    vector_db_exists = os.path.exists(VECTOR_DB_PATH) and os.path.isdir(VECTOR_DB_PATH)
    overall_success = db_success and (not vector_db_exists or vector_success)
    
    # 3. Upload to S3 if AWS configuration is present
    if AWS_ACCESS_KEY and AWS_SECRET_KEY and AWS_BUCKET:
        print(f"Uploading backups to S3 bucket '{AWS_BUCKET}'...")
        try:
            s3_client = boto3.client(
                "s3",
                aws_access_key_id=AWS_ACCESS_KEY,
                aws_secret_access_key=AWS_SECRET_KEY,
                region_name=AWS_REGION
            )
            
            # Upload main DB backup
            if db_file_result and os.path.exists(db_file_result):
                s3_key_db = f"janus-backups/janus_backup_{timestamp}.db"
                s3_client.upload_file(str(db_file_result), AWS_BUCKET, s3_key_db)
                print(f"S3 upload for main DB completed successfully! Key: {s3_key_db}")
                os.remove(db_file_result)
                print("Local main DB backup file removed (S3 storage active).")
                db_file_result = None
                
            # Upload vector DB backup
            if vector_file_result and os.path.exists(vector_file_result):
                s3_key_vector = f"janus-backups/chromadb_backup_{timestamp}.tar.gz"
                s3_client.upload_file(str(vector_file_result), AWS_BUCKET, s3_key_vector)
                print(f"S3 upload for Chroma DB completed successfully! Key: {s3_key_vector}")
                os.remove(vector_file_result)
                print("Local Chroma DB backup file removed (S3 storage active).")
                vector_file_result = None
                
        except Exception as e:
            print(f"Failed to upload backup to S3: {e}")
            overall_success = False
    else:
        print("AWS credentials not configured. Backups kept locally.")
        
    return overall_success, db_file_result, vector_file_result

if __name__ == "__main__":
    run_backup()

# Project Janus: Cloud Operations & Migration History

This guide provides instructions for managing the Project Janus daemon on your cloud droplet, followed by a historical log of the migration steps and troubleshooting decisions made during the setup.

---

## Part 1: Daemon Operations Guide

Project Janus runs as a background systemd service on your Ubuntu droplet. You can control the application (both the FastAPI Web server and the background heartbeat swarm) using the following commands.

### 1. Checking Status and Logs
*   **Check Daemon Status:**
    ```bash
    sudo systemctl status janus
    ```
    This shows whether the daemon is active, its uptime, memory usage, and the last few lines of log output.

*   **View Real-Time Live Logs:**
    ```bash
    journalctl -u janus -f
    ```
    Press `Ctrl+C` to exit the log stream.

*   **View Last 100 Log Lines (Paged):**
    ```bash
    journalctl -u janus -n 100 --no-pager
    ```

### 2. Starting, Stopping, and Restarting
*   **Start Janus:**
    ```bash
    sudo systemctl start janus
    ```
*   **Stop Janus:**
    ```bash
    sudo systemctl stop janus
    ```
*   **Restart Janus:**
    ```bash
    sudo systemctl restart janus
    ```
    *(Use this after editing `.env` configuration files or pulling updates from Git).*

### 3. Enabling/Disabling Auto-Start on Boot
*   **Enable auto-start on boot (Active):**
    ```bash
    sudo systemctl enable janus
    ```
*   **Disable auto-start on boot:**
    ```bash
    sudo systemctl disable janus
    ```

---

## Part 2: Migration Chronology & Troubleshooting Log

This section documents the steps taken on **June 12, 2026** to successfully migrate Project Janus from a local iMac to a DigitalOcean droplet.

### Step 1: VM Provisioning & Network Security
*   **Action:** Provisioned a DigitalOcean Droplet running **Ubuntu 24.04 LTS** (1 vCPU, 2GB RAM) in the NYC region.
*   **Action:** Configured a DigitalOcean cloud firewall to secure the droplet, opening only:
    *   **Port 22** (SSH/SFTP and remote terminal management)
    *   **Port 5005** (Janus Web Interface/API)

### Step 2: Code Repository Deployment (Droplet)
*   **Action:** Attempted to clone the GitHub repository into `/opt/janus`.
*   **Troubleshooting:**
    *   *Symptom:* SSH handshake failed with `git@github.com: Permission denied (publickey)`.
    *   *Resolution:* Generated an SSH key on the droplet (`ssh-keygen -t ed25519 -C "janus-prod-droplet"`) and registered the public key as a **Deploy Key** with **Write Access** on the GitHub repository page. Re-ran the clone to successfully deploy code to `/opt/janus`.

### Step 3: Local Filesystem Mounting (SFTP/SSHFS)
*   **Action:** Configured a local folder mount on the iMac to allow direct code editing on the VM, bypassing bidirectional Git sync conflicts once Janus begins self-modification.
*   **Troubleshooting:**
    *   *Symptom 1:* Initial mount attempt immediately disconnected with `remote host has disconnected`.
        *   *Cause:* The target directory `/opt/janus` had not been successfully created on the server yet.
    *   *Symptom 2:* Accessing the mount point resulted in `ls: janus-remote: Operation not permitted`.
        *   *Cause:* macOS sandboxing and user privilege mismatches between local iMac accounts and the remote `root` account.
        *   *Resolution:* Installed `macFUSE` from the official website and mounted the folder using specific macOS FUSE flags:
            ```bash
            sshfs root@192.241.154.234:/opt/janus ~/projects/janus-remote -o follow_symlinks,reconnect,defer_permissions,local
            ```
            `defer_permissions` delegated control checks to the VM, and `local` marked it as a local drive, resolving the sandbox lock.

### Step 4: Antigravity CLI Installation
*   **Action:** Installed the `agy` command-line utility on the droplet:
    ```bash
    curl -fsSL https://antigravity.google/cli/install.sh | bash
    ```
*   **Key Discovery:** The CLI automatically authenticated and authorized the correct user account and active subscription upon first launch. This occurred via secure credential forwarding over the remote IDE connection tunnel, avoiding manual headless login configuration.

### Step 5: systemd Daemon Configuration
*   **Action:** Created the service configuration file `/etc/systemd/system/janus.service`.
*   **Troubleshooting:**
    *   *Symptom 1:* Failed with status `217/USER`.
        *   *Resolution:* Replaced the template placeholder `User=<user>` with `User=root`.
    *   *Symptom 2:* Warning `Assignment outside of section. Ignoring.`.
        *   *Resolution:* Cleared a stray line header at the very top of the configuration file.
    *   *Symptom 3:* Failed with status `203/EXEC`.
        *   *Resolution:* Noticed the droplet's virtual environment folder was named `.venv` (with a dot prefix) rather than the standard template `venv`. Corrected the executable path to `/opt/janus/.venv/bin/python`.
*   **Outcome:** The service started successfully. The FastAPI server and background heartbeat swarm run concurrently. The web interface is fully responsive at `http://192.241.154.234:5005`.

---

## Part 3: Path and Environment Reference

*   **Remote Project Path:** `/opt/janus`
*   **Remote Python Binary:** `/opt/janus/.venv/bin/python`
*   **Local iMac Mount Path:** `~/projects/janus-remote`
*   **Active Port:** `5005` (API & Web Chat SPA)
*   **Service Name:** `janus.service`

---

## Part 4: Security and Access Control

Because Janus is now hosted in the cloud, you must protect your instance from unauthorized access.

### 1. Application-Level Authentication (JWT)
Janus is natively protected by **RS256 JWT Authentication**. Every API call and WebSocket session (`/ws/chat`) requires a valid token or a secure `party_id` (UUID) registered in the SQLite `parties` database table. Without it, the server returns a `401 Unauthorized` status.

### 2. Network-Level Security (Recommended Actions)
Because Janus serves over plain HTTP (`http://<droplet-ip>:5005`), your tokens are sent in cleartext across the internet. To secure your setup:

*   **IP Whitelisting (Simplest & Safest):**
    Open your **DigitalOcean Control Panel**, locate the cloud firewall protecting your droplet, and modify the inbound rule for **Port 5005**:
    *   Change the source from `All IPv4 / All IPv6` to your specific local public IP address (e.g. your home or office IP).
    *   *Result:* Only your local network will be allowed to connect to the Janus web server; all other connection attempts will be silently dropped.

*   **Reverse Proxy with SSL (For Secure Public Access):**
    If you need to access Janus securely from anywhere, set up a reverse proxy (like **Nginx** or **Caddy**) with Let's Encrypt to enable **HTTPS (Port 443)**, and block direct public access on port `5005`.

---

## Part 5: Database Backups & Disaster Recovery (S3)

To ensure the safety of Project Janus, a dual-database backup system is automated on your droplet.

### 1. What is Backed Up?
The backup system takes consistent snapshots of both databases used by Janus:
*   **Main Database (`janus.db`):** Safe SQLite online copy of the active database (safely handling WAL/journal modes).
*   **Chroma Vector Database (`data/chromadb`):** Safe SQLite online copy of `chroma.sqlite3`, combined with a recursive copy of the semantic vector index directories, packaged as a compressed `.tar.gz` archive.

### 2. S3 Backup Configuration
Backups are triggered nightly at midnight (`00:00`) via a system crontab entry for the `root` user:
```bash
0 0 * * * cd /opt/janus && .venv/bin/python scripts/backup_db.py
```
This script reads credentials from `/opt/janus/.env`:
*   `AWS_ACCESS_KEY_ID`: AWS user access key.
*   `AWS_SECRET_ACCESS_KEY`: AWS user secret key.
*   `AWS_S3_BUCKET`: The destination S3 bucket.
*   `AWS_DEFAULT_REGION`: The S3 bucket region (defaults to `us-east-1`).

If AWS credentials are valid, backups are uploaded to S3 under the prefix `janus-backups/` and immediately deleted from the local disk to save VM space. If credentials are not present, backups are retained locally in `/opt/janus/backups/`.

### 3. Manual Backup Execution (On-Demand)
To run a backup manually at any time (e.g., before performing system updates or code changes):
```bash
cd /opt/janus && .venv/bin/python scripts/backup_db.py
```

---

### 4. Disaster Recovery & Restore Procedures

In the event of a system failure, database corruption, or when migrating to a new droplet, follow these steps to restore your data from S3.

#### Step A: Stop the Janus Daemon
Always stop the running application before replacing database files to prevent write collisions and lockouts:
```bash
sudo systemctl stop janus
```

#### Step B: Identify and Download the S3 Backup Files
1. Log in to your AWS Console or use the AWS CLI to locate the backups in your bucket under the `janus-backups/` folder.
2. Backups are named with timestamps:
   *   Main DB: `janus_backup_YYYY-MM-DD_HHMMSS.db`
   *   Vector DB: `chromadb_backup_YYYY-MM-DD_HHMMSS.tar.gz`
3. Download the matching pair of files to your server (e.g. into a temporary folder `/tmp/restore/` or directly to `/opt/janus/backups/`).
   Using the AWS CLI:
   ```bash
   aws s3 cp s3://YOUR_S3_BUCKET/janus-backups/janus_backup_2026-06-13_021714.db /opt/janus/backups/janus_backup_restore.db
   aws s3 cp s3://YOUR_S3_BUCKET/janus-backups/chromadb_backup_2026-06-13_021714.tar.gz /opt/janus/backups/chromadb_restore.tar.gz
   ```

#### Step C: Restore the Main Database
1. Move your current active database files (including transient WAL/SHM files) out of the way:
   ```bash
   mv /opt/janus/janus.db /opt/janus/janus.db.corrupted
   rm -f /opt/janus/janus.db-wal /opt/janus/janus.db-shm
   ```
2. Copy the downloaded backup database file to the active location:
   ```bash
   cp /opt/janus/backups/janus_backup_restore.db /opt/janus/janus.db
   ```
3. Apply secure ownership and permission settings:
   ```bash
   chmod 600 /opt/janus/janus.db
   chown root:root /opt/janus/janus.db
   ```

#### Step D: Restore the Chroma Vector Database
1. Move the current active vector database directory out of the way:
   ```bash
   mv /opt/janus/data/chromadb /opt/janus/data/chromadb.corrupted
   ```
2. Create a fresh destination directory and extract the backup archive into it:
   ```bash
   mkdir -p /opt/janus/data/chromadb
   tar -xzf /opt/janus/backups/chromadb_restore.tar.gz -C /opt/janus/data/chromadb/
   ```
3. Recursively restore ownership and permissions:
   ```bash
   chmod -R 700 /opt/janus/data/chromadb
   chown -R root:root /opt/janus/data/chromadb
   ```

#### Step E: Restart the Janus Daemon and Verify
1. Restart the Janus background systemd service:
   ```bash
   sudo systemctl start janus
   ```
2. Monitor the system logs to confirm the databases initialized and loaded successfully without error:
   ```bash
   sudo systemctl status janus
   journalctl -u janus -n 50 --no-pager
   ```
3. Once verification is complete and the application is stable, you can safely remove the temporary backup files and corrupted directories:
   ```bash
   rm -f /opt/janus/backups/janus_backup_restore.db
   rm -f /opt/janus/backups/chromadb_restore.tar.gz
   rm -f /opt/janus/janus.db.corrupted
   rm -rf /opt/janus/data/chromadb.corrupted
   ```


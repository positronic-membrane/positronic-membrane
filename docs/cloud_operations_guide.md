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


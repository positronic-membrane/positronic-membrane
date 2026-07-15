# Restore Runbook: Bare Droplet → Running Instance

Disaster-recovery procedure for issue [#109](https://github.com/positronic-membrane/positronic-membrane/issues/109):
"the mind is not in git." Everything that constitutes a running instance —
constitution, roadmaps, self-model, goals, memory — lives in `janus.db` (plus the
Chroma vector store) on one droplet. This document is the procedure for rebuilding
a running instance from nothing but an off-site backup, starting from a droplet
that has *none* of the code, dependencies, or secrets on it yet.

This is full disaster recovery, not selective state transfer. It is a companion to,
not a substitute for, the issue #100 metadata export (a portable subset of state
for handing off to a *new* instance deliberately). Use this runbook when the goal
is "bring this exact instance back to life"; use #100 when the goal is "start a
new instance with some inherited context."

For day-to-day backup operation (what's backed up, cron schedule, on-demand
manual backup) see `docs/cloud_operations_guide.md` Part 5 — that document is
authoritative for the backup *job*. This document is authoritative for the
restore *procedure*, and goes further than Part 4 of that guide (which assumes
the code and service are already installed on the host being restored to).

## What you need before you start

- SSH access to a fresh droplet (or any bare Ubuntu 24.04 host).
- Read access to the S3 bucket configured as `AWS_S3_BUCKET`.
- The `BACKUP_ENCRYPTION_KEY` Fernet key, from wherever it's stored *outside* the
  backups it protects (password manager / separate secrets store). If backups were
  taken unencrypted (`BACKUP_ENCRYPTION_KEY` unset at backup time), skip decryption.
- The real secret *values* for `.env` (LLM/OpenRouter API keys, `AWS_*` credentials,
  webhook URLs, GitHub PATs, etc.) from your secrets store. The backup's secrets
  inventory (`secrets_inventory_<timestamp>.json`) tells you *which* variables need
  values — it deliberately never contains the values themselves.
- Optionally, the `.keys/jwt_{private,public}.pem` pair, if you want restored
  parties' existing JWTs to remain valid. If you skip this, the app auto-generates
  a fresh keypair on first boot and every previously issued token becomes invalid
  (parties re-authenticate) — acceptable for most disaster-recovery scenarios,
  called out here so it's a choice rather than a surprise.

## Step 1: Base packages

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git build-essential libpq-dev awscli
```

`libpq-dev` is only needed if you're running Postgres mode; harmless to install either way.

## Step 2: Clone the repository

```bash
sudo mkdir -p /opt/janus && sudo chown "$USER":"$USER" /opt/janus
git clone git@github.com:positronic-membrane/positronic-membrane.git /opt/janus
cd /opt/janus
```

Use a deploy key or PAT with read access if this droplet doesn't already have one
registered — see `docs/cloud_operations_guide.md` Part 2, Step 2 for the deploy-key
procedure that was used the first time this was set up.

## Step 3: Bootstrap the environment

```bash
./setup.sh
```

This creates `.venv`, installs the package with dev extras, and copies
`.env.example` to `.env`. Do **not** fill in `.env` yet — do that in Step 4 using
the restored secrets inventory as your checklist.

## Step 4: Download and decrypt the latest backup set

```bash
mkdir -p /tmp/restore && cd /tmp/restore
aws s3 ls s3://YOUR_S3_BUCKET/janus-backups/ | sort | tail -10   # find the latest timestamp
TS=2026-07-15_000000   # substitute the timestamp you picked

aws s3 cp "s3://YOUR_S3_BUCKET/janus-backups/janus_backup_${TS}.db.enc" .
aws s3 cp "s3://YOUR_S3_BUCKET/janus-backups/chromadb_backup_${TS}.tar.gz.enc" .
aws s3 cp "s3://YOUR_S3_BUCKET/janus-backups/secrets_inventory_${TS}.json.enc" .
```

If a given backup run had no `BACKUP_ENCRYPTION_KEY` configured, the objects won't
have a `.enc` suffix — download and use them directly, skip decryption below.

Decrypt (the project uses Fernet, not gpg/age — this is a one-liner, not a
separate tool to install). `BACKUP_ENCRYPTION_KEY` is PBKDF2-derived into the
actual Fernet key (same mechanism as `JANUS_ENCRYPTION_KEY`, a different salt/key
domain) — don't pass it to `Fernet()` directly, use `derive_fernet_key`:

```bash
export BACKUP_ENCRYPTION_KEY="<the key from your secrets store>"
cd /opt/janus && /opt/janus/.venv/bin/python -c "
import os
from cryptography.fernet import Fernet
from src.security import derive_fernet_key
f = Fernet(derive_fernet_key(os.environ['BACKUP_ENCRYPTION_KEY'], b'janus-backup-v1-fernet-salt'))
for name in ['janus_backup_${TS}.db', 'chromadb_backup_${TS}.tar.gz', 'secrets_inventory_${TS}.json']:
    data = open(name + '.enc', 'rb').read()
    open(name, 'wb').write(f.decrypt(data))
"
```

Read the decrypted `secrets_inventory_${TS}.json` now and fill in `/opt/janus/.env`
with real values for every key it lists, sourced from your secrets store.

## Step 5: Verify backup integrity before trusting it

The backup job already ran `PRAGMA integrity_check` before upload, but re-verify
after transfer/decryption — corruption in transit or a decryption mistake is
otherwise silent:

```bash
/opt/janus/.venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('janus_backup_${TS}.db')
print(conn.execute('PRAGMA integrity_check;').fetchone())
"
```

Must print `('ok',)`. Do not proceed with a backup that fails this check — go back
to Step 4 and pull an earlier timestamp instead.

## Step 6: Place the restored data

```bash
cp /tmp/restore/janus_backup_${TS}.db /opt/janus/janus.db
mkdir -p /opt/janus/data/chromadb
tar -xzf /tmp/restore/chromadb_backup_${TS}.tar.gz -C /opt/janus/data/chromadb/
chmod 600 /opt/janus/janus.db
chmod -R 700 /opt/janus/data/chromadb
```

If you're restoring the JWT keypair rather than letting one regenerate:

```bash
mkdir -p /opt/janus/.keys && chmod 700 /opt/janus/.keys
# copy jwt_private.pem / jwt_public.pem from your secrets store here
chmod 600 /opt/janus/.keys/*.pem
```

## Step 7: Create the systemd service

`/etc/systemd/system/janus.service` — runs as `devuser`, not `root` (running as
root previously caused a dubious-ownership git sync failure and contributed to an
OOM on a 2GB droplet; see `docs/cloud_operations_guide.md` Part 2):

```ini
[Unit]
Description=Positronic Membrane (Project Janus)
After=network.target

[Service]
Type=simple
User=devuser
WorkingDirectory=/opt/janus
EnvironmentFile=/opt/janus/.env
ExecStart=/opt/janus/.venv/bin/python -m src.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo cp janus.service /etc/systemd/system/janus.service   # after writing the file above
sudo systemctl daemon-reload
sudo systemctl enable --now janus
```

## Step 8: Verify

```bash
curl -s http://localhost:5005/readyz
# {"status":"ok"}  with HTTP 200 once the DB, vector DB, and daemon heartbeat are all live

curl -s http://localhost:5005/health | python3 -m json.tool
# full diagnostic: database, vector_db, daemon heartbeat freshness, llm_api configured, uptime
```

`/readyz` returns 503 while the daemon heartbeat is still cold-starting — give it
a few seconds (up to the mid-layer cadence × 5, minimum 30s) before treating a 503
as a real failure.

## Step 9: Clean up

```bash
rm -rf /tmp/restore
unset BACKUP_ENCRYPTION_KEY
```

---

## Rehearsal log

An untested restore procedure is a hypothesis, not a capability. This section
records actual rehearsed restores, not just the theoretical procedure above.

| Date | Environment | Backup age | Result | Time to restore | Notes |
|------|-------------|------------|--------|------------------|-------|
| 2026-07-15 | Local scratch dir, isolated from the live DB (not a droplet) | Fresh (same-run backup) | Success | Backup 2.6s + restore/verify 1.4s = ~4s total | First rehearsal — steps 4–6 and 8 exercised directly; see notes and incident below |

**2026-07-15 rehearsal notes:** Ran `scripts/backup_db.py` against this dev
instance's real `janus.db`/`data/chromadb` with a throwaway `BACKUP_ENCRYPTION_KEY`
and AWS explicitly disabled (local-only backup path, written to a scratch
directory, never the live `backups/` dir). Decrypted the resulting `.db.enc` and
`.tar.gz.enc` artifacts with the Fernet key into a second, separate scratch
directory, ran `PRAGMA integrity_check` against both the restored main DB and the
restored `chroma.sqlite3` (both `('ok',)`), extracted the vector archive (46
files), and queried the restored DB directly — 41 tables present,
`system_config`/`core_constitution` row counts matched expectations. This
exercises Steps 4–6 and 8 of the procedure above end-to-end. Steps 1–3 (bare-OS
package install, git clone, `setup.sh`) and Step 7 (systemd unit) were not
re-exercised in this pass — those are standard and already covered by the
original droplet migration log in `docs/cloud_operations_guide.md` Part 2. A real
droplet rehearsal (fresh VPS, full Steps 1–9) is still recommended before relying
on this as the sole disaster-recovery path, and should be re-run periodically
rather than treated as permanently valid.

**Incident during this rehearsal (retained here deliberately, not scrubbed):**
the first backup run in this rehearsal was executed against this environment's
real `.env`, which — unexpectedly — held live AWS credentials for a real off-site
bucket already in production use for nightly backups. The run uploaded 3 new
objects (correct, harmless) but its retention pass, running for the first time
against months of pre-existing backup history in that bucket, immediately pruned
42 older objects to fit the new daily×7/weekly×4 window. The bucket did not have
versioning enabled, so those objects are not recoverable; the operator confirmed
this is not treated as a critical loss. The fix: retention now "arms" itself on
its first run against a given backup location (local dir or S3 prefix) by writing
a marker instead of pruning, and forever after only prunes backups created **at or
after** that marker — see `_get_or_arm_local_retention_marker` /
`_get_or_arm_s3_retention_marker` in `scripts/backup_db.py`. Pre-existing history
can no longer be deleted by this feature, on this or any future first run against
a new bucket/directory. The remainder of the rehearsal (the actual restore
exercise) was subsequently re-run entirely locally, with AWS credentials
explicitly unset, to avoid touching production infrastructure again.

# Scripts

## sync-vault-to-s3.ps1

Syncs the approved Obsidian vault to `s3://mce-second-brain/ami-context/` using
`aws s3 sync --delete`. This keeps the nightly context feed current so Monday's
pipeline always has fresh vault content.

### Prerequisites

1. **AWS CLI** installed and on `PATH`.
   Download: <https://aws.amazon.com/cli/>
2. **AWS credentials** configured with permission to `s3:PutObject`, `s3:DeleteObject`,
   and `s3:ListBucket` on `mce-second-brain`.
   Run `aws configure` or set `AWS_PROFILE` / `AWS_DEFAULT_REGION` as needed.
3. **VAULT_PATH** set — either as a Windows environment variable or in the `.env`
   file at the repo root (see `.env.example`).

### Configuration

The script reads `VAULT_PATH` in this order:

1. Windows environment variable `VAULT_PATH`
2. `VAULT_PATH=...` line in the `.env` file at the repo root

Example `.env` entry:

```
VAULT_PATH=C:\Users\mike\Documents\second-brain
```

Logs are written to `%LOCALAPPDATA%\MagicContentEngine\vault-sync.log`.

### Running manually

```powershell
# From the repo root
.\scripts\sync-vault-to-s3.ps1
```

If PowerShell execution policy blocks the script, run once as Administrator:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

---

## Windows Task Scheduler — Sunday 10pm NZT

Follow these steps to schedule the sync to run automatically every Sunday at
10pm New Zealand Time (Pacific/Auckland). No manual commands are required after
this one-time setup.

### Step-by-step

#### 1. Open Task Scheduler

Press `Win + R`, type `taskschd.msc`, press Enter.

#### 2. Create a new task

In the right-hand **Actions** panel, click **Create Task…** (not "Create Basic Task").

#### 3. General tab

| Field | Value |
|---|---|
| Name | `MCE Vault Sync` |
| Description | `Syncs Obsidian vault to s3://mce-second-brain/ami-context/ every Sunday 10pm NZT` |
| Security options | Select **Run whether user is logged on or not** |
| | Tick **Run with highest privileges** |
| Configure for | Windows 10 / Windows 11 |

Click **OK** and enter your Windows password when prompted (required for
"run when not logged on").

#### 4. Triggers tab

Click **New…**

| Field | Value |
|---|---|
| Begin the task | On a schedule |
| Settings | Weekly |
| Start | Set the date to the next Sunday, time **22:00:00** |
| Recur every | 1 week |
| On | Sunday only |
| Time zone | **(UTC+12:00) Auckland, Wellington** |

> **Why UTC+12?** Windows Task Scheduler uses the system clock. NZT is UTC+12
> in standard time and UTC+13 during daylight saving (NZDT, roughly
> September–April). If your PC clock is set to the Auckland time zone,
> scheduling at 22:00 local time is correct year-round — Windows adjusts
> automatically for DST.
>
> If your PC is set to UTC, schedule at **10:00 UTC** in winter (NZST) and
> **09:00 UTC** in summer (NZDT), or use the Auckland time zone option if
> available in your Windows version.

Click **OK**.

#### 5. Actions tab

Click **New…**

| Field | Value |
|---|---|
| Action | Start a program |
| Program/script | `powershell.exe` |
| Add arguments | `-NonInteractive -ExecutionPolicy RemoteSigned -File "C:\Dev\magic-content-engine\scripts\sync-vault-to-s3.ps1"` |
| Start in | `C:\Dev\magic-content-engine` |

> Adjust the path to match where you cloned the repo.

Click **OK**.

#### 6. Conditions tab

Recommended settings:

- Untick **Start the task only if the computer is on AC power** (laptops may be
  on battery Sunday night).
- Tick **Wake the computer to run this task** if you want it to run even when
  the PC is asleep.

#### 7. Settings tab

| Field | Value |
|---|---|
| Allow task to be run on demand | Ticked |
| If the task fails, restart every | 5 minutes, up to 3 times |
| Stop the task if it runs longer than | 1 hour |
| If the running task does not end when requested, force it to stop | Ticked |

Click **OK**.

#### 8. Verify the task

Right-click **MCE Vault Sync** in the task list and choose **Run** to test it
immediately. Check the log file at:

```
%LOCALAPPDATA%\MagicContentEngine\vault-sync.log
```

A successful run ends with:

```
[INFO] === vault-sync finished (OK) ===
```

---

### Idempotency

`aws s3 sync` compares ETags (MD5 checksums) before uploading. Running the
script twice in a row uploads nothing the second time if the vault has not
changed. The `--delete` flag removes S3 objects that no longer exist locally,
keeping S3 as an exact mirror of the vault.

---

### Troubleshooting

| Symptom | Fix |
|---|---|
| `VAULT_PATH is not set` | Add `VAULT_PATH=C:\path\to\vault` to `.env` or set as a system environment variable |
| `VAULT_PATH does not exist` | Check the path is correct and the drive is mounted |
| `AWS CLI not found` | Install AWS CLI v2 and restart the terminal / Task Scheduler |
| `AccessDenied` from AWS | Check IAM permissions: `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` on `mce-second-brain` |
| Task runs but nothing uploads | Confirm `VAULT_PATH` points to the correct vault directory |
| Script blocked by execution policy | Run `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` as Administrator |

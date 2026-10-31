# Schedule approved clips locally

`zunara jobs` is a small local scheduler for clips you have already reviewed and approved. It keeps
jobs in a private file, `DATA_DIR/scheduler/jobs.json` (see `zunara doctor` for `data_dir`), and
runs due jobs through the existing commands: `publish` (YouTube), `instagram publish|finish` and
`browser publish`. Workspace locks, topic policy, approval hashes and every adapter receipt still
apply. There is no daemon, web UI, paid provider or new account connection.

## Add jobs

Each job names one clip, one destination and its exact identity. `--at` needs a UTC offset.

```sh
zunara jobs add /work/session-01 --only clip-01 --destination youtube \
  --at 2030-01-01T18:00:00-05:00 --channel main --channel-id UC...
zunara jobs add /work/session-01 --only clip-02 --destination instagram \
  --at 2030-01-01T19:00:00-05:00 --account-id IG_ACCOUNT_ID --username YOUR_HANDLE
zunara jobs add /work/session-01 --only clip-03 --destination instagram-browser \
  --at 2030-01-02T19:00:00-05:00 --username YOUR_HANDLE --connection chrome
```

Adding a job is offline. It checks approval, the caption and the topic policy, then freezes the
approved hash, title, description, tags, topic and YouTube privacy. If any of them change, the job
is `blocked` before it contacts a platform; restore the values, or cancel the job and add a new one.
The destination identity cannot be edited. YouTube defaults to `private`; `--privacy public` must be
explicit. `--ai-generated` is accepted only for native Instagram, the only route that sends it.
`--destination tiktok` is refused: native TikTok posting needs an approved TikTok app (see
[native publishing](native-publishing.md)).

Duplicate protection has two parts. First, the jobs file in this data directory allows one active
job per clip (approved hash) and platform account, across the workspaces it lists. Native and
browser Instagram jobs count as the same account when the usernames match (case-insensitive).
Second, each run checks the receipts **in the selected workspace only**. Other workspaces, copies,
older historical folders and other data directories are not indexed globally.

For YouTube, any earlier `publications.json` or `social-publications.json` record for the same
bytes that has not failed skips the job, because provider accounts cannot be mapped to a channel
ID. An unresolved manual record blocks the job until you resolve it. The receipt is never changed.

## Run due jobs

```sh
zunara jobs run                      # offline preview: no tokens, browsers or API calls
zunara jobs run --execute --timezone America/Bogota
zunara jobs list                     # jobs, saved IDs/URLs and setup hints (offline)
zunara jobs cancel JOB_ID            # only pending or blocked jobs
```

`--execute` takes a global scheduler lock, so two runs cannot overlap. It persists `running` before
each remote call and then reads the adapter's own receipt to decide the outcome:

| State | Meaning | Next run |
| --- | --- | --- |
| `pending` | Not due yet, or deferred by the daily cap | Runs when due |
| `blocked` | Provably nothing was sent: setup missing, metadata changed, policy denied | Tried again |
| `waiting` | Native Instagram container is processing | One GET, at most once a minute; publishes when `FINISHED` |
| `running` | A run is in progress | Becomes `interrupted` if that run died |
| `unknown`, `interrupted` | A write may have happened | Never retried; reconcile manually |
| `succeeded`, `failed`, `skipped`, `cancelled` | Final | Never run again |

`succeeded` keeps the real IDs: a YouTube `video_id` with the requested privacy (not proof the video
is visible), the Instagram `container_id`, `media_id` and `permalink`, or the browser permalink. A
processing container is not a post. To resolve `unknown`/`interrupted`, use `zunara instagram status
WORK --refresh`, `zunara browser status WORK` or YouTube Studio. The adapter receipts stay the
source of truth, and adapters still refuse duplicates.

`--max-per-platform-day` (default 2) limits how many distinct clips per platform can **start** a
write in one calendar day of `--timezone` (default UTC). Running, unknown, interrupted, failed and
successful jobs count. Checking a native Instagram container that already exists does not count.
Jobs over the limit stay `pending`. The limit counts only jobs, not manual uploads.

## Periodic runs (disabled by default)

Nothing is installed or enabled for you. To automate, copy an example, review it and enable it
yourself. Use one runner only; never run another scheduler or browser runner alongside it.

```cron
# crontab -e  (commented out on purpose)
# */5 * * * * /path/to/zunara jobs run --execute --timezone America/Bogota >> ~/.local/state/zunara-jobs.log 2>&1
```

```ini
# ~/.config/systemd/user/zunara-jobs.service
[Service]
Type=oneshot
ExecStart=/path/to/zunara jobs run --execute --timezone America/Bogota

# ~/.config/systemd/user/zunara-jobs.timer
[Timer]
OnCalendar=*:0/5
Persistent=false

[Install]
WantedBy=timers.target
```

Enable it only when you are ready: `systemctl --user enable --now zunara-jobs.timer`. Disable it
with `systemctl --user disable --now zunara-jobs.timer`. For the Chrome connection, connect once
with `zunara browser connect`; scheduled runs never prompt and stop as `blocked` when the bridge
is missing.

## Moving from another scheduler

1. Stop and disable the old scheduler or browser runner first. Confirm it is not running.
2. Schedule from the original workspace and leave its receipts in place: `queue.json`,
   `instagram-publications.json`, `browser-publications.json`, `social-publications.json` and
   `publications.json`. Jobs read the receipts of that workspace only, so clips recorded there are
   skipped or resumed, not reposted. A copied or re-rendered workspace does not inherit another
   workspace's history, and Zunara has no import command. Do not schedule from a copy unless you
   have carried every receipt over and checked the platform yourself.
3. Add jobs only for clips you explicitly select. Run `zunara jobs run` to preview, then
   `--execute` once by hand before you enable any timer.

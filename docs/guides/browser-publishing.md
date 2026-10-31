# Unattended browser publishing (Instagram)

`zunara browser` publishes approved clips to Instagram by driving the Instagram website in a
dedicated Chromium profile. It needs no Meta developer app and no publishing subscription. After a
person connects the profile once, the command runs by itself, for example from a timer. No agent
has to click anything.

Only Instagram is implemented. Other platform names are rejected; they are not partially supported.

## What it does and does not do

- It uses Playwright's bundled Chromium with a profile that Zunara creates under
  `CONFIG_DIR/browser-profiles/instagram/USERNAME/`. Directories are owner-only (`0700`). Symlinked
  profile paths, directories Zunara did not create, and locations inside a personal Chrome,
  Chromium, Edge or Brave user-data directory are refused. Your everyday browser profile is never
  read or reused.
- It never reads or exports cookies or tokens and never attaches to a running browser over CDP.
  It adds no stealth or automation-hiding flags, solves no captcha and uses no hidden endpoints.
  It runs no script provided by the page.
- Logging in, two-factor codes, checkpoints and captchas are always done by a person in a visible
  window. When the unattended run meets any of them, it stops.
- It clicks only a control it can identify exactly from its accessible role and name, in English
  or Spanish. If a control is missing or appears more than once, the run stops with `ui_changed`
  instead of guessing.

The Spanish labels for the profile link, create link, post menu item, dialog title and file
button were observed in the live web UI in October 2026. The remaining labels have not been
checked against the live site: crop, Original, Next, caption, Share, the reels notice and the
success text. Instagram changes its interface without notice, so treat the first real run as a
supervised smoke test. Automated tests use local fixture pages, not Instagram.

## Install

```sh
uv sync --locked --no-dev --extra browser
uv run --no-sync playwright install chromium
uv run --no-sync zunara browser doctor
```

`doctor` reports whether Playwright and its Chromium build are installed. It also lists the
profiles that exist. It does not open a browser, so it cannot tell whether a session is still
logged in.

## Connect the profile once

```sh
zunara browser login --platform instagram --username YOUR_HANDLE --timeout 600
```

A visible Chromium window opens on the Instagram login page. Sign in yourself and complete any
verification. The command waits, for at most `--timeout` seconds, until the page's own profile
link shows `YOUR_HANDLE` (`--timeout` is optional; 600 is the default). It then records the verification time and closes the window. If a
different account is logged in, it stops with `account_mismatch`. Each username gets a separate
profile. A profile directory does not prove a live session: every publication run checks again.

Instagram sessions expire, and Instagram may ask for verification again at any time. Repeat
`login` when a run reports `login_required` or `checkpoint_required`.

## Preview and publish

[Review and approve](first-clip.md) the clip and its caption first. The caption comes from the
queue's `description`, or from `title` when there is no description. Approval covers media bytes,
not text. The receipt keeps the caption's SHA-256 so you can audit what was sent.

```sh
zunara browser publish /work/session-01 --platform instagram --username YOUR_HANDLE \
  --only clip-01            # offline preview: no browser, no network
zunara browser publish /work/session-01 --platform instagram --username YOUR_HANDLE \
  --only clip-01 --execute  # opens the browser and publishes
```

Before opening the browser, the preview and execution both check the approved hash, the
caption limits, local media limits and the configured Instagram topic policy. They also check
earlier outcomes recorded by this runner, the native Instagram adapter, Upload-Post and
`publications.json`. Execution then runs these steps:

1. Opens Instagram's home page and stops on a login, checkpoint or captcha page.
2. Reads the handle from the navigation's own profile link and requires `--username`. Opening
   a profile URL is not taken as evidence of the logged-in account.
3. Opens the create dialog, then saves the receipt as `composing`.
4. Attaches a private, re-verified copy of the approved file. The workspace file is never
   attached directly.
5. Chooses the **Original** crop so that vertical video is not cropped square, then advances
   through the edit steps. If it cannot select Original, it stops.
6. Fills the caption and reads it back. It saves the receipt as `sharing`, then clicks the exact
   Share control.
7. Waits up to `--share-timeout` seconds (default 300) for Instagram's confirmation heading in
   the composer dialog; matching words elsewhere on the page, such as in the caption, do not count. It saves a
   permalink only if the confirmation shows one.

Several clips run one after another in the same browser. Before each clip, the runner opens the
home page again and re-checks the account. Use `--headed` to watch a run; the default is headless.

## Results and receipts

`out/browser-publications.json` is a private receipt file in the workspace, keyed by platform,
username and approved hash. Read it with `zunara browser status WORK`.

| State | Meaning | Next run |
| --- | --- | --- |
| `preflight` / `stopped` without `media_selected` | Stopped before media was attached (`login_required`, `checkpoint_required`, `account_mismatch`, `ui_changed`, `browser_unavailable`, `not_attempted`) | Retried normally after you fix the cause |
| `composing` or `stopped` with `media_selected` | Media may have been attached; Share was never clicked | Blocked; check for a draft, then rerun with `--retry-unshared` |
| `sharing` / `unknown` | Share was clicked; success was not seen (timeout, error text, a checkpoint after sharing, or a crash) | Always blocked; never retried |
| `published` | Instagram's success confirmation was seen | Skipped |

A stop exits with code 2 and an actionable message. Raw browser errors are not shown, because they
can contain URLs or page text. To resolve an `unknown` receipt, check the account manually. Then,
while no command is running, back up the receipt file and edit that record yourself. There is no
command that overrides it. Duplicate protection is per workspace. Upload-Post receipts name a
provider account identifier, not a username, so any browser receipt for the same bytes blocks an
Instagram upload through `social publish`.

## Running from a timer

The command is deterministic and suits a timer. Zunara has no daemon or queue worker. Commands
for the same workspace or profile do not overlap: a second run fails quickly with "Workspace is
busy" or "profile is in use". It does not wait. The examples below also prevent overlap at the
scheduler level.

```ini
# ~/.config/systemd/user/zunara-instagram.service
[Unit]
Description=Publish approved Zunara clips to Instagram

[Service]
Type=oneshot
ExecStart=%h/.local/bin/zunara browser publish %h/Videos/session-01 --platform instagram \
  --username YOUR_HANDLE --only clip-01 --execute
TimeoutStartSec=20min
```

```ini
# ~/.config/systemd/user/zunara-instagram.timer
[Timer]
OnCalendar=*-*-* 18:00
Persistent=true

[Install]
WantedBy=timers.target
```

A `oneshot` service does not start a second time while it is still running. With cron, wrap the
command in `flock -n /tmp/zunara-instagram.lock …`. A failed run needs a person: read
`journalctl --user -u zunara-instagram` or `zunara browser status`, run `browser login` again
when asked, and reconcile `unknown` receipts. The timer does not guarantee that a post goes out:
the machine must be awake, Chromium must start, and the session must still be valid.

## Limitations

- The selectors depend on Instagram's web UI and can stop working when it changes. In that case
  the run stops before Share with `ui_changed`.
- Headless runs may look different to Instagram than headed ones. Zunara does not disguise
  automation. If headless runs are challenged, use `--headed` on a desktop session.
- Instagram's terms, rate limits and account restrictions still apply. Publish only media you
  own or have permission to publish.
- The runner publishes feed reels with a caption only. It does not set covers, tags, locations,
  collaborators, AI labels or schedules.

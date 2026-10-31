# Unattended browser publishing (Instagram)

`zunara browser` publishes approved clips to Instagram by driving the Instagram website. It needs
no Meta developer app and no publishing subscription. After a person connects once, the command
runs by itself, for example from a timer. No agent has to click anything.

Only Instagram is implemented. Other platform names are rejected; they are not partially supported.

There are two connections. Choose one with `browser publish --connection`:

| Connection | Browser | Connect once with | Notes |
| --- | --- | --- | --- |
| `dedicated` (default) | Playwright's Chromium with a profile owned by Zunara | `browser login` | Works headless; you log in to that profile separately |
| `chrome` | Your main, already logged-in Chrome, through Microsoft's Playwright Extension | `browser connect` | Chrome must stay running; Linux and macOS only |

Both connections use the same safety checks, receipts and page steps described below.

## What it does and does not do

- In `dedicated` mode, it uses Playwright's bundled Chromium with a profile that Zunara creates under
  `CONFIG_DIR/browser-profiles/instagram/USERNAME/`. Directories are owner-only (`0700`). Symlinked
  profile paths, directories Zunara did not create, and locations inside a personal Chrome,
  Chromium, Edge or Brave user-data directory are refused. Your everyday browser profile is never
  read or reused.
- It never reads, copies or exports cookies, storage or passwords. It never attaches to a browser
  over CDP. `chrome` mode connects only through the official extension, described below.
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

## Or connect your main Chrome

Use this when you prefer to stay logged in to Instagram in your everyday Chrome. Zunara never
asks for or stores your Instagram password: a stored password would not stop Instagram from
asking for a captcha or checkpoint either.

1. Install [Playwright Extension](https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm)
   by Microsoft from the Chrome Web Store, and log in to Instagram in Chrome yourself.
2. Install Node.js so that `npx` is on `PATH`. Zunara runs the public, pinned
   `@playwright/cli@0.1.22` through `npx --yes --package @playwright/cli@0.1.22 playwright-cli`.
   The first run downloads that exact version from npm.
3. Connect and verify the account:

```sh
zunara browser connect --platform instagram --username YOUR_HANDLE --timeout 120
```

`connect` attaches the fixed session `zunara-instagram` to the extension. Approve the connection
in Chrome when the extension asks. Zunara then opens a new tab of its own and reads the handle
from Instagram's profile link. It closes that tab and leaves the connection running.
`zunara browser disconnect` detaches it; Chrome and your tabs keep running.

How the bridge stays on its own tab:

- Each run opens a new tab at a one-time `about:blank#zunara-…` address and claims it only when
  the address matches exactly.
- Every later page operation first checks that claim. Zunara operates on and closes only the
  tab it claimed.
- The CLI's `tab-new` output, which lists your open tabs, is discarded unread.
- After successful CLI commands, Zunara deletes the returned page snapshot files. Transient
  CLI files stay in the private `CONFIG_DIR/browser-bridge/` directory.

The connection does not survive a Chrome restart or an extension disconnect. Unattended runs
never prompt you and never launch a browser. They stop with `connection_required` unless you
configured the extension's bridge token, either in `PLAYWRIGHT_MCP_EXTENSION_TOKEN` or in the
owner-only file `CONFIG_DIR/browser-extension-token`. With the token, a run re-attaches by
itself. The token is a capability for the extension bridge, not an Instagram credential. Zunara
passes it only to the attach subprocess and never prints it. Keep it out of chats and
repositories.

Logins, checkpoints and captchas are resolved by you in your Chrome; afterwards run `connect`
again. Your Instagram session there can still expire. The bridge is not available on Windows,
where `npx` is a command-shell shim. The dedicated mode is unchanged there.

## Preview and publish

[Review and approve](first-clip.md) the clip and its caption first. The caption comes from the
queue's `description`, or from `title` when there is no description. Approval covers media bytes,
not text. The receipt keeps the caption's SHA-256 so you can audit what was sent.

```sh
zunara browser publish /work/session-01 --platform instagram --username YOUR_HANDLE \
  --only clip-01            # offline preview: no browser, no network
zunara browser publish /work/session-01 --platform instagram --username YOUR_HANDLE \
  --only clip-01 --execute  # dedicated profile
zunara browser publish /work/session-01 --platform instagram --username YOUR_HANDLE \
  --only clip-01 --connection chrome --execute  # your main Chrome
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
home page again and re-checks the account. In dedicated mode, use `--headed` to watch a run; the
default is headless. In `chrome` mode, the run happens in a tab of your visible Chrome. Each page
step there is one CLI call, so a run takes a few minutes.

## Results and receipts

`out/browser-publications.json` is a private receipt file in the workspace, keyed by platform,
username and approved hash. Read it with `zunara browser status WORK`.

| State | Meaning | Next run |
| --- | --- | --- |
| `preflight` / `stopped` without `media_selected` | Stopped before media was attached (`login_required`, `checkpoint_required`, `account_mismatch`, `ui_changed`, `browser_unavailable`, `not_attempted`) | Retried normally after you fix the cause |
| `composing` or `stopped` with `media_selected` | Media may have been attached; Share was never clicked | Blocked; check for a draft, then rerun with `--retry-unshared` |
| `sharing` / `unknown` | Share was clicked; success was not seen (timeout, error text, a checkpoint after sharing, or a crash) | Always blocked; never retried |
| `published` | Instagram's success confirmation was seen | Skipped |

Each receipt records its `connection`. A stop exits with code 2 and an actionable message that
names the connection; `chrome` stops point to your main Chrome and `browser connect`, not to
`browser login`. `connection_required` is a stop before anything is attached and can be rerun. Raw browser errors are not shown, because they
can contain URLs or page text. To resolve an `unknown` receipt, check the account manually. Then,
while no command is running, back up the receipt file and edit that record yourself. There is no
command that overrides it. Duplicate protection is per workspace. Upload-Post receipts name a
provider account identifier, not a username, so any browser receipt for the same bytes blocks an
Instagram upload through `social publish`.

## Running from a timer

The command is deterministic and suits a timer. Zunara has no daemon or queue worker. Commands
for the same workspace, profile or Chrome bridge do not overlap: a second run fails quickly with
"Workspace is busy", "profile is in use" or "Chrome bridge is in use". It does not wait. The examples below also prevent overlap at the
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

- `chrome` mode needs Chrome to keep running with the extension connected. A timer cannot
  re-approve a connection; without the bridge token it stops until you run `connect` again.

- The selectors depend on Instagram's web UI and can stop working when it changes. In that case
  the run stops before Share with `ui_changed`.
- Headless runs may look different to Instagram than headed ones. Zunara does not disguise
  automation. If headless runs are challenged, use `--headed` on a desktop session.
- Instagram's terms, rate limits and account restrictions still apply. Publish only media you
  own or have permission to publish.
- The runner publishes feed reels with a caption only. It does not set covers, tags, locations,
  collaborators, AI labels or schedules.

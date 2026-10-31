# YouTube uploads

Use this after [reviewing and approving a local clip](first-clip.md). This adapter talks to YouTube;
exports for the other platforms are local files only. Keep rendering and YouTube extras installed:

```sh
uv sync --locked --no-dev --extra asr --extra youtube
```

## Authorize a channel

1. Create a YouTube channel and a Google Cloud project. Enable YouTube Data API v3.
2. Configure the OAuth consent setup and create a **Desktop app** client. Add your account as a
   test user if the project is in testing. Follow Google's [desktop OAuth
guide](https://developers.google.com/youtube/data/guides/auth/installed-apps)
   for the current console steps.
3. Download the client JSON outside the checkout. Point Zunara to it and authorize:

```sh
export ZUNARA_YOUTUBE_CLIENT_SECRET="$HOME/.config/zunara/client_secret.json"
uv run --no-sync zunara auth --channel personal
```

`personal` is a local alias, not a Google account name. OAuth uses your browser and a random
localhost callback port; this flow needs access to a local desktop browser. Requested scopes are
`youtube.upload` and `youtube.readonly`. Verify the returned `id` and `title` before proceeding.
Tokens are private JSON at `CONFIG_DIR/tokens/personal.json`; never paste them into a prompt or
repo. A valid saved token can be reused without opening a browser. To select another account, use a
new alias and authorize it, or back up and remove only the old alias token while commands are idle.

## Preview, then execute

```sh
uv run --no-sync zunara publish "$HOME/Videos/first-session" --only first-clip \
  --channel personal --channel-id UC_YOUR_CHANNEL_ID
```

This is an **online preflight without an upload**, not an offline dry run. It opens/refreshes the
saved credentials, reads the authenticated channel and checks selected media. A channel mismatch
refuses the batch. Its JSON plan contains `file`, `channel_id`, `privacy` and `scheduled_at`. Watch
the video and review its metadata, then explicitly execute the approved selection:

```sh
uv run --no-sync zunara publish "$HOME/Videos/first-session" --only first-clip \
  --channel personal --channel-id UC_YOUR_CHANNEL_ID --privacy private --execute
uv run --no-sync zunara status "$HOME/Videos/first-session"
```

Execution returns the same plan shape. The resulting `youtube_id`, `channel_id`, `uploaded_at` and
state are stored in the queue. A successful API response confirms an upload ID, not completed
processing or visible public playback. Check the actual result in YouTube Studio.

## Current metadata limits

The adapter sends queue title, description and tags. It also sets category `24` (Entertainment),
`defaultLanguage: "es"` and `selfDeclaredMadeForKids: false`. There are no CLI overrides for those
three values. Check whether those declarations fit your video before using this publisher; an
English-captioned MP4 does not change the language metadata. Custom queue fields are not read as
upload options. Destination eligibility and API restrictions are separate from installation; consult
Google's [upload
reference](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert).

## Schedule a batch

Add `--schedule 2030-01-01T12:00:00-05:00 --every-hours 3` to the preview and execution commands.
Use an actual future datetime with an explicit offset. The first pending clip gets that time;
subsequent clips get the interval in queue order. The interval must be finite and positive. A
scheduled upload is sent as private with `publishAt`, regardless of `--privacy`. `scheduled_at`
records the requested time; it is not a confirmation that publication happened. Zunara has no local
scheduling daemon. Once YouTube accepts the schedule, the platform handles its release.

## Recover an interrupted upload

Do not retry blindly: the server may have accepted a video before the response was lost. Stop
workspace commands, back up `out/queue.json`, then check the exact channel in Studio.

- If the video exists, set its row's `youtube_id` to the confirmed ID and `upload_state` to
  `"uploaded"`. Preserve known channel/timestamps; do not invent an upload completion time.
- If you have confirmed no video was accepted, leave `youtube_id` null and reset `upload_state`
  to `"pending"`. Preview the selection again before a deliberate retry.
- If the outcome is still uncertain, leave it `"uploading"` and resolve it in Studio first.

A pending batch containing an `uploading` row is refused. Rows already containing an ID are skipped.
In a partially completed batch, earlier successes stay uploaded; later rows may be pending. Do not
clear existing IDs to force another upload. Use a new media filename for a revised video.

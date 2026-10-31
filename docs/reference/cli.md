# Command reference

Run `uv run --no-sync zunara COMMAND ...` from a synced checkout, or use `zunara` in its active
virtual environment. From elsewhere, add `uv run --project /path/to/zunara-studio --no-sync`.
Arguments containing spaces need shell quotes. The installed command is the supported interface;
internal Python modules are not a versioned SDK.

## Output and errors

Successful operations print one JSON document to stdout. Analysis progress and render timings go to
stderr. `--help` and `--version` print text. Exit code `0` means success; argument, dependency,
media and remote-operation failures handled by the CLI exit `2`. Read both the exit status and
stderr. Value errors describe the failed check; other exceptions are shortened to avoid dumping SDK
payloads. External-process diagnostics may still contain paths or URLs; keep raw logs private.

```sh
uv run --no-sync zunara inspect /path/to/video.mp4 > result.json 2> diagnostics.log
```

Names used for render output, token aliases and workspace export folders must match
`[A-Za-z0-9][A-Za-z0-9_-]{0,79}`. `--only` takes comma-separated filename stems, without `.mp4`.
Omitting it selects every queue entry where supported. An empty selection string is an error;
unknown stems are errors. Selection follows queue order, not the order written in `--only`.

## Local inspection

| Command | Result | Effects and requirements |
| --- | --- | --- |
| `--version` | Package version as text | No model or account access |
| `doctor` | Object with `version`, `tools`, `config_dir`, `data_dir`, `youtube_client_configured` | Checks executable/file presence; no OAuth or model load |
| `inspect FILE` | Object shown below | Runs ffprobe; needs a readable video stream |

```json
{"duration_s": 15.0, "width": 1280, "height": 720, "codec": "h264", "has_audio": true}
```

Inspection reports media properties, not caption accuracy, rights or platform eligibility.
`doctor.tools` contains booleans for `ffmpeg`, `ffprobe` and `yt-dlp`; the latter is optional for
local rendering. The client-configured flag only checks whether the desktop OAuth JSON exists.

## Render

```text
render WORK START END --hook TEXT --name NAME [--layout {4:3,full}] [--source PATH]
```

| Argument | Meaning and default |
| --- | --- |
| `WORK` | Workspace; creates `out/` when needed |
| `START`, `END` | Finite seconds, `0 <= START < END`; file-relative with `--source`, VOD-relative otherwise |
| `--hook TEXT` | Required title, rendered uppercase; does not translate speech |
| `--name NAME` | Required output stem; produces `out/NAME.mp4` and `out/NAME.ass` |
| `--layout` | Default `4:3` center crop; `full` preserves the wide foreground |
| `--source PATH` | Local input with audio and video, covering the interval; omitted means remote VOD mode |

Result: `{"file": "/absolute/work/out/clip.mp4"}`. Needs FFmpeg, the `asr` extra and model weights.
Local-source mode preserves the input and refuses existing MP4/ASS output names. It stages video
before exclusive placement and needs hard links on the output filesystem. Remote mode needs
`WORK/audio16k.wav` and `WORK/base_url.txt`, streams the VOD video, and can overwrite output names.
See [rendering](../guides/rendering.md) for layout, encoding and caption limits.

## Queue and export

| Command | Result | State changes |
| --- | --- | --- |
| `queue WORK` | Entire queue array | Adds discovered `.mp4` files, re-hashes media, resets changed pending approvals, saves JSON |
| `status WORK` | Entire queue array | Reads persisted state; does not refresh hashes |
| `approve WORK --only STEMS` | Selected queue records | Refreshes first, then records approved media hashes; requires nonempty titles |
| `export WORK --platform PLATFORM [--only STEMS] [--destination PATH]` | `{"exported": N}` | Verifies selected media and creates a new local batch; no account access |

`WORK/out/` must already be a real directory for `queue`, `approve`, `export` and `publish`.
`status` can read an absent queue as an empty array. Queue filenames are plain `.mp4` names inside
it. Refresh does not remove stale records: a missing file fails validation. Approval does not freeze
metadata. Export rechecks current media hashes and enforces titles of 1–100 characters and
descriptions up to 5000 characters, even for a non-YouTube destination.

Platforms are `tiktok`, `instagram`, `x`, `youtube`. Destination defaults to `DATA_DIR/ready`; the
batch is `DESTINATION/PLATFORM/WORK_BASENAME/`. Its parent may be created, but an existing batch is
refused. Each video gets a `.txt` containing title and description; tags are not appended.
`manifest.json` contains filenames and approved hashes. There is no direct TikTok/Instagram/X post.
See [workspace reference](workspace.md) for record fields, policy and locking.

## YouTube

```text
auth --channel ALIAS
publish WORK --channel ALIAS --channel-id ID [--only STEMS]
        [--privacy {private,unlisted,public}] [--schedule ISO_DATETIME]
        [--every-hours HOURS] [--execute]
```

| Option | Meaning and default |
| --- | --- |
| `--channel` | Required local token alias; not the public channel title |
| `--channel-id` | Required exact ID returned by `auth`; a mismatch refuses all uploads |
| `--privacy` | Default `private`; a schedule overrides this to private plus `publishAt` |
| `--schedule` | Future ISO datetime with an explicit UTC offset; absent means no scheduling |
| `--every-hours` | Finite positive number, default `3`; spaces scheduled clips in queue order |
| `--execute` | Performs uploads; absent returns an online preflight plan |

`auth` opens desktop browser OAuth when needed, saves a token and returns `{"id": "UC...", "title":
"Channel title"}`. A valid saved token may avoid the browser. Both authorization and preview can
contact Google and write refreshed credentials.

Publish returns an array of plans, in preview and execution mode:

```json
[{"file": "clip.mp4", "channel_id": "UC...", "privacy": "private", "scheduled_at": null}]
```

It skips selected rows already containing `youtube_id`. An `uploading` row stops the pending batch
until reconciled. Execution writes IDs and timestamps to the queue; read them with `status`. It does
not return IDs in the plan. A later failure can leave earlier clips uploaded. Category `24`,
`defaultLanguage: "es"` and `selfDeclaredMadeForKids: false` are currently fixed in the adapter;
there are no CLI overrides. See [YouTube](../guides/youtube.md) before using it.

## Acquisition and analysis

```text
fetch CHANNEL [--index N] [--workspace ROOT] [--youtube-query TERMS]
match VOD_AUDIO MATCHES_JSON QUERY_AUDIO [QUERY_AUDIO ...]
rank WORK [TOP_N]
```

Repeat `--youtube-query` for separate search terms; its value is one quoted argument. `--index` is a
nonnegative integer, default `0`. `--workspace` defaults to `DATA_DIR/work`. Fetch creates
`ROOT/CHANNEL_YYYYMMDD/`, not the root itself. It downloads audio, Spanish subtitles and chat/clip
signals; it does not save a full source MP4. It needs `yt-dlp` (`downloads` extra), FFmpeg and
network access. Endpoint access and available renditions can change independently of Zunara.

Match needs `analysis`, a VOD audio track, an output JSON path with an existing parent and one or
more query files. Use 16 kHz mono WAVs. Query stems become JSON keys, so use distinct stems. Rank
defaults to the top `15` candidates and requires a positive count. It reads fixed filenames from
`WORK` and replaces `moments.json`; it does not fetch missing signals or transcribe speech.

The JSON result is `{"stage": "fetch", "status": "complete"}` (or `match` / `rank`). Actual analysis
records are written into workspace files. Stage progress goes to stderr. Use `fetch --help`, `match
--help` or `rank --help` for real arguments without importing optional dependencies. The [rendering
guide](../guides/rendering.md) connects these stages using the same workspace.

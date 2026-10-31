# Workspace and configuration

A workspace is a user-owned folder outside the checkout. Use a name such as `session-01`: its
basename becomes an export folder and must satisfy the [CLI name rule](cli.md#output-and-errors).
Keep the original source separately and make each render revision a new filename.

## Files and ownership

```text
session-01/
  out/
    clip-01.mp4          # rendered video, or a finished video copied here
    clip-01.ass          # optional render sidecar; captions are already burned into MP4
    queue.json          # metadata, media hashes and upload state
    .zunara.lock        # queue/export/upload coordination
```

Queue discovery only scans immediate lowercase `out/*.mp4` files, not subdirectories. `out` cannot
be a symlink. Queue media paths must resolve inside it; external symlink targets are refused. Local
`render --source` may read a source elsewhere and needs no analysis files in the workspace.

The optional VOD pipeline adds these inputs and outputs:

| File or directory | Writer → reader |
| --- | --- |
| `meta.json`, `base_url.txt` | Fetch → operator / remote renderer; URL may expire and should stay private |
| `audio16k.wav` | Fetch → matching, ranking and remote-mode ASR |
| `subs_es.vtt`, `chat_samples.json` | Fetch → ranking |
| `kick_clips/*.wav`, `kick_clips/vod_clips.json` | Fetch → matching / operator |
| `ext_audio/*.wav`, `yt_ids.txt` | Optional fetch search → matching / operator |
| `matches.json` | Match, when given this output path → ranking |
| `moments.json` | Rank → operator selecting an interval |

`moments.json` is an array containing `peak_s`, `start_s`, `end_s`, `score`, `picks`, `sources`,
`chat_per_min`, `loud_db` and `transcript`. It proposes intervals around signal peaks. A score is
relative to that VOD's signals, not a probability of success or a cross-stream quality rating.

## Queue fields

`out/queue.json` is an array of objects. Refresh creates the fields shown below; `topic` and upload
timestamps are added when needed. Unknown fields are retained, but are not automatically used by a
publisher.

```json
[{
  "file": "clip-01.mp4",
  "title": "An unexpected answer",
  "description": "Source and context for this excerpt.",
  "tags": ["interview"],
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "approved_sha256": null,
  "youtube_id": null,
  "upload_state": "pending"
}]
```

The digest above illustrates the format; refresh computes the real value. Never copy an example hash
into an approval. Edit text fields in your editor while no workspace command is running.

| Field | Type and meaning |
| --- | --- |
| `file` | Plain `.mp4` filename; required, unique, present inside `out/` |
| `title` | String; must be nonblank to approve, 1–100 characters to export/upload |
| `description` | String; at most 5000 characters to export/upload |
| `tags` | Array of strings; sent to YouTube, not appended to exported captions |
| `topic` | Optional label; checked when the destination policy restricts topics |
| `sha256` | Lowercase 64-character digest or null; refreshed by `queue` |
| `approved_sha256` | Reviewed media digest or null; written by `approve` |
| `youtube_id` | Nonempty platform ID or null; rows with IDs are skipped by publish |
| `upload_state` | `pending`, `uploading` or `uploaded`; missing means pending |
| `channel_id` | Exact channel ID recorded when execution starts |
| `uploaded_at` | ISO UTC upload-completion timestamp, written after an ID is received |
| `scheduled_at` | Requested publication datetime with offset, or null; not proof of publication |

`uploaded` requires a video ID. `uploading` means the remote outcome may be unknown, not that a
process is still running. Missing optional timestamps are normal for older or unuploaded entries.

## Review lifecycle

`status` reads state without re-hashing. `queue` discovers files and recalculates hashes; it clears
approval if a pending video's bytes changed. `approve` refreshes first, then approves the named
videos. Export and upload independently re-hash before using media and verify their temporary copy.
If an uploaded or in-flight video changes, refresh refuses it: use a new filename for a revision.

Approval covers media bytes only. A changed title, description, tags or topic does not clear the
hash approval. Review that metadata before every upload. A local hash also does not record another
person's consent; agent workflows need their own explicit task authorization.

File locks coordinate cooperating queue/export/upload writers with a 10-second timeout. They do not
lock an editor, other application or the source video. JSON uses atomic replacement; uploads are not
a transaction with the remote server. Keep one writer per workspace. See [upload
recovery](../guides/youtube.md#recover-an-interrupted-upload) before changing upload state. To
remove a missing-file record, back up the queue and remove only that stale entry while idle. There
is no prune command. Keep the backup outside `out/` if it contains obsolete operational state.

## Config and data paths

`zunara doctor` prints the resolved paths. On a typical Linux setup, config is `~/.config/zunara`
and data is `~/.local/share/zunara`; use the printed values on other systems or customized setups.

| Environment variable | Default / role |
| --- | --- |
| `ZUNARA_CONFIG_DIR` | Platform user config directory for `zunara` |
| `ZUNARA_DATA_DIR` | Platform user data directory for `zunara`; default export and fetch roots |
| `ZUNARA_YOUTUBE_CLIENT_SECRET` | `CONFIG_DIR/client_secret.json`; desktop OAuth client file |

Paths expand `~` and resolve to absolute paths. The CLI reads process environment variables and does
not load `.env`. The ASR package manages its own model cache; `ZUNARA_DATA_DIR` does not relocate
it. Disk use also includes temporary audio/video copies during rendering and export/upload. Token
files are `CONFIG_DIR/tokens/ALIAS.json`; they and their directory must not be symlinks. Private
JSON writes use owner-only file permissions and a private parent directory on POSIX.

Export also leaves a coordination file `DESTINATION/PLATFORM/.WORK_BASENAME.lock` alongside the
batch. It is not media and is not part of the manifest.

## Topic policy

Save `publishing-policy.json` in the config directory, not inside a workspace:

```json
{"platforms": {"x": {"allowed_topics": ["programming"]}}}
```

Allowed platform keys are `x`, `youtube`, `instagram`, `tiktok`. Topics match
`[a-z0-9][a-z0-9_-]{0,63}`; each configured platform needs a nonempty list. Unknown keys and
malformed files are errors. No file, an empty `platforms` object or an omitted platform means
unrestricted. A restricted destination refuses a missing/disallowed topic. Export checks every
selected row; publish checks selected rows without a `youtube_id`. This checks labels, not the
meaning of a video. The CLI checks before export writes a batch or publish opens a YouTube client.
Direct Python calls to `queue.export` and `youtube.upload` do not load policy automatically; library
callers must apply `editorial.load_policy(...).check(...)`.

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
    queue.json          # metadata, media hashes and direct YouTube upload state
    social-publications.json  # optional multi-platform API receipts
    browser-publications.json # optional unattended-browser receipts
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
`social publish` checks the selected destinations that still need submission before loading its key
or making provider requests. Direct calls to the adapter also need that policy check at the caller.
Direct Python calls to `queue.export` and `youtube.upload` do not load policy automatically; library
callers must apply `editorial.load_policy(...).check(...)`.

## Multi-platform API state

The optional `social` adapter stores private receipts in `out/social-publications.json`, using the
same workspace lock. Each submission associates the approved media hash with the provider profile,
destination platform, expected connected account and remote request/job identifiers. These records
are separate from `queue.json` and must be preserved when moving a workspace. Submission markers
protect against an interrupted request being silently sent again. The guard is local to that
workspace; it does not discover copies in another workspace or posts created in another tool.

The API key comes from `UPLOAD_POST_API_KEY`, then `CONFIG_DIR/upload-post-api-key`. It is never a
queue field or receipt field. See [API publishing](../guides/social-api.md) before handling an
unresolved submission or configuring a provider account.

The receipt envelope has `version: 1` and a `records` array. Each record has:

| Field | Meaning |
| --- | --- |
| `profile`, `platform`, `approved_sha256` | Per-workspace duplicate key: provider profile, destination and approved media bytes |
| `file`, `account` | Source filename and the exact connected identifier selected at submission |
| `state` | `submitting`, `unknown`, `accepted`, `scheduled`, `completed` or `failed` |
| `request_id`, `job_id` | Client/provider request or scheduling identifiers, used for polling |
| `idempotency_key` | Unique submission key sent to the provider to suppress a repeated request |
| `platform_post_id`, `post_url` | Optional provider-confirmed result; not proof of public playback |
| `scheduled_at` | Requested schedule with a UTC offset, or null for an immediate submission |
| `privacy`, `language` | Requested YouTube privacy and optional language metadata |
| `delivery` | When present, identifies an inbox draft rather than a completed post |
| `created_at`, `updated_at` | Local UTC receipt timestamps; not inferred publication dates |

An absent result field means the provider has not returned that result. `completed` means a
per-platform success with an ID or URL; `accepted` can still await processing or moderation.
The client generates a request ID before submission and also sends it as the provider's idempotency
key. Status refresh can query that ID after an interrupted response, including unresolved records.
A definite request rejection may release its submission marker so configuration can be corrected;
an uncertain transport outcome remains guarded. Preserve failed and unresolved receipts until the
actual remote outcome has been reconciled.

## Native Instagram state

`out/instagram-publications.json` is a private version-1 receipt envelope with a `records` array.
The duplicate key is account ID + approved media SHA-256, scoped to the workspace. Each record
saves the filename, account username, Graph version, container/media identifiers, returned
permalink, processing code, creation fields and local UTC timestamps. Null identifiers or URLs
mean they have not been returned; they are never inferred from a filename.

Creation/transfer/publication markers are persisted before their remote writes. A known container
can be queried after interruption. A lost creation response with no container ID remains unresolved;
do not remove its receipt or change workspaces merely to force a second upload. `published` has
a returned media ID; `api_published` means Meta reported `PUBLISHED` for the container after a lost
response, but the media ID/permalink is still unknown. Preserve both outcomes when moving or
rolling back a workspace. See [native publishing](../guides/native-publishing.md).

## Browser publishing state

`out/browser-publications.json` is a private version-1 envelope with a `records` array. The key is
platform + username + approved media SHA-256, scoped to the workspace. Records hold the filename,
caption SHA-256, `state` (`preflight`, `stopped`, `composing`, `sharing`, `unknown`, `published`),
the stop `status`, the `media_selected` and `share_clicked` flags, an observed permalink or null,
and UTC timestamps. `composing` is saved before media is attached and `sharing` before Share is
clicked, so a crash never causes a silent second post. See [browser
publishing](../guides/browser-publishing.md#results-and-receipts).

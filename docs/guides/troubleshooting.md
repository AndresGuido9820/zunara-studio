# Troubleshooting

The CLI reports `zunara: <reason>` and exits with status 2. Usage or progress lines may precede it.
Some FFmpeg, model and remote-service failures are shortened to a generic sentence to reduce
disclosure; others include a specific recovery step. External-process diagnostics can still contain paths or URLs; keep raw logs private.

## Generic messages

**`Operation failed; check input files, dependencies and access.`** This covers a missing or
unreadable file, a failed FFmpeg run, a missing Python package, or a network error such as a failed
Kick request or the first model download. Check the following, in this order:

1. The paths exist and are readable and writable. `rank` needs `audio16k.wav`, `chat_samples.json`,
   `subs_es.vtt` and `matches.json` in the workspace.
2. The extra for the command is installed (see below).
3. FFmpeg has the `ass` filter and the `libx264` encoder (see [installation](installation.md)).
4. For the first render, Hugging Face must be reachable for the model download.

**`Remote or model operation failed; check access and dependencies.`** This covers everything else.
The usual causes are a YouTube API error (quota, revoked access, a rejected video), a speech model
error, or a `fetch --index` past the end of the channel's VOD list. For YouTube, check the channel
in YouTube Studio and run `zunara auth` again.

A missing rank input or remote-render input reports `Missing workspace input(s): ...` before loading
the stage. Restore the named file or use the correct workspace. An unreadable/corrupt WAV can still
produce either generic message; verify it locally with FFmpeg.

**`Speech model weights are not cached. ...`** If `HF_HUB_OFFLINE=1` is set with an empty cache,
unset it for the first download, or provide a populated cache. `HF_HOME` changes the model cache; a
model cached under another location is not automatically available there. Retry a short local render
after checking network access and disk space. Never paste a raw signed download URL into a public
issue.

## Missing extras

| Symptom | Install |
| --- | --- |
| `Rendering needs the asr extra …` | `--extra asr` |
| `Install the YouTube extra …` from `auth` or `publish` | `--extra youtube` |
| `match` fails with "Operation failed" | `--extra analysis` |
| `yt-dlp is required …` from `fetch` | `--extra downloads`, or a system `yt-dlp` |

## Rendering

| Message | Fix |
| --- | --- |
| `Render interval ends after the source (N s); …` | Keep `END` within the duration `inspect` reports. |
| `Render interval must be finite and satisfy 0 <= start < end.` | Check the order of the two times. |
| `argument start: invalid float value: '1:05'` | Times are seconds: `65`. |
| `Source has no audio stream; …` / `Source is not a readable video file.` | Use a file with audio; check it with `zunara inspect`. |
| `Output NAME already exists; choose another --name.` | Pick a new name. `--source` mode never overwrites. |
| `Could not place the clip: … hard links …` | Move the workspace off FAT/exFAT. |
| `Use 1–80 letters, digits, underscores or hyphens for names.` | Applies to `--name`, `--channel`, the `fetch` channel and, in `export`, the workspace folder. |

If the captions use the wrong font, libass couldn't find Anton; install it and check with `fc-list`.
Wrong words usually mean an unsupported language or music over the speech; see
[rendering](rendering.md#captions-and-language).

## Queue, approval and export

| Message | Fix |
| --- | --- |
| `Workspace must contain a real out directory, not a symlink.` | Create `WORK/out`. A symlinked `out` is refused. |
| `Set a title before approval: FILE` | Fill `title` in `out/queue.json`. |
| `Approval requires --only with the clips you have reviewed.` | Name them: `--only a,b`. |
| `Unknown clip names: …` | Use names without `.mp4`, and run `zunara queue` after rendering. |
| `Clip needs review/approval (or changed after approval): FILE` | Watch the clip, then `approve --only` it. |
| `Title must contain 1–100 characters.` / `Description exceeds 5000 characters.` | Shorten the text. Text edits don't reset approval. |
| `Missing media: FILE` | Each command checks every entry. Restore the file, or back up `queue.json` and remove the entry while nothing runs. |
| `Previously uploaded/in-flight media changed; use a new filename.` | Restore the original, or render the new version under another name. |
| `Export destination already exists; choose a fresh destination.` | Use a new `--destination`. Batches are never merged. |
| `Topic policy for PLATFORM allows only …` | Give each listed clip an allowed `topic`, or narrow `--only`. |
| `Cannot read publishing-policy.json …` / `Unknown platform in publishing policy …` | Fix the file in `CONFIG_DIR`. Exports and uploads stay blocked until then. |

## YouTube

| Message | Fix |
| --- | --- |
| `Missing desktop OAuth client JSON; …` | Set `ZUNARA_YOUTUBE_CLIENT_SECRET` to the client file. |
| `Authorize this channel first: zunara auth --channel ALIAS` | Run `auth`. Previews need a token too. |
| `Authenticated channel does not match --channel-id; nothing uploaded.` | Use the ID `auth` printed, or authorize the right account. |
| `An earlier upload has an unknown outcome. …` | Reconcile it as the [YouTube guide](youtube.md#recover-an-interrupted-upload) describes. |
| `Schedule needs a timezone, …` / `Schedule must be in the future.` | Use a future time with an offset: `2030-01-01T12:00:00-05:00`. |

When reporting a problem, include the command, the `zunara:` line, `zunara doctor` output and your
FFmpeg version, but no tokens, secrets, `base_url.txt`, chat samples or private media.

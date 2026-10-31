# Rendering

`zunara render` cuts one interval of a long video into a 1080×1920 clip. A title sits on top and
word-by-word captions sit below. Render transcribes only that interval, then encodes the clip in one
FFmpeg pass.

```text
zunara render WORK START END --hook TEXT --name NAME [--layout 4:3|full] [--source PATH]
```

## Two modes

**Local source (`--source PATH`).** The usual mode: it cuts from a video file on your disk, and the
workspace needs no other files. The source is never modified. Before loading the model or writing
anything, render checks three things: the source has video and audio, `END` is within its duration,
and `out/NAME.mp4` and `out/NAME.ass` don't exist yet. The video is encoded in a hidden folder
inside `out/` and then hard-linked into place. If another file took the name in the meantime, render
stops instead of replacing it. Because of the hard link, the workspace must be on a filesystem that
supports hard links; FAT and exFAT don't.

**Fetched Kick VOD (no `--source`).** This mode cuts from a workspace prepared by `zunara fetch`. It
transcribes `WORK/audio16k.wav` and streams the video over the network from the 1080p60 rendition
listed in `WORK/base_url.txt`. It needs Internet access, and it fails if the VOD is gone or has no
1080p60 rendition. It also **overwrites** an existing `out/NAME.mp4` and `out/NAME.ass` without
asking.

## Timelines

`START` and `END` are plain seconds, such as `754.5`, and the clip runs from `START` to `END`.

- With `--source`, times count from the start of that file. If the file is part of a longer
  stream, for example a download that begins 1:30:00 into the broadcast, subtract that offset
  from times you noted in the full stream.
- Without `--source`, times count from the start of the Kick VOD. That is the timeline of
  `moments.json`, so a moment's `start_s` and `end_s` can be used directly.
- Caption times inside the `.ass` file start at zero, the beginning of the clip.

## Layout and output

The frame has three layers. The background is the whole source frame, blurred and darkened. In the
middle is a foreground band: `--layout 4:3` (the default) crops the center 4:3 of the frame, which
suits one wide camera, and `--layout full` keeps the whole 16:9 frame for a webcam overlay in a
corner. On top are the captions. The `--hook` text appears in capitals at the top for the whole
clip, and the speech appears below in capitals, three words per line, with the current word in
yellow. Both use Anton. The 4:3 crop expects a landscape source and is likely to fail on a vertical
one.

Output is H.264 at 30 fps with AAC at 160 kb/s, with a −14 LUFS loudness target. VAAPI is used when
a test encode on `/dev/dri/renderD128` succeeds, libx264 otherwise. The encoders use different
quality settings. A later VAAPI encode failure does not trigger an automatic software retry. AAC audio is resampled to
48 kHz so loudness normalization does not leave a 96/192 kHz output unsuitable for Reels.

## Captions and language

Captions come from Parakeet TDT 0.6B v3. It transcribes speech in the language it recognizes; it
does not translate. English and Spanish can be used without a translation step. Check recognition on
your own source, especially names, numbers, music and overlapping voices. The hook is used exactly
as you typed it, in capitals.

There is no caption editor. To fix a word, edit `out/NAME.ass` and encode a new MP4 from the
original source with the corrected ASS, or render again under a new name. Editing the sidecar alone
cannot change burned-in captions; burning it over the existing MP4 would stack a second set of
captions. Review and approve the new video. Kick's `subs_es.vtt` from `fetch` only feeds ranking
transcripts, never rendered captions.

## What Zunara adds over FFmpeg

FFmpeg does the cutting, blurring, subtitle burning and encoding. Zunara adds word-timed captions
from a local speech model, the caption/layout pipeline and a loudness target, no-overwrite source
checks, and then the review queue, hash-bound approval and export manifest. If you only need a cut
and crop without captions or review, plain FFmpeg is simpler. See [where it adds
value](../README.md#where-it-adds-value).

## Fetched-stream pipeline (optional)

`fetch` creates `WORKSPACE/<channel>_<yyyymmdd>/` and prints its path to standard error. Use that
folder as `WORK` from then on:

```sh
uv run --no-sync zunara fetch example-channel --workspace ~/Videos/kick
WORK=/path/to/the/channel_date_directory_printed_by_fetch
uv run --no-sync zunara match "$WORK/audio16k.wav" "$WORK/matches.json" \
  /path/to/an/actual/query.wav
uv run --no-sync zunara rank "$WORK" 10
uv run --no-sync zunara render "$WORK" 3412 3482 --hook 'Text' --name moment-1
```

`fetch` needs `yt-dlp` and relies on Kick website endpoints that can change without notice. `match`
needs the `analysis` extra. `rank` writes `WORK/moments.json`; each moment's `start_s` and `end_s`
span about 70 seconds around a peak (fields in the [workspace
reference](../reference/workspace.md)). Ranking only suggests candidates. It doesn't judge whether a
moment is accurate, fair to the people in it, or yours to republish.

Use actual 16 kHz mono query WAVs from `kick_clips/` or `ext_audio/`. An unmatched shell glob is a
literal nonexistent filename, not an empty input list. If there are no query clips, write `{}` to
`matches.json` and rank with chat/loudness alone. Ranking still needs all its other input files.

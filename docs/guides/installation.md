# Installation

Zunara Studio is a command-line tool. There is no desktop app, web interface, background service or
scheduler. Every action happens when you run a `zunara` command, and successful operations print
JSON to standard output. Help and version output are text.

## Requirements

- Python 3.11 or newer. CI tests 3.11–3.13 on Linux only.
- FFmpeg and `ffprobe`, built with libass (burned-in captions) and libx264 (software encoder).
- [uv](https://docs.astral.sh/uv/) is recommended because it installs the locked dependency set.
- The Anton font, optional. It's the caption and hook typeface. Without it libass picks a
  fallback font.

FFmpeg builds differ. Check the filters and encoders in the build you installed:

```sh
ffmpeg -hide_banner -filters | grep -E ' (ass|boxblur|loudnorm) '
ffmpeg -hide_banner -encoders | grep -E 'libx264|h264_vaapi'
```

You need `ass`, `boxblur`, `loudnorm` and `libx264`. `h264_vaapi` is optional: when it works on
`/dev/dri/renderD128` on Linux, rendering selects it. Encoding speed and quality depend on the
hardware and input. Anton is available from Google Fonts. On Linux, copy the `.ttf` into
`~/.local/share/fonts`, run `fc-cache`, and confirm with `fc-list | grep -i anton`.

## Install from a checkout

The package is not on PyPI yet:

```sh
git clone https://github.com/AndresGuido9820/zunara-studio.git
cd zunara-studio
uv sync --locked --no-dev --extra asr
uv run --no-sync zunara doctor
```

With pip, create and activate a separate virtual environment:

```sh
python3 -m venv .venv-pip
. .venv-pip/bin/activate
python -m pip install '.[asr]'
```

Pip resolves declared version ranges; it does not use `uv.lock`.

`--no-dev` omits contributor tools such as pytest, Ruff and mypy. Development setup is separate in
the [testing guide](../testing.md). `uv sync` installs the checkout in editable mode, so source
edits change the installed commands immediately; use a fixed checkout for a reproducible job.

## Optional extras

The base install can inspect videos, rank a fetched workspace, and run the review queue and exports.
Everything else is opt-in:

| Extra | Adds | Needed for |
| --- | --- | --- |
| `asr` | `onnx-asr` (ONNX Runtime, CPU) and `huggingface-hub` | `render`: the word-by-word captions |
| `youtube` | Google API client and OAuth libraries | `auth` and `publish` |
| `analysis` | `librosa` and `scipy` | `match` |
| `downloads` | `yt-dlp` | `fetch`, unless `yt-dlp` is already on your `PATH` |

`uv sync --locked --no-dev --all-extras` installs all four. If an extra is missing, the command that
needs it fails; see [troubleshooting](troubleshooting.md#missing-extras).

The base runtime dependencies are:

| Package | Declared minimum | Used for |
| --- | --- | --- |
| `filelock` | 3.18 | Workspace/export coordination |
| `platformdirs` | 4.3 | OS-specific config and data directories |
| `numpy` | 2.0 | Signal arrays and rendering audio |
| `soundfile` | 0.13 | WAV access for analysis and ASR |
| `requests` | 2.32.4 | HTTP acquisition |

[pyproject.toml](../../pyproject.toml) declares direct dependency ranges; [uv.lock](../../uv.lock)
records the full resolved set, including transitive packages. The `dev` group contains contributor
tooling and is separate from the runtime extras above.

## The speech model

`render` transcribes audio with NVIDIA's Parakeet TDT 0.6B v3 model (int8 ONNX build). The `asr`
extra doesn't include the weights. The first render downloads model weights from Hugging Face into
its cache (`~/.cache/huggingface` unless `HF_HOME` says otherwise), and later renders reuse that
copy. Once the model is downloaded, `HF_HUB_OFFLINE=1` keeps renders from contacting Hugging Face.
Recognition runs on the CPU; a GPU is not required. Allow RAM for the ONNX model and working arrays;
there is no validated minimum hardware specification. Start with a short clip. A 16 kHz mono 16-bit
VOD WAV uses about 110 MiB per hour, before model weights, query audio, videos and temporary
render/export/upload copies.

## What `doctor` checks and where files go

`zunara doctor` never reads tokens or calls an API. It reports whether `ffmpeg`, `ffprobe` and
`yt-dlp` are on `PATH`, whether a YouTube client file exists, and the resolved `config_dir` and
`data_dir`. It doesn't test FFmpeg filters, fonts, extras, tokens or the speech model.

On Linux the config directory is `~/.config/zunara` (OAuth tokens, publishing policy) and the data
directory is `~/.local/share/zunara` (default exports and fetch workspaces). Override them with
`ZUNARA_CONFIG_DIR`, `ZUNARA_DATA_DIR` and `ZUNARA_YOUTUBE_CLIENT_SECRET`; see the [workspace
reference](../reference/workspace.md#config-and-data-paths). Zunara doesn't load `.env` files. Keep
workspaces, media and credentials outside the checkout.

Next: [make your first clip](first-clip.md).

## Optional publication service

The multi-platform `social` commands use the existing `requests` dependency. They require an
Upload-Post account and key, separate from Google OAuth and browser sessions. Account connections,
plan limits and private key storage are described in [API publishing](social-api.md). The offline
`zunara social doctor` command reports configuration presence without validating a live grant.

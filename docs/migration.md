# From the local scripts

Zunara Studio extracts the clip workflow from an earlier local folder named `money_engine`.
The original folder is left intact. Videos and working data are not copied into this repository.

| Previous script | Installed command |
| --- | --- |
| `clipper/fetch_vod.py` | `zunara fetch CHANNEL --workspace /path/to/work` |
| `clipper/audio_match.py` | `zunara match VOD_AUDIO MATCHES_JSON QUERY_AUDIO...` |
| `clipper/rank_moments.py` | `zunara rank WORK [TOP_N]` |
| `clipper/render.py` | `zunara render WORK START END --hook TEXT --name NAME` |
| `clipper/publish.py queue WORK` | `zunara queue WORK` |
| `clipper/publish.py status WORK` | `zunara status WORK` |
| `clipper/publish.py tiktok WORK` | Review, then `zunara export WORK --platform tiktok` |
| `clipper/publish.py youtube WORK` | `zunara publish WORK --channel ALIAS --channel-id ID` (preview) |

Existing workspaces remain usable at their current paths. Refresh a queue, review its metadata
and approve selected media before export/upload. Old records are not automatically considered approved.
Authorize again using `zunara auth`: old `.pickle` files are not read. Set your OAuth JSON location
explicitly; there is no fallback to another project's credentials.

The earlier blog, product-generation, social text templates, stream monitor and MCP server are
not part of this release. They remain in the original folder and have not been audited as supported
Zunara features. This repository is the clip tool, not a rebrand of every unrelated script.

## Optional multi-platform publication

Existing queues and direct YouTube commands continue to work. The `social` commands add a separate
API receipt file only when publication executes. Configure the provider key and connect accounts
before using them. Preserve both queue and API receipt files during rollback or workspace moves;
removing receipt state can permit duplicate submissions. Older versions ignore the new receipt
file and cannot reconcile its remote requests. Do not use an old version to retry an unresolved
API submission.

## Native Instagram publication and audio inspection

The native `instagram` commands add their own receipt file without rewriting direct YouTube or
optional service receipts. Preserve all of them during rollback. `inspect` adds `audio_codec` and
`audio_sample_rate` fields (null if absent/unknown); existing fields remain. Rendering now fixes AAC
output at 48 kHz to prevent loudness normalization from leaving a 96/192 kHz stream. Existing
videos are unchanged and may need a reviewed 48 kHz copy for native Reels.

## Local job scheduling

`zunara jobs` adds `DATA_DIR/scheduler/jobs.json`. It does not change any workspace format. Stop any
other scheduler or browser runner before you enable it. Schedule from the original workspace with
its receipts in place, since only the selected workspace's receipts are checked; copies are not
indexed globally. Add jobs only for clips you select. See [local scheduling](guides/scheduling.md#moving-from-another-scheduler).

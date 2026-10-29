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

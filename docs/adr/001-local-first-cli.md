# 001: Keep media processing local and publication explicit

Status: accepted.

The original scripts mixed reusable processing with local paths and downloaded files. A hosted
backend would add storage, account isolation and operational work without helping a local editor.

Use a `src` Python package, a CLI composition boundary, a JSON workspace and optional adapters.
Keep OAuth material in user config directories. Require media review and the exact channel ID
before uploads. Use explicit execution flags rather than automatic publishing after render.

Consequences: users can install and test without API credentials. A filesystem queue is easy to
inspect, but it is single-user and cannot provide a transaction with a remote platform. An unknown
upload outcome needs manual reconciliation. A service, multi-user database and automatic scheduling
daemon remain separate future decisions.

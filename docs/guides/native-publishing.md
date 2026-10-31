# Publish without an intermediary

Zunara can use your own platform application instead of a social publishing subscription.
YouTube uses the existing Google OAuth adapter. Instagram uses Meta's Facebook-Login API and
uploads the reviewed local file directly to Meta. Neither adapter needs Upload-Post credentials.
Platform authorization, quotas and account eligibility still apply.

## YouTube

Follow the [direct YouTube guide](youtube.md): create a personal Google Cloud project, enable
YouTube Data API v3, configure a Desktop OAuth client and authorize the intended channel. The
existing `auth` and `publish` commands already implement that path. An account logged into YouTube
in Chrome does not supply an OAuth grant to the CLI.

## Instagram: one-time setup

The local-file upload path requires **Facebook Login for Business**, a professional Instagram
account linked to a Facebook Page, and the appropriate Meta application access. An Instagram-Login
token uses a different API path and cannot be substituted for the Page token used here.

1. Create your own Meta developer app and configure Facebook Login for Business and Instagram.
2. Link the intended professional Instagram account to a Page you manage.
3. Obtain the Page access token and Instagram account ID through Meta's authorized setup flow.
   Publishing needs `instagram_basic`, `instagram_content_publish` and `pages_read_engagement`;
   additional permissions can depend on how account roles were assigned. Request only what the
   actual app requires. Meta decides the required access level and review.
4. Put the token in `ZUNARA_INSTAGRAM_ACCESS_TOKEN`, or the private file
   `CONFIG_DIR/instagram-access-token`. Do not paste it into a chat, issue or repository. Keep file
   permissions owner-only (`chmod 600`).
5. Run `zunara instagram doctor`, then verify the exact account:

```sh
zunara instagram account --account-id IG_ACCOUNT_ID --username YOUR_HANDLE
```

The account ID is numeric; it is not the Facebook Page ID. Both ID and username must match the
API response. `doctor` checks local setup presence without contacting Meta. `account` is an
online read, and does not create a container or publish anything. The adapter pins Graph API
`v26.0`, verified against Meta's documentation in October 2026; use `--api-version` deliberately
when migrating to another supported version.

## Upload, process, publish

First [review and approve](first-clip.md) the actual video and its caption. A caption comes from
queue description, falling back to title. Hashtags belong in that caption. The native adapter
checks media approval, account binding and the configured Instagram topic policy.

```sh
zunara instagram publish /work/session-01 --only clip-01 \
  --account-id IG_ACCOUNT_ID --username YOUR_HANDLE
```

This is a preview. It does not upload the file or create a remote container. Add `--execute` to
prepare the reviewed upload. The adapter copies the approved bytes, creates an Instagram Reel
container and transfers that copy to Meta's fixed upload host. No public file server or CDN is
needed. A processing container is not a published post.

```sh
zunara instagram publish /work/session-01 --only clip-01 \
  --account-id IG_ACCOUNT_ID --username YOUR_HANDLE --execute
zunara instagram status /work/session-01 --refresh
zunara instagram finish /work/session-01 --only clip-01 \
  --account-id IG_ACCOUNT_ID --username YOUR_HANDLE
# Add --execute to finish when Meta reports FINISHED and publication is authorized.
```

`finish` previews by default. Executing it publishes only the selected prepared container after
checking its saved identity, media approval and current processing status. The receipt preserves the exact caption sent at creation. Later queue caption edits are refused
at finish because they would not change the existing container; restore the saved caption before
continuing. Keep media unchanged during the workflow. `status --refresh` is read-only: it never uploads or
publishes. An agent or task runner can operate these phases without a browser or further clicks
within the user's authorization. Meta recommends checking processing about once per minute for
up to five minutes; do not spin in a tight loop. Zunara itself adds no scheduling daemon or claim
of native Instagram scheduling.

Use `--ai-generated` when the content needs the platform's synthetic-media disclosure. Ordinary
captioning or cutting of original footage does not by itself create synthetic people or speech.
This initial adapter does not expose every Meta publishing feature, such as paid-partnership
labels, carousels or Stories; use an appropriate flow when your content needs them.

The CLI currently applies a conservative 300 MiB upload cap, separate from Meta's own limits.
It accepts H.264/HEVC video, 3–900 seconds, and 48 kHz AAC when an audio stream is present.
Media that passes these local checks can still be rejected by Meta for other requirements.

## Interrupted requests and receipts

Preserve `out/instagram-publications.json` with the workspace. It records the approved media hash,
account, container and submission stages before remote writes. An interrupted creation, binary
transfer or publish call must not silently start a second container or post. Query a known
container with `status --refresh`; if Meta reports `PUBLISHED`, record that outcome even when the
publish response or permalink was lost. Do not claim a public URL until one was returned.

`IN_PROGRESS` means processing; `FINISHED` means ready for publication; `PUBLISHED` means Meta
reports publication. `ERROR` and `EXPIRED` need recovery rather than repeated creation. An unknown
creation response can lack a container ID; reconcile it in Meta before retrying. Keep receipts and
source files intact, including on rollback. Local duplicate protection is scoped to the workspace;
it is not a global detector for copies uploaded elsewhere.

Contract tests use synthetic media and mocked HTTP. They prove request/state behavior, not that
a personal Meta app is authorized or a live Reel was published.

## TikTok: approval is not replaceable with code

TikTok's [direct-post guide](https://developers.tiktok.com/docs/en/content-posting-api-get-started)
requires a developer app, an approved `video.publish` scope and a user grant. Unaudited clients
are restricted to private visibility. Its [sharing guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines)
also require a product intended for a wider audience and a publishing experience with creator
identity, privacy choices and consent; a private account-uploading utility is not an accepted use.
An open repository alone is not evidence that the app has passed that process.

Zunara does not bypass those restrictions or pretend that implementing another client grants
public posting rights. A subscription is not the only possible route, but the native public route
still needs TikTok's approval and compliant onboarding. Until then, report that destination as
blocked rather than claiming that an exported video has been published.

The Instagram flow above follows Meta's [content-publishing documentation](https://developers.facebook.com/documentation/instagram-platform/content-publishing)
and its [official API collection](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api).

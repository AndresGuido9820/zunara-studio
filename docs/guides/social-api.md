# Publish through an API

The `zunara social` commands send approved local videos to Upload-Post, which publishes to the
connected Instagram, TikTok, YouTube or X account. Routine uploads need no browser. An initial
provider account, API key and social-account connection are required; installing Zunara does not
grant access to an existing browser session.

This adapter uses a third-party service. Video bytes and the selected title/description are sent
to that service and the destination platforms. Keep using the [direct YouTube adapter](youtube.md)
if you prefer your own Google project. Local rendering and export still work without either API.

## Connect once

1. Create an [Upload-Post account](https://app.upload-post.com/), review its terms and choose a plan.
2. In its dashboard, create a profile and connect only the social accounts you want Zunara to use.
   Complete the platforms' OAuth grants yourself and verify their identities.
3. Generate an API key. Supply it through `UPLOAD_POST_API_KEY` in the process environment, or
   save it in the private file `CONFIG_DIR/upload-post-api-key`. Do not put it in a command-line
   argument, queue record, issue, prompt or repository. Environment configuration takes precedence.
4. Run the offline configuration check, then discover connected accounts:

```sh
uv run --no-sync zunara social doctor
uv run --no-sync zunara social accounts
```

`doctor` checks configuration presence, not whether credentials work. `accounts` contacts the
provider and returns profile and platform identifiers. A profile's name is not necessarily your
Instagram handle or YouTube channel ID. Use the actual connected identifiers in the next step.

There is no extra Python package to install: the adapter uses Zunara's existing HTTP dependency.
Network access and valid provider permissions are runtime requirements.

### Cost and eligibility

As checked on 30 October 2026, Upload-Post lists 10 uploads per month and two profiles on its free
plan, with API access. **TikTok is excluded from Free.** Its Basic plan is US$24 per month when
billed monthly; the US$16 figure is the monthly equivalent of annual billing. Review the provider's
[current pricing](https://www.upload-post.com/pricing/) before choosing a plan. Zunara does not
purchase, upgrade or renew a subscription. The provider also lists a US$19/month add-on for
X posts containing links (up to 50 such posts per month); review that separately before using it
for repository promotion. Platform eligibility and moderation still apply.

## Preview an approved selection

First [render, review and approve](first-clip.md) the exact media. The example below is for a
Spanish entertainment clip and accounts you have connected. Replace every placeholder:

```sh
uv run --no-sync zunara social publish ~/Videos/session-01 --only clip-01 \
  --profile PROFILE_ID --platform instagram --platform youtube \
  --account instagram=INSTAGRAM_ID --account youtube=YOUTUBE_ID \
  --language es --privacy public
```

This performs online preflight without sending a video: media approval, metadata, topic policy,
provider profile, account identity and access are checked before the batch starts. Supply one
`--account PLATFORM=ID` for every requested platform. An absent, mismatched or expired connection
refuses the batch. Do not use a display name as identity evidence.

Add `--platform tiktok --account tiktok=TIKTOK_ID` for a plan and connection that support TikTok.
The CLI refuses TikTok on a free plan. For X, select an appropriately reviewed programming demo
when that is your destination's topic policy; media approval does not replace the user's publication
instructions. This initial adapter forwards title/description text, not the queue's separate tag list. Put
intended hashtags in that text.

## Submit and check

Repeat the reviewed command with `--execute` to transfer the video. YouTube privacy defaults to
`private`; `--privacy public` explicitly requests public YouTube uploads. Instagram and TikTok
follow their publication settings; the YouTube privacy flag does not make them private.

```sh
uv run --no-sync zunara social publish ~/Videos/session-01 --only clip-01 \
  --profile PROFILE_ID --platform instagram --platform youtube \
  --account instagram=INSTAGRAM_ID --account youtube=YOUTUBE_ID \
  --language es --privacy public --execute
uv run --no-sync zunara social status ~/Videos/session-01
uv run --no-sync zunara social status ~/Videos/session-01 --refresh
```

Submission can return a request ID before processing finishes. `status` reads local receipts;
`--refresh` queries their provider status without submitting another video. A provider-confirmed
ID or URL is different from checking publicly playable video. TikTok moderation can delay its URL;
keep that result pending rather than inventing a link or submitting it again. The adapter disables
TikTok's automatic inbox fallback and treats any returned inbox delivery as a draft, not a
completed post.

For provider scheduling, calculate a future time and pass the same value to preview and execution:

```sh
SCHEDULE=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(days=1)).isoformat())')
# Add --schedule "$SCHEDULE" to the publication commands.
```

Use an explicit UTC offset and a time within the provider's 365-day horizon.
A returned schedule/job ID confirms acceptance of a schedule, not completed publication. The
provider handles that schedule; Zunara adds no local daemon. A caller can run the CLI from a task
runner, but each new immediate upload still needs that runner and this computer available.

## Supported metadata

The adapter exposes queue title/description, YouTube privacy/language and provider scheduling.
The provider's other options, including the separate YouTube tag list, thumbnails, commercial
content settings, made-for-children declarations and synthetic-media disclosure, are not exposed
in this initial CLI. Use a publisher that supports the required settings when the provider's
ordinary defaults do not fit your content. Passing custom queue fields does not enable them.

## Receipts and interrupted requests

`out/social-publications.json` records the submitted media hash, provider profile, destination
account and each platform's state. Keep it with the workspace. It is separate from the existing
YouTube queue fields. The command saves its client request ID before the remote request, sends an idempotency key,
and uses a verified temporary media copy. Re-running execution must not resend the same recorded selection.

If the connection drops during submission, the provider may already have accepted the video. Leave
the receipt intact and run `social status WORK --refresh`: it can query the client request ID
even after a lost response. If the outcome remains unresolved, inspect provider history before
any retry. Do not delete a receipt or create another workspace merely to bypass a duplicate
guard. A partial batch can contain both successes and failures; retrying the entire batch may
create duplicates. The local guard is per workspace, not a global cross-workspace registry.

Keep original sources and media unchanged during publication. Approval covers video bytes, not
subsequent metadata edits or permission to post. The topic policy checks labels, not video meaning.
No live provider upload is proven by the contract tests: those use synthetic media and mocked HTTP.

API behavior is based on the provider's [OpenAPI specification](https://docs.upload-post.com/openapi.json)
and [video-upload documentation](https://docs.upload-post.com/api/upload-video).

# Integrations setup (owner)

How to register Slate's OAuth app with each provider (ADR-0032, ADR-0033). Each provider needs a **client ID** and **client secret**, set in three places:

| Where | What |
| --- | --- |
| Vercel, project `api` → Settings → Environment Variables | the client ID and secret (the API runs the sign-in) |
| GitHub → repo → Settings → Secrets and variables → Actions | the same values (the worker refreshes tokens while syncing) |
| Local `.env` | the same values, with `APP_URL=http://localhost:3000` |

Every provider uses **one redirect URI**, the web app's callback:

- Production: `https://slate-livid-nine.vercel.app/integrations/callback`
- Local: `http://localhost:3000/integrations/callback` (Slack accepts https only, so test Slack on the deployed site)

`APP_URL` must be set (Vercel `api`: `https://slate-livid-nine.vercel.app`), and `CREDENTIALS_FERNET_KEY` must be set everywhere, since tokens are encrypted with it. After changing Vercel variables, redeploy.

Never paste a secret into a chat or commit it.

## Google: Gmail, Google Calendar, Google Drive

One Google client serves all three. Each is a separate sign-in that asks only for its own permission.

1. In the Google Cloud console, open the project already used for Gmail.
2. **APIs & Services → Library:** enable **Gmail API**, **Google Calendar API** and **Google Drive API**.
3. **Google Auth Platform → Data access → Add or remove scopes:** add
   - `.../auth/gmail.readonly` (restricted)
   - `.../auth/calendar.readonly` (sensitive)
   - `.../auth/drive.readonly` (restricted)
   - `openid`, `.../auth/userinfo.email`
   Save.
4. **Clients:** the existing web client already has both redirect URIs. Nothing to change.
5. **Audience:** stays *In production*, unverified. Don't submit for verification. Slate shows users a notice explaining Google's warning screen.
6. Env: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` (already set for Gmail).

**If someone unticks a permission** on Google's screen, Slate refuses the sign-in and asks them to connect again with every box ticked. **If Google returns no refresh token** (rare; it happens when an old grant is reused), they must remove Slate under myaccount.google.com → Security → Third-party access, then connect again.

## Notion

1. Go to https://www.notion.so/profile/integrations → **New integration**.
2. **Type: Public.** Name it `Slate`, and add a logo if you like.
3. **Capabilities:** tick **Read content** only. Untick Update content and Insert content. For user information, "without email addresses" is enough.
4. **OAuth domain & URIs → Redirect URIs:** add both callback URLs above. Notion also asks for a company name, website (`https://slate-livid-nine.vercel.app`), privacy policy (`https://slate-livid-nine.vercel.app/privacy`), terms (the same link is fine) and support email (`usta.aashu@gmail.com`).
5. Save, then copy the **OAuth client ID** and **OAuth client secret**.
6. Env: `NOTION_OAUTH_CLIENT_ID`, `NOTION_OAUTH_CLIENT_SECRET`.

When people connect, Notion asks them which pages Slate may see. Slate indexes only those pages and their sub-pages. To add pages later, they reconnect, or share the page with the Slate integration inside Notion.

## Linear

1. In Linear: **Settings → API → OAuth applications → Create new**.
2. Name `Slate`, developer `usta.aashu@gmail.com`, developer URL `https://slate-livid-nine.vercel.app`.
3. **Callback URLs:** add both callback URLs above.
4. Leave it private to your workspace, or make it public if other Linear workspaces should connect.
5. Copy the **Client ID** and **Client secret**.
6. Env: `LINEAR_OAUTH_CLIENT_ID`, `LINEAR_OAUTH_CLIENT_SECRET`.

Slate asks only for the `read` scope.

## Slack

1. Go to https://api.slack.com/apps → **Create New App → From scratch**. Name it `Slate`, pick your workspace.
2. **OAuth & Permissions:**
   - **Redirect URLs:** add `https://slate-livid-nine.vercel.app/integrations/callback` (Slack accepts https only).
   - **User Token Scopes** (not Bot Token Scopes): `channels:history`, `channels:read`, `users:read`.
3. To let people outside your workspace connect: **Manage Distribution → Activate Public Distribution**. Without it, only your own workspace can.
4. **Basic Information → App Credentials:** copy the **Client ID** and **Client Secret**.
5. Env: `SLACK_OAUTH_CLIENT_ID`, `SLACK_OAUTH_CLIENT_SECRET`.

Slate indexes public channels the person has joined, one document per channel per day. Slack rate-limits apps that aren't in its Marketplace heavily, so large workspaces sync slowly; runs stop and resume on their own.

## Check it works

1. `uv run alembic upgrade head` (applies 0010) on the test branch, run `uv run pytest -m integration`, then do the same on prod.
2. Open **Integrations**: each provider you configured shows **+ Connect**. Ones without keys show "Not set up".
3. Connect one, and you return to Integrations with "Connected …". Open **Manage**: the account shows "First sync queued", then a count.
4. Ask a question in Chat about something in it, for example "what did Priya email me last week?" or "what's on my calendar next week?".

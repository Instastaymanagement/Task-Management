# Task Management

Every **5 minutes**, this workflow reads new Slack messages and guest conversations, finds the tasks in them, and adds them to monday.com assigned to the right person.

1. **Read:** picks up everything new since the last run:
   - **Slack:** new messages and thread replies in every channel the bot has been invited to.
   - **Quo:** new text messages on the London (Canada) and Boston (USA) Quo numbers. Calls aren't read, and the "Primary" number isn't linked. Tasks from the London number go on the Canada board, tasks from the Boston number on the USA board (set in `QUO_NUMBERS` in the workflow).
   - **Hospitable:** new messages in every guest conversation (reservations from 6 months ago to a year ahead). Templated automated messages are left out.
2. **Find:** Claude reads the conversation and picks out real tasks (things someone was asked to do, agreed to do, or clearly needs to do). It skips chit-chat, FYIs, work that's already done, and tasks already open on the boards. For each task it decides:
   - **who** should do it (whoever was asked, @-tagged, or said "I'll do it"),
   - **USA or Canada**, from the property, city, or channel (it's given both Properties Master lists). Guest conversations always go to the country of the reservation's property.
   - a **deadline**, if one was mentioned ("by Friday", "tomorrow").
3. **Add:** creates the task on the **Daily Tasks** board of the matching workspace:

| Country | monday workspace | Board |
|---|---|---|
| USA | USA Automated (Trial) | Daily Tasks (`18435520178`) |
| Canada | Canada Automated(Trial) | Daily Tasks (`18435476578`) |

Each new item goes in the **Other task** group with:

- **Name:** a short description of the task.
- **Person:** the assignee. monday notifies them.
- **Status:** Not done.
- **Deadline:** when one was mentioned.
- **Notes/Updates:** the details, where it came from (a link to the Slack message, or the guest, property and reservation code), and a `[slack …]` or `[hospitable …]` key. The key stops the same message from ever creating a second item.

From guest conversations, Claude picks out work the team has to do for the stay: repairs, missing items, supplies, cleaning, laundry, early check-in or late checkout to arrange, refunds or charges, complaints to follow up, and anything a host reply promised. Questions the host already fully answered (wifi, parking, directions) are skipped.

If a task can't be tied to a country, it goes on the Canada board. Change `DEFAULT_COUNTRY` in the workflow to change this.

## Setup

### 1. Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → *From scratch* and pick the Instastay workspace. Keep it as an internal app for your own workspace (Marketplace-distributed apps get much tighter message-reading limits).
2. **OAuth & Permissions** → *Bot Token Scopes*, add:
   `channels:history`, `channels:read`, `groups:history`, `groups:read`, `users:read`, `users:read.email`
3. **Install to Workspace** and copy the **Bot User OAuth Token** (`xoxb-...`).
4. In each channel to watch, type `/invite @<app name>`. The bot only reads channels it's in.

`users:read.email` lets the script match Slack people to monday users by email. That way Claude knows that the Slack name "Falis Ahmed" and the monday user "Falis" are the same person.

### 2. Repository secrets

Add these in **Settings → Secrets and variables → Actions**:

| Secret | Notes |
|---|---|
| `SLACK_BOT_TOKEN` | Bot token from step 1 |
| `MONDAY_API_TOKEN` | monday.com → avatar → Developers → My access tokens |
| `HOSPITABLE_API_TOKEN` | Hospitable → Apps → API access → personal access token (read access is enough). Without it, guest conversations are skipped. |
| `QUO_API_KEY` | Quo → Settings → API → generate key. Without it, Quo texts are skipped. |
| `ANTHROPIC_API_KEY` | Claude API key from console.anthropic.com |
| `ANTHROPIC_WORKSPACE_ID` | Only needed if the API key isn't scoped to a workspace |

You can reuse the monday, Hospitable and Anthropic values from the Review-Automation / Review-Response repos.

## Running

- **Scheduled:** every 5 minutes, and **creates tasks for real**. GitHub sometimes starts scheduled runs a few minutes late. Nothing is missed when that happens, because each run reads everything since the last successful run.
- **Manually:** Actions → *Slack tasks to monday.com* → Run workflow. **dry_run** is ticked by default. It lists the tasks it would create on the run page without touching monday or moving the checkpoint.
- **First run:** reads the last 2 hours (`FIRST_RUN_HOURS`).
- **Only some channels:** set `SLACK_CHANNELS` in `.github/workflows/slack-tasks.yml`, e.g. `"maintenance,cleaning-ops,team"`.

### Exact 5-minute runs (external cron)

GitHub's own timer often runs late or skips runs. A free cron service starts the workflow every 5 minutes through GitHub's API. The GitHub timer stays on as a backup. If two runs overlap, the second waits for the first, and a Slack message never creates more than one task.

1. **GitHub token:** GitHub → avatar → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token.
   - Resource owner: **Instastaymanagement**
   - Repository access: **Only select repositories** → Task-Management
   - Permissions → Repository → **Actions: Read and write** (nothing else)
   - Expiration: up to 1 year. Put a reminder in your calendar to renew it.
2. **cron-job.org:** sign up for free, then **Create cronjob**:
   - URL: `https://api.github.com/repos/Instastaymanagement/Task-Management/actions/workflows/slack-tasks.yml/dispatches`
   - Schedule: every 5 minutes
   - Advanced → Request method: **POST**
   - Advanced → Headers:
     - `Authorization: Bearer <your token>`
     - `Accept: application/vnd.github+json`
     - `Content-Type: application/json`
     - `X-GitHub-Api-Version: 2022-11-28`
   - Advanced → Request body: `{"ref":"main","inputs":{"dry_run":"false"}}`
3. Click **Test run**. A good response is **204 No Content**, and a new run appears under Actions within a few seconds.

Leave `"dry_run":"false"` in the request body. Without it, manual runs default to a dry run and create nothing.

The time of the last run is stored in the GitHub Actions cache (`slack-state-*`), separately for Slack and Hospitable. If Hospitable is down, guest conversations are retried on the next run, and Slack carries on. If the cache is cleared, the next run reads the last `FIRST_RUN_HOURS` again. The Slack key in each item's notes stops anything already added from being added twice.

## Tests

```
pip install -r requirements.txt
python -m unittest discover -s tests
```

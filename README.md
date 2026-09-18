# github history

Clone a GitHub repository, rewrite every contributor in its history to a single
identity, give it a new name derived from what the repo contains, and publish it
as a fresh repository on your own account.

Four steps, run from a local web UI or from the command line:

| step | what happens |
| --- | --- |
| clone | `git clone` into `repos/<owner>/<repo>` |
| rewrite | every author, committer and tagger becomes one identity, and `Co-authored-by` trailers are removed; timestamps are kept |
| name | a new repo name is invented from the README, manifest, layout and language mix |
| publish | a new repo is created on your GitHub account and the history is pushed into it |

## Requirements

- Python 3.8 or newer — standard library only, nothing to install
- `git`
- [`gh`](https://cli.github.com/) (GitHub CLI), authenticated

Check that you are signed in:

```bash
gh auth status
```

The token needs the `repo` scope to create repositories. Nothing else is
required — the email you publish under is not validated against the account, so
there is no need for the `user` scope.

## Running the UI (the easy path)

```bash
python3 ui.py
```

That serves `http://127.0.0.1:8765/` and opens it in your browser. If 8765 is
taken, it walks forward to the next free port and tells you which one it used.
The page:

- signs you in to GitHub with the browser device flow (a one-time code you paste
  at `github.com/login/device`), with **Cancel** to back out while it waits
- takes the original GitHub URL and the GitHub username to credit
- shows a status line, a live log and a progress bar with a percentage
- has **Run**, **Pause**, **Resume** and **Stop**

**Dry run is checked by default.** A dry run does the clone, the rewrite and the
naming for real, then prints what it *would* create on GitHub without creating
it. Uncheck it to publish.

Pause is not cooperative: running git commands are sent `SIGSTOP`, so a clone
halfway through receiving objects genuinely freezes and continues where it left
off on Resume.

Options: `--port N` to pin a port (it fails with a clear message if that exact
port is busy), `--no-browser` to not open one. The
server listens on `127.0.0.1` only, and every API call requires a token minted
at startup and embedded in the page.

## Connecting to a relay

The **Server** card joins this program to a [Relay](https://relay-production-7643.up.railway.app)
workspace as a *worker*. **Connect server** reveals the fields; **Connect** enrols.

Give the **workspace** URL, not the console root — the workspace slug is the last
path segment:

```
https://relay-production-7643.up.railway.app/upwork
                                            ^^^^^^ the workspace
```

The handshake is the relay's own worker protocol:

```
POST {base}/{workspace}/enrol   {worker_id, token, label}   -> 202 Accepted
```

`worker_id` is the active profile's **id** (not its username), `token` is that
profile's token, and `label` is its name. A profile with no id cannot connect.

`202` means **pending, not connected**. The worker picks its own token and waits
in the workspace's pending list until an operator approves it in the relay
console. The card shows *Waiting for approval* with the worker id to approve, and
re-checks every few seconds by calling `GET /{workspace}/messages`, which starts
answering once approved — so the card flips to *Connected* on its own. **Check
now** forces a check; **Disconnect** forgets the workspace and token.

After approval the worker authenticates with `Authorization: Bearer <its token>`:

```bash
python3 server_link.py connect https://host/upwork
python3 server_link.py status                 # local view, no request
python3 server_link.py refresh                # ask whether approval arrived
python3 server_link.py publish <channel> '{"any":"json"}'
python3 server_link.py messages --wait 25 --limit 50
python3 server_link.py ack <channel> <seq>
python3 server_link.py disconnect
```

The workspace URL, worker id and token live in `server.json` (mode `0600`,
gitignored). What travels over `publish`/`messages` is not decided yet.

## The queue

While a relay is connected, the program listens on one channel (`github` by
default) and queues every GitHub URL that arrives. Queued URLs are run **one at
a time**, using the account currently in use. When the queue drains, a single
report is published back to the same channel:

```json
{"event": "done", "worker": "conner-morrison", "completed": 2, "failed": 0,
 "repos": [{"source": "...", "published": "...", "name": "..."}],
 "dry_run": false, "at": "2026-09-18T12:20:40Z"}
```

URLs are read out of whatever shape the message has — a bare string, a
`{"url": ...}` field, a list, or a sentence with links in it — and anything on
another channel is ignored. Messages are acknowledged once queued, so a restart
does not replay them, and the queue itself is on disk (`queue.json`) so it
resumes where it left off.

The queue sits at the top of the **Run** panel, above the original URL field,
and lists what is waiting, running, done or failed with the published repo for
each. **Fold** collapses the list (the state is remembered), **Listening** stops
taking new work, and **Clear finished** drops the completed rows. URLs can also
be added by hand there, which is the easy way to test without a relay.

Queued runs always run for real and publish **public** repositories. There is no
dry-run switch on the queue; the manual run below it still has one.

A failed URL does not stop the queue; it is marked failed, counted in the report
and the next one starts. The report is sent once per drain, not repeatedly while
idle.

Start the UI with `--no-queue` to neither listen nor run.

## History and retention

Every real publish is recorded in `history.json` (gitignored). Dry runs are not
recorded, because they create nothing.

In the UI, **Show history** lists them: repository, when it was published, how
long it has left, and buttons to **Pin** or **Delete**. The retention window is
set in the same panel and defaults to **7 days**.

- **Delete** removes the repository from GitHub *and* the local clone, after a
  confirmation. It cannot be undone.
- **Pin** exempts a repository from the automatic sweep. Pinned repositories are
  kept until you delete them yourself.
- **The sweep** runs when the UI starts and once an hour while it is open. It
  deletes recorded repositories past the retention window. Nothing outside
  `history.json` is ever touched, and a local clone is only removed when it sits
  inside `repos/`.
- Start the UI with `--no-sweep` to leave expired repositories alone.
- A retention of `0` means the next sweep deletes everything unpinned.

### Permission to delete

Deleting needs the `delete_repo` scope, which `gh auth login` does not grant.
Until it is granted, deletes and sweeps fail with that message and the entry
stays in the list to be retried — nothing is lost, but nothing is deleted
either.

Grant it from the UI with **Allow deleting repos** in the account card (it
appears only while the scope is missing), then copy the one-time code and finish
in the browser. **Cancel** stops that flow and leaves the current login exactly
as it was. Or from a terminal:

```bash
gh auth refresh -h github.com -s delete_repo
```

Either way the browser step must be completed — paste the code and approve. It
is not granted until you do, and `gh auth status` is the check:

```bash
gh auth status        # 'Token scopes:' should now include delete_repo
```

The same operations work from the command line, which is what you would put in
a cron job if you want sweeping without the UI open:

```bash
python3 history.py list
python3 history.py sweep --dry-run     # show what is due
python3 history.py sweep               # delete what is due
python3 history.py delete <name>       # delete one now
python3 history.py keep <name>         # pin it; --off to unpin
python3 history.py retention 14        # change the window
```

## Running from the command line

Copy the template and fill it in:

```bash
cp .env.example .env
```

```ini
GIT_AUTHOR_NAME="your-github-login"
GIT_AUTHOR_EMAIL="your-github-login@users.noreply.github.com"
```

`GIT_COMMITTER_NAME` and `GIT_COMMITTER_EMAIL` are optional and default to the
author values. `NAME` and `EMAIL` work as fallback keys. `.env` is gitignored.

Then:

```bash
python3 clone_repo.py https://github.com/owner/repo --dry-run   # see what it would do
python3 clone_repo.py https://github.com/owner/repo             # publish, private, with a prompt
python3 clone_repo.py owner/repo --public --name my-own-name -y
```

Useful flags: `--no-rewrite` (clone only), `--no-push` (stop after rewriting),
`--name` (skip name generation), `--dest` (clone somewhere else), `--depth`
(shallow clone), `-f` (re-clone over an existing copy).

Accepted URL forms: `https://github.com/owner/repo`, the `.git` form,
`git@github.com:owner/repo.git`, bare `owner/repo`, and deep links such as
`.../tree/main/src`.

## The individual tools

Each step also runs on its own:

```bash
python3 rewrite_history.py repos/owner/repo          # rewrite an existing clone
python3 name_repo.py repos/owner/repo --explain      # names, and the signals behind them
python3 push_history.py repos/owner/repo --check-only # check the connection, change nothing
python3 push_history.py repos/owner/repo --new        # publish to a new repo
```

`push_history.py` without `--new` force-pushes over the repo you cloned from.
That is the one destructive path in here and nothing calls it for you.

## What is checked before publishing

One thing: that this device is connected to GitHub, via `gh auth status`. If it
is, the run proceeds; if not, it stops with gh's own message.

The name and email are **not** compared against the account. Whatever you enter
is what the rewritten commits carry, so any address works — your real one, a
`noreply` one, anything. The UI prefills the email from your GitHub profile,
falling back to `git config user.email`, then to
`<login>@users.noreply.github.com`.

## Saved accounts

The account card keeps a list of profiles. **Add account** takes a name, id,
token, username and email; **Use** selects one in a click; **Forget** removes it.
**Click a profile** to open it, edit any field and save it again — leave the
token blank there to keep the saved one.

What a profile is for, and what the token is **not**:

- The **token is the relay worker password** — the secret the relay approves
  together with the worker id. It is **not** a GitHub credential and is never
  used to authenticate to GitHub. Any string is accepted and stored as entered.
- **Use** only marks the profile active. It supplies the relay worker id and
  token when you Connect, and the **username and email** that commits are
  rewritten to. It does **not** sign you in to GitHub.
- The account that actually pushes, creates and deletes is whoever is signed in
  through the browser **Sign in** button — separate from these profiles.
- Because nothing is derived from the token, the **username is required**; blank
  name, id and email fall back to the username (email to
  `<username>@users.noreply.github.com`).

The tokens live in `accounts.json` next to the program: mode `0600`, gitignored,
and never sent to the browser — the page only ever sees `...4OHJ`. Anyone who can
read that file can act as those profiles, so treat it like any other credential
file, and use **Forget** to remove one.

```bash
python3 accounts.py list
python3 accounts.py add --name "Conner Morrison" --email you@example.com   # prompts for the token
python3 accounts.py switch conner-morrison
python3 accounts.py remove conner-morrison
```

### The username is resolved, not trusted

The UI asks for one identity field: a **GitHub username**. Before anything is
cloned, it is looked up with `GET /users/<login>`. If no such account exists -
or the name is an organization, or not a valid login - the run is refused with
that message and nothing happens.

When it does exist, the rewrite uses that account's own details: its display
name (or login), and its address. GitHub does not publish account emails, so
unless the profile sets one, the address used is the account's own
`<id>+<login>@users.noreply.github.com`, which GitHub links back to it - commits
written with it are credited to that user.

The same lookup runs from the command line and from the **Check username**
button:

```bash
python3 verify_user.py conner-morrison
github.com/conner-morrison exists (id 264292412)
commits will carry conner-morrison <264292412+conner-morrison@users.noreply.github.com>
```

The command-line pipeline still reads its identity from `.env` and does not do
this lookup.

GitHub also builds the contributor list from `Co-authored-by:` trailers in
commit messages, not just from the author field. Those are stripped by default,
since a rewrite that left them would still credit the original people. Pass
`--keep-coauthors` to `rewrite_history.py` to keep them. `Signed-off-by` is left
alone: it does not create contributors.

Note what that means on GitHub: commits are attributed by email. An address
tied to another person's GitHub account will show those commits as authored by
them, on their profile. Use an address that is yours.

## Troubleshooting

- **`You are not logged into any GitHub hosts`** — run `gh auth login`, or use
  the Sign in button in the UI.
- **`no GitHub user named '...'`** — the username does not exist. Check it with
  `python3 verify_user.py <name>`.
- **`every candidate name is already taken`** — pass `--name` (or fill the name
  field) with something specific.
- **`git fast-import failed`** — the message carries fast-import's own first
  error line; a crash report is written to the clone's `.git/` directory.
- **Port already in use** — only happens when you pin a port with `--port`;
  without it the server picks the next free one. `ss -ltnp | grep :8765` shows
  what is holding it.

## What this does to attribution

The rewrite replaces *every* contributor with one identity, and the generated
name does not resemble the original. On your own repositories that is
housekeeping. On someone else's, it removes the attribution their license almost
certainly requires, and a generated name makes the origin hard to trace. The
publish step creates a private repo by default for that reason; making it public
is the point where this stops being a private copy.

## Layout

| file | role |
| --- | --- |
| `ui.py`, `ui.html` | local web UI and its page |
| `pipeline.py` | the four steps as one observable job with progress |
| `proc.py` | pause / resume / stop for child processes |
| `clone_repo.py` | command-line entry point, URL parsing |
| `rewrite_history.py` | the history rewrite, `.env` parsing |
| `name_repo.py` | name generation from repo contents |
| `verify_user.py` | resolves a GitHub username to the identity to write |
| `accounts.py` | saved GitHub accounts and one-click switching |
| `accounts.json` | those accounts, including tokens (0600, gitignored) |
| `server_link.py` | handshake with a relay workspace, then send and receive |
| `queue_runner.py` | listens on the channel, runs queued URLs, reports done |
| `queue.json` | the queue and its settings (gitignored) |
| `server.json` | the connected server and this client's id (gitignored) |
| `history.py` | record of published repos, deletion and the retention sweep |
| `history.json` | that record (gitignored) |
| `push_history.py` | connection check, repo creation, pushing |
| `repos/` | where clones land (gitignored) |

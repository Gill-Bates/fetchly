# Platform Cookies

Public URLs download signed out. Age-gated, private, and login-walled content needs a
signed-in browser session, imported per platform.

**Settings → Integrations → Cookies**

## Importing a session

The reliable path is a *Copy as cURL* from your browser's dev tools:

1. Open the platform's site in a browser where you are **signed in**
2. Press ++f12++ to open the dev tools
3. Go to the **Network** tab and select the **Fetch/XHR** filter
4. **Reload** the page
5. Right-click any request to that platform → **Copy** → **Copy as cURL**
6. Paste it into the platform's tile in fetchly

## Accepted formats

Browsers do not hand out Netscape cookie jars, which is the only format yt-dlp reads.
fetchly therefore accepts whatever the dev tools actually produce and converts it:

| Format | What it is |
|---|---|
| `header` | A bare `name=value; name2=value2` string, with or without a leading `Cookie:` |
| `request` | A whole *Copy as cURL* / *Copy as fetch* / *Copy as PowerShell* command, in any of the shells the menu offers |
| `json` | The array written by cookie-export extensions (Cookie-Editor, EditThisCookie) — the only input carrying real expiry metadata |
| Netscape jar | A prepared `cookies.txt`, via the file upload |

Everything is normalized to the same Netscape jar in `DATA_DIR/cookies/`, written with
mode `0600`.

| Limit | Value |
|---|---|
| Max input size | 256 KiB |
| Encoding | UTF-8 |
| Rate limit | 10/minute (import, delete), 30/minute (status) |

## Validity checking

Each platform tile shows a status derived from a **structural** check of the stored jar:

- Does it parse as a Netscape cookie jar?
- Does it carry cookies for that platform's domains?
- Are they unexpired?
- Does it contain the cookies that mark a signed-in session?

| Platform | Expected cookie domains |
|---|---|
| YouTube | `.youtube.com`, `.google.com` |
| TikTok | `.tiktok.com` |
| Instagram | `.instagram.com` |
| Facebook | `.facebook.com` |

!!! info "Never a live check"
    fetchly does not contact the platform to test your cookies. A live check would mean
    a real request per jar and would risk tripping the platform's own abuse detection.
    A jar that passes the structural check can still be rejected by the platform — a
    revoked session looks structurally fine.

A pasted import additionally **requires** a session cookie and tells you which one is
missing; storing a jar without one would leave downloads signed out while Settings
claimed otherwise. The file upload is more permissive, for scripted setups that already
hold a prepared jar.

### When the check runs

| Moment | What is checked |
|---|---|
| On import | The paste or upload is parsed, converted, and judged **before** anything is written. A paste without a session cookie is rejected and nothing is stored |
| On opening Settings | The server re-reads every jar from disk and renders the tiles from that snapshot |
| On `GET /api/cookies` | The same snapshot as JSON. The page refetches it once on load and after each import or removal |
| Before every download | The worker re-checks the jar matching the submitted URL and skips it unless it still holds unexpired session cookies |

Nothing runs on a timer and nothing is cached: every badge you see was derived from the
file on disk at that moment. A jar that expires while a Settings tab sits open keeps its
last-rendered badge until you reload — the download path checks independently, so it
never acts on a stale badge.

## What a tile shows

Each tile in **Settings → Integrations → Cookies** carries a status badge, and under it
a line of chips describing what is actually stored.

| Badge | Status | Meaning | Downloads for that platform |
|---|---|---|---|
| None stored | `missing` | No jar imported yet | Run signed out |
| Valid | `valid` | Parses, and holds unexpired cookies for the platform's domains | Use the jar |
| Expired | `expired` | Every cookie for the platform's domains is past its expiry | Run signed out |
| Invalid | `invalid` | Not a Netscape jar, empty, or holds no cookies for this platform's domains | Run signed out |

The chips under a stored jar:

| Chip | Meaning |
|---|---|
| `12 cookies for .youtube.com, .google.com` | How many cookies the file holds, and which of the platform's domains they cover. Worth reading: a jar copied from the wrong request is otherwise indistinguishable from a working one |
| `expires 14 Mar 2027, 193 days left` | The **earliest** expiry among the platform's unexpired cookies — not a promise the session is still alive |
| `no expiry date` | The import came from a copied request header, which carries no expiry dates at all. Only a cookie-extension JSON export does |
| `updated 2 hours ago` | When the jar was last written. yt-dlp rewrites it at the end of every run, so this is when the platform last rotated the session — a better liveness signal than any expiry date |
| `Missing sessionid - downloads run signed out` | Structurally fine, but the cookie that marks a signed-in session is gone. The downloader looks for these by name |
| `Expired on 14 Mar 2026 - downloads run signed out` | Shown in place of the chips above once everything has expired |

Each tile's actions: **Paste cookies** opens the guided import dialog (it stays open on
a rejected paste, so the text is never lost), and **Remove**, shown only while a jar
exists, deletes it.

## How cookies are used

The platform is detected from the submitted URL, and the matching jar — if any — is
passed to yt-dlp for that job. A jar that is missing, unparseable, or whose session
cookies have all expired is **skipped**, and the download runs signed out rather than
failing.

Signed out is a working state, not an error: public URLs download exactly as they
always did. Only content the platform actually gates behind a login fails, and the
resulting message says whether to add cookies or refresh expired ones. So there is no
need to keep a jar current for a platform whose content you only pull publicly, and a
jar going stale never breaks a queue mid-run.

## Deleting

Each tile has a remove action (`DELETE /api/cookies/{platform}`). Deleting a jar reverts
that platform to signed-out downloads.

## Security

!!! danger "A cookie jar is a live session"
    An imported jar is a working, signed-in session for your own account on that
    platform. Anyone who reads the file can act as you there.

- Files are stored under `DATA_DIR/cookies/` at mode `0600`
- fetchly logs a warning if a jar is found group- or world-readable
- Treat the whole data volume as a credentials store: restrictive permissions,
  encrypted backups, no casual sharing of snapshots
- Sign out of the platform in the browser you copied from, and it invalidates the
  imported session too — copy from a session you intend to keep alive

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Tile says no session cookie | The copied request was not from a signed-in page, or the wrong request was copied |
| Tile is valid but downloads are still gated | The platform revoked the session; re-import |
| Import rejected as too large | Something other than a cookie header or export was pasted |
| Import rejected as not UTF-8 | A binary file was uploaded |

More in [Troubleshooting](../troubleshooting.md).

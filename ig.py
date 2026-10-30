#!/opt/homebrew/bin/python3.14
"""Instagram manager CLI over the official Instagram API (Instagram Login flavour).

Config lives in ~/DesignStudio/instagram/.env (chmod 600):
    IG_TOKEN=<long-lived user access token>
    IG_USER_ID=<instagram professional account id>        # filled by `ig.py whoami --save`
    IG_MEDIA_REPO=<github owner/repo used to host images>  # public URLs Meta can fetch
    IG_API_VERSION=                                         # optional, e.g. v23.0; blank = unversioned

Commands:
    whoami [--save]                 account info; --save stores IG_USER_ID
    limit                           publishing quota used in the last 24h
    post IMAGE --caption-file F     single image post
    carousel IMG1 IMG2 ... --caption-file F
    reel VIDEO --caption-file F [--cover IMG]
    queue-add WHEN KIND FILES... --caption-file F    WHEN = 'YYYY-MM-DD HH:MM' local time
    queue-list / queue-run [--dry-run]               run posts that are due
    insights [--days N]             account totals (reach, views, follows...)
    media-insights [--limit N]      per-post performance for recent posts
    comments [--limit N]            recent comments across recent posts
    reply COMMENT_ID TEXT           reply to a comment on our own post
    refresh-token                   extend the 60-day token
    sync-cloud                      push pending posts to the GitHub runner and pull back what it published
    checkin [--days N]              daily report + Mac notification

Cloud mode (IG_CLOUD=1, used by the GitHub Actions runner in the media repo): files and captions in
queue.json are repo-relative and already published on GitHub Pages, so nothing is converted or pushed.
With IG_POSTING=cloud in .env the Mac never publishes; it only runs sync-cloud.
Add --dry-run to any publishing command to see what would be sent without sending it.
"""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path(__file__).resolve().parent
ENV = HOME / ".env"
QUEUE = HOME / "queue.json"
LOG = HOME / "posted.log"
HOSTING = HOME / "hosting"
GRAPH = "https://graph.instagram.com"
CLOUD = os.environ.get("IG_CLOUD") == "1"


# ---------- config ----------

def load_env():
    env = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def save_env(updates):
    env = load_env()
    env.update(updates)
    ENV.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    os.chmod(ENV, 0o600)


def cfg(key, required=True):
    v = os.environ.get(key) or load_env().get(key, "")
    if required and not v:
        sys.exit(f"Missing {key} in {ENV}")
    return v


# ---------- http ----------

def api(method, path, params=None, base=None):
    version = cfg("IG_API_VERSION", required=False)
    root = base or (f"{GRAPH}/{version}" if version else GRAPH)
    params = dict(params or {})
    params["access_token"] = cfg("IG_TOKEN")
    url = f"{root}/{path.lstrip('/')}"
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            err = json.loads(body).get("error", {})
            msg = f"{err.get('type', 'Error')} {err.get('code', '')}: {err.get('message', body)}"
        except ValueError:
            msg = body[:500]
        raise SystemExit(f"API {method} {path} failed ({e.code}): {msg}")


def user_id():
    return cfg("IG_USER_ID")


# ---------- media prep + hosting ----------

def to_jpeg(path, feed=True):
    """The API accepts JPEG only. Convert anything else (PNG etc.) and enforce aspect limits.
    feed=False skips the 4:5-1.91:1 feed check (Reel covers are 9:16)."""
    from PIL import Image, ImageOps
    p = Path(path).expanduser()
    im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
    ratio = im.width / im.height
    if feed and not 0.8 - 1e-3 <= ratio <= 1.91 + 1e-3:
        sys.exit(f"{p.name}: aspect {ratio:.3f} is outside Instagram's 4:5 to 1.91:1 feed range")
    if im.width > 1440:
        im = im.resize((1440, round(1440 / ratio)), Image.LANCZOS)
    out = HOME / "outbox" / (p.stem + ".jpg")
    out.parent.mkdir(exist_ok=True)
    im.save(out, quality=95, subsampling=0)
    return out


def host(path, dry=False):
    """Publish a file to the media repo and return a public URL Meta can fetch."""
    repo = cfg("IG_MEDIA_REPO")
    path = Path(path)
    if CLOUD:
        owner, rname = repo.split("/")
        url = f"https://{owner}.github.io/{rname}/{path.as_posix()}"
        if not dry:
            wait_public(url)
        return url
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{path.name}"
    # GitHub Pages, not raw.githubusercontent: raw serves .mp4 as application/octet-stream, Pages as video/mp4.
    owner, rname = repo.split("/")
    url = f"https://{owner}.github.io/{rname}/media/{name}"
    if dry:
        return url
    if not (HOSTING / ".git").exists():
        subprocess.run(["gh", "repo", "clone", repo, str(HOSTING)], check=True)
    (HOSTING / "media").mkdir(exist_ok=True)
    shutil.copy(path, HOSTING / "media" / name)
    git = ["git", "-C", str(HOSTING)]
    subprocess.run(git + ["add", f"media/{name}"], check=True)
    subprocess.run(git + ["commit", "-qm", f"media: {name}"], check=True)
    subprocess.run(git + ["push", "-q", "origin", "HEAD:main"], check=True)
    wait_public(url)
    return url


def wait_public(url, tries=60):
    """Pages rebuilds after each push (about 1-2 min), so allow up to 5 minutes."""
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status == 200:
                    return
        except urllib.error.URLError:
            pass
        time.sleep(5)
    sys.exit(f"Hosted file never became reachable: {url}")


# ---------- publishing ----------

def wait_container(cid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = api("GET", cid, {"fields": "status_code,status"})
        code = st.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            sys.exit(f"Container {cid} failed: {st}")
        time.sleep(5)
    sys.exit(f"Container {cid} still processing after {timeout}s")


def publish(cid):
    res = api("POST", f"{user_id()}/media_publish", {"creation_id": cid})
    mid = res["id"]
    info = api("GET", mid, {"fields": "permalink,timestamp"})
    with LOG.open("a") as f:
        f.write(json.dumps({"media_id": mid, **info}) + "\n")
    print(f"Published: {info.get('permalink', mid)}")
    return mid


def read_caption(path):
    text = Path(path).expanduser().read_text().strip()
    if len(text) > 2200:
        sys.exit(f"Caption is {len(text)} chars; Instagram's limit is 2200")
    tags = [w for w in text.split() if w.startswith("#")]
    if len(tags) > 30:
        sys.exit(f"Caption has {len(tags)} hashtags; Instagram's limit is 30")
    if "—" in text:
        sys.exit("Caption contains an em dash; house style forbids them")
    return text


def do_post(image, caption, dry=False):
    jpg = Path(image) if CLOUD else to_jpeg(image)
    url = host(jpg, dry)
    if dry:
        print(json.dumps({"kind": "image", "image_url": url, "caption": caption}, indent=2))
        return
    cid = api("POST", f"{user_id()}/media", {"image_url": url, "caption": caption})["id"]
    wait_container(cid)
    return publish(cid)


def do_carousel(images, caption, dry=False):
    if not 2 <= len(images) <= 10:
        sys.exit("A carousel needs 2 to 10 items")
    urls = [host(Path(i) if CLOUD else to_jpeg(i), dry) for i in images]
    if dry:
        print(json.dumps({"kind": "carousel", "image_urls": urls, "caption": caption}, indent=2))
        return
    kids = []
    for u in urls:
        kid = api("POST", f"{user_id()}/media", {"image_url": u, "is_carousel_item": "true"})["id"]
        wait_container(kid)
        kids.append(kid)
    cid = api("POST", f"{user_id()}/media", {"media_type": "CAROUSEL", "children": ",".join(kids), "caption": caption})["id"]
    wait_container(cid)
    return publish(cid)


def do_reel(video, caption, cover=None, dry=False):
    vurl = host(Path(video).expanduser(), dry)
    params = {"media_type": "REELS", "video_url": vurl, "caption": caption, "share_to_feed": "true"}
    if cover:
        params["cover_url"] = host(Path(cover) if CLOUD else to_jpeg(cover, feed=False), dry)
    if dry:
        print(json.dumps(params, indent=2))
        return
    cid = api("POST", f"{user_id()}/media", params)["id"]
    wait_container(cid, timeout=1200)
    return publish(cid)


# ---------- queue ----------

def queue_load():
    return json.loads(QUEUE.read_text()) if QUEUE.exists() else []


def queue_save(q):
    QUEUE.write_text(json.dumps(q, indent=2))


def queue_add(when, kind, files, caption_file):
    dt.datetime.strptime(when, "%Y-%m-%d %H:%M")
    read_caption(caption_file)  # validate now, not at 3am
    for f in files:
        if not Path(f).expanduser().exists():
            sys.exit(f"Missing file: {f}")
    q = queue_load()
    q.append({"when": when, "kind": kind, "files": [str(Path(f).expanduser()) for f in files],
              "caption_file": str(Path(caption_file).expanduser()), "status": "pending"})
    q.sort(key=lambda x: x["when"])
    queue_save(q)
    print(f"Queued {kind} for {when} ({len(q)} items in queue)")


GRACE_HOURS = 6  # a post more than this late is marked stale instead of firing in a backlog burst


def already_posted(caption):
    """True if one of the account's latest posts has this exact caption (guards against double posts
    when a run published but its status update was lost)."""
    try:
        recent = api("GET", f"{user_id()}/media", {"fields": "caption", "limit": 10}).get("data", [])
    except SystemExit:
        return False
    return any((m.get("caption") or "").strip() == caption.strip() for m in recent)


def queue_run(dry=False):
    if not CLOUD and cfg("IG_POSTING", required=False) == "cloud":
        print("Posting runs on GitHub (IG_POSTING=cloud); the Mac only syncs. Use sync-cloud.")
        return
    if not dry:
        if not cfg("IG_TOKEN", required=False):
            print("Account not connected yet (IG_TOKEN empty); nothing posted")
            return
        if not cfg("IG_USER_ID", required=False):
            # First run after the user pastes a token: look up and store the account id.
            me = api("GET", "me", {"fields": "user_id,username,account_type"})
            save_env({"IG_USER_ID": me["user_id"]})
            print(f"Connected @{me.get('username')} ({me.get('account_type')})")
    q = queue_load()
    now_dt = dt.datetime.now()
    now = now_dt.strftime("%Y-%m-%d %H:%M")
    cutoff = (now_dt - dt.timedelta(hours=GRACE_HOURS)).strftime("%Y-%m-%d %H:%M")
    for x in q:
        if x["status"] == "pending" and x["when"] < cutoff:
            x["status"] = "stale"
            print(f"Marked stale (missed by more than {GRACE_HOURS}h): {x['when']} {x['files'][0]}")
    if not dry:
        queue_save(q)
    stuck = [x for x in q if x["status"] == "posting"]
    for x in stuck:
        print(f"WARNING: {x['when']} {Path(x['files'][0]).name} was interrupted mid-post; check the profile, then set its status")
    due = [x for x in q if x["status"] == "pending" and x["when"] <= now]
    if not due:
        print("Nothing due")
        return
    item = due[0]  # one post per run keeps spacing natural and failures isolated
    cap = read_caption(item["caption_file"])
    if not dry and already_posted(cap):
        item["status"] = "posted"
        queue_save(q)
        print(f"Already on the profile, marked posted: {item['when']} {Path(item['files'][0]).name}")
        return
    if not dry:
        # Claim it first: if we crash after publishing but before recording, it must not be retried.
        item["status"] = "posting"
        queue_save(q)
    try:
        if item["kind"] == "image":
            mid = do_post(item["files"][0], cap, dry)
        elif item["kind"] == "carousel":
            mid = do_carousel(item["files"], cap, dry)
        elif item["kind"] == "reel":
            mid = do_reel(item["files"][0], cap, item["files"][1] if len(item["files"]) > 1 else None, dry)
        else:
            sys.exit(f"Unknown kind {item['kind']}")
    except SystemExit as e:
        if not dry:
            item["status"], item["error"] = "failed", str(e)
            queue_save(q)
        raise
    if not dry:
        item["status"], item["media_id"] = "posted", mid
        queue_save(q)


# ---------- analytics + community ----------

def insights(days):
    until = int(time.time())
    since = until - days * 86400
    metrics = "reach,views,accounts_engaged,total_interactions,likes,comments,shares,saves,follows_and_unfollows,profile_links_taps"
    res = api("GET", f"{user_id()}/insights", {"metric": metrics, "period": "day", "metric_type": "total_value",
                                               "since": since, "until": until})
    me = api("GET", "me", {"fields": "username,followers_count,follows_count,media_count"})
    print(f"@{me.get('username')}  followers {me.get('followers_count')}  posts {me.get('media_count')}  (last {days} days)")
    for m in res.get("data", []):
        tv = m.get("total_value", {})
        print(f"  {m['name']:24s} {tv.get('value', '')}")
        for b in tv.get("breakdowns", []):
            for r in b.get("results", []):
                print(f"      {','.join(r.get('dimension_values', []))}: {r.get('value')}")


def recent_media(limit):
    return api("GET", f"{user_id()}/media", {"fields": "id,caption,media_type,permalink,timestamp,like_count,comments_count",
                                              "limit": limit}).get("data", [])


def media_insights(limit):
    rows = []
    for m in recent_media(limit):
        metric = "reach,views,likes,comments,saved,shares,total_interactions"
        try:
            ins = {d["name"]: d["values"][0]["value"] for d in api("GET", f"{m['id']}/insights", {"metric": metric})["data"]}
        except SystemExit as e:
            ins = {"error": str(e)[:80]}
        rows.append({"when": m["timestamp"][:16], "type": m["media_type"], **ins,
                     "caption": (m.get("caption") or "")[:40].replace("\n", " "), "link": m["permalink"]})
    print(json.dumps(rows, indent=2))


def comments(limit):
    for m in recent_media(limit):
        cs = api("GET", f"{m['id']}/comments", {"fields": "id,text,username,timestamp,replies{id,text,username}"}).get("data", [])
        if not cs:
            continue
        print(f"\n{m['permalink']}")
        for c in cs:
            replied = " [replied]" if c.get("replies", {}).get("data") else ""
            print(f"  {c['id']}  @{c.get('username')}: {c.get('text')}{replied}")


def reply(comment_id, text):
    if "—" in text:
        sys.exit("Reply contains an em dash; house style forbids them")
    res = api("POST", f"{comment_id}/replies", {"message": text})
    print(f"Replied: {res.get('id')}")


def notify(title, body, subtitle=""):
    """macOS notification; quietly skipped when not running in a GUI session."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    sub = f' subtitle "{esc(subtitle)}"' if subtitle else ""
    subprocess.run(["osascript", "-e", f'display notification "{esc(body)}" with title "{esc(title)}"{sub}'],
                   capture_output=True)


def checkin(days=7):
    try:
        _checkin(days)
    except (urllib.error.URLError, OSError) as e:
        notify("Instagram check-in", "Couldn't reach Instagram (Mac offline?). Will retry tomorrow.")
        print(f"checkin failed: {e}")


def _checkin(days=7):
    """Daily check: confirm what published, pull stats for recent posts, flag queue problems,
    write reports/YYYY-MM-DD.md and send a one-line Mac notification."""
    if not cfg("IG_TOKEN", required=False):
        return
    posted = [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()] if LOG.exists() else []
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
    recent = [p for p in posted if p.get("timestamp", "") >= cutoff]
    me = api("GET", "me", {"fields": "username,followers_count,media_count"})
    lines, short = [f"# Check-in {dt.date.today()}", "",
                    f"@{me['username']}: {me['followers_count']} followers, {me['media_count']} posts", ""], []
    for p in recent:
        try:
            ins = {d["name"]: d["values"][0]["value"] for d in
                   api("GET", f"{p['media_id']}/insights", {"metric": "reach,likes,comments,saved,shares"})["data"]}
        except SystemExit as e:
            ins = {"error": str(e)[:60]}
        when = p["timestamp"][:16].replace("T", " ")
        stats = ", ".join(f"{k} {v}" for k, v in ins.items())
        lines.append(f"- {when}: {stats}  {p.get('permalink', '')}")
        short.append(f"reach {ins.get('reach', '?')}, saves {ins.get('saved', '?')}")
    problems = [x for x in queue_load() if x["status"] in ("failed", "stale", "posting")]
    for x in problems:
        lines.append(f"- PROBLEM: {x['status']} {x['when']} {Path(x['files'][0]).name} {x.get('error', '')[:80]}")
    nxt = next((x for x in queue_load() if x["status"] == "pending"), None)
    if nxt:
        lines.append(f"\nNext: {nxt['when']} {nxt['kind']} {Path(nxt['files'][0]).name}")
    rep = HOME / "reports"
    rep.mkdir(exist_ok=True)
    (rep / f"{dt.date.today()}.md").write_text("\n".join(lines) + "\n")
    if problems:
        body = f"{len(problems)} queue problem(s). Ask Claude to check Instagram."
    elif short:
        body = f"{me['followers_count']} followers. Latest post: {short[-1]}"
    else:
        body = f"{me['followers_count']} followers. Nothing posted in the last {days} days."
    nudge = pick_nudge(recent, problems)
    if nudge:
        lines.append(f"\nNudge: {nudge}")
    notify("Instagram check-in", body, subtitle=nudge)
    print("\n".join(lines))


def pick_nudge(recent, problems):
    """One prompt for the user to bring back to Claude, most useful first. The user asked for these
    reminders on 2026-10-25: 'check Instagram', 'weekly review', and sending new photos."""
    state_f = HOME / "reports" / ".state.json"
    state = json.loads(state_f.read_text()) if state_f.exists() else {}
    comments = 0
    for p in recent:
        try:
            comments += api("GET", p["media_id"], {"fields": "comments_count"}).get("comments_count", 0)
        except SystemExit:
            pass
    new_comments = comments - state.get("comments", 0)
    state["comments"] = comments
    state_f.write_text(json.dumps(state))
    pending = [x for x in queue_load() if x["status"] == "pending"]
    week_ahead = (dt.datetime.now() + dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    own = [x for x in pending if x["when"] <= week_ahead
           and any("/originals/" in f or "/me_" in f or "desk-editorial" in f for f in x["files"])]
    today = dt.date.today()
    if problems:
        return "Say \"check Instagram\" to Claude."
    if new_comments > 0:
        return f"{new_comments} new comment(s). Reply in the app, or say \"check Instagram\"."
    if today.weekday() == 0:
        return f"Monday: say \"weekly review\" to Claude. {len(pending)} posts left in the queue."
    if len(pending) < 7:
        return f"Only {len(pending)} posts left. Say \"weekly review\" to plan the next batch."
    if len(own) < 2 or today.weekday() == 3:
        return "Took any photos? Paste them to Claude to edit for the feed."
    if today.weekday() in (2, 5):
        return "Say \"check Instagram\" to see how posts are doing."
    return ""


def item_id(x):
    return f"{x['when']}|{Path(x['files'][0]).name}"


RUNNER_STATUSES = ("posted", "posting", "stale", "failed")


def sync_cloud():
    """Mirror the local queue into the media repo for the GitHub runner, and pull back its results.

    Local queue.json stays the plan of record; the runner owns status changes after an item is pending.
    """
    import re
    repo = cfg("IG_MEDIA_REPO")
    if not (HOSTING / ".git").exists():
        subprocess.run(["gh", "repo", "clone", repo, str(HOSTING)], check=True)
    git = ["git", "-C", str(HOSTING)]
    subprocess.run(git + ["pull", "-q", "--rebase", "--autostash"], check=True)
    cloud_q = HOSTING / "queue.json"
    cloud = {x.get("id") or item_id(x): x for x in (json.loads(cloud_q.read_text()) if cloud_q.exists() else [])}
    local = queue_load()
    for x in local:
        c = cloud.get(item_id(x))
        # Only pull back statuses the runner itself sets; Mac-side holds (paused, backlog) are not runner results,
        # so a stale "paused" in the cloud copy must not undo a local restore.
        if c and c["status"] in RUNNER_STATUSES and x["status"] in ("pending", "posting"):
            x["status"] = c["status"]
            for k in ("media_id", "error"):
                if k in c:
                    x[k] = c[k]
    queue_save(local)
    # Merge the runner's publish log into the local one (checkin reads it).
    if (HOSTING / "posted.log").exists():
        seen = set(LOG.read_text().splitlines()) if LOG.exists() else set()
        new = [l for l in (HOSTING / "posted.log").read_text().splitlines() if l.strip() and l not in seen]
        if new:
            with LOG.open("a") as f:
                f.write("\n".join(new) + "\n")
    (HOSTING / "queue_media").mkdir(exist_ok=True)
    (HOSTING / "queue_captions").mkdir(exist_ok=True)
    out = []
    for x in local:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", item_id(x)).strip("-")
        entry = {k: v for k, v in x.items() if k not in ("files", "caption_file")}
        entry["id"] = item_id(x)
        if x["status"] != "pending":
            c = cloud.get(item_id(x))
            entry["files"] = c["files"] if c else []
            entry["caption_file"] = c.get("caption_file", "") if c else ""
            out.append(entry)
            continue
        files = []
        for i, f in enumerate(x["files"]):
            src = Path(f)
            if src.suffix.lower() in (".mp4", ".mov"):
                dst = HOSTING / "queue_media" / f"{slug}-{i}{src.suffix.lower()}"
                if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
                    shutil.copy(src, dst)
            else:
                is_cover = x["kind"] == "reel" and i > 0
                dst = HOSTING / "queue_media" / f"{slug}-{i}.jpg"
                # Re-upload when the source was edited after the last sync (re-rendered designs).
                if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
                    shutil.copy(to_jpeg(src, feed=not is_cover), dst)
            files.append(dst.relative_to(HOSTING).as_posix())
        cap = HOSTING / "queue_captions" / f"{slug}.txt"
        cap.write_text(read_caption(x["caption_file"]) + "\n")
        entry["files"] = files
        entry["caption_file"] = cap.relative_to(HOSTING).as_posix()
        out.append(entry)
    cloud_q.write_text(json.dumps(out, indent=2) + "\n")
    shutil.copy(Path(__file__), HOSTING / "ig.py")
    subprocess.run(git + ["add", "-A", "queue.json", "queue_media", "queue_captions", "ig.py"], check=True)
    if subprocess.run(git + ["diff", "--cached", "--quiet"]).returncode:
        subprocess.run(git + ["commit", "-qm", "queue: sync from Mac"], check=True)
        subprocess.run(git + ["push", "-q", "origin", "HEAD:main"], check=True)
        print(f"Synced {sum(1 for e in out if e['status'] == 'pending')} pending item(s) to GitHub")
    else:
        print("Cloud queue already up to date")
    # GitHub's cron is best-effort (runs are often late or skipped), so when the Mac is awake and
    # something is due, trigger the runner directly. GitHub stays the only publisher.
    now = dt.datetime.now()
    window = (now - dt.timedelta(hours=GRACE_HOURS)).strftime("%Y-%m-%d %H:%M")
    due = [x for x in local if x["status"] == "pending" and window <= x["when"] <= now.strftime("%Y-%m-%d %H:%M")]
    if due:
        r = subprocess.run(["gh", "workflow", "run", "post.yml", "-R", repo], capture_output=True, text=True)
        print(f"Triggered runner for {len(due)} due item(s)" if r.returncode == 0 else f"Trigger failed: {r.stderr.strip()}")


def refresh_token():
    res = api("GET", "refresh_access_token", {"grant_type": "ig_refresh_token"}, base=GRAPH)
    save_env({"IG_TOKEN": res["access_token"]})
    days = int(res.get("expires_in", 0)) // 86400
    print(f"Token refreshed; valid for about {days} days")


# ---------- cli ----------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("whoami"); s.add_argument("--save", action="store_true")
    sub.add_parser("limit")
    s = sub.add_parser("post"); s.add_argument("image"); s.add_argument("--caption-file", required=True); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("carousel"); s.add_argument("images", nargs="+"); s.add_argument("--caption-file", required=True); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("reel"); s.add_argument("video"); s.add_argument("--caption-file", required=True); s.add_argument("--cover"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("queue-add"); s.add_argument("when"); s.add_argument("kind", choices=["image", "carousel", "reel"]); s.add_argument("files", nargs="+"); s.add_argument("--caption-file", required=True)
    sub.add_parser("queue-list")
    s = sub.add_parser("queue-run"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("insights"); s.add_argument("--days", type=int, default=7)
    s = sub.add_parser("media-insights"); s.add_argument("--limit", type=int, default=12)
    s = sub.add_parser("comments"); s.add_argument("--limit", type=int, default=6)
    s = sub.add_parser("reply"); s.add_argument("comment_id"); s.add_argument("text")
    sub.add_parser("refresh-token")
    s = sub.add_parser("checkin"); s.add_argument("--days", type=int, default=7)
    sub.add_parser("sync-cloud")
    a = p.parse_args()

    if a.cmd == "whoami":
        me = api("GET", "me", {"fields": "user_id,username,account_type,followers_count,media_count"})
        print(json.dumps(me, indent=2))
        if a.save:
            save_env({"IG_USER_ID": me["user_id"]})
            print(f"Saved IG_USER_ID to {ENV}")
    elif a.cmd == "limit":
        print(json.dumps(api("GET", f"{user_id()}/content_publishing_limit", {"fields": "quota_usage,config"}), indent=2))
    elif a.cmd == "post":
        do_post(a.image, read_caption(a.caption_file), a.dry_run)
    elif a.cmd == "carousel":
        do_carousel(a.images, read_caption(a.caption_file), a.dry_run)
    elif a.cmd == "reel":
        do_reel(a.video, read_caption(a.caption_file), a.cover, a.dry_run)
    elif a.cmd == "queue-add":
        queue_add(a.when, a.kind, a.files, a.caption_file)
    elif a.cmd == "queue-list":
        for x in queue_load():
            print(f"{x['when']}  {x['status']:8s} {x['kind']:8s} {', '.join(Path(f).name for f in x['files'])}")
    elif a.cmd == "queue-run":
        queue_run(a.dry_run)
    elif a.cmd == "insights":
        insights(a.days)
    elif a.cmd == "media-insights":
        media_insights(a.limit)
    elif a.cmd == "comments":
        comments(a.limit)
    elif a.cmd == "reply":
        reply(a.comment_id, a.text)
    elif a.cmd == "refresh-token":
        refresh_token()
    elif a.cmd == "checkin":
        checkin(a.days)
    elif a.cmd == "sync-cloud":
        sync_cloud()


if __name__ == "__main__":
    main()

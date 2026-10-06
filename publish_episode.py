#!/usr/bin/env python3
"""Publish a podcast episode to a static (GitHub Pages) RSS feed.

  publish_episode.py add EPISODE.mp3 --title "Title" --description "Text" [--slug S] [--date ISO8601] [--no-push]
  publish_episode.py remove FILE|SLUG|GUID [--keep-file] [--no-push]   # drop an episode (and its stored MP3)
  publish_episode.py migrate [--no-push]    # upload any MP3s still in local episodes/ to object storage
  publish_episode.py build [--no-push]      # regenerate feed.xml from episodes.json only

Audio lives in object storage (Cloudflare R2 / any S3-compatible bucket), configured in
podcast.json under "storage"; only the small feed (feed.xml, episodes.json, cover) lives in git.
`add` uploads the MP3 (Content-Type audio/mpeg, long Cache-Control), checks the public URL,
records it in episodes.json (byte length, duration, guid, pubDate), regenerates feed.xml,
then git commit + push. Without a "storage" block it falls back to copying into episodes/.

Credentials come only from the environment at runtime and are never written anywhere:
  R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID   (endpoint https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com)
Requires: python3, boto3, ffprobe (ffmpeg), curl, git with push access.
"""
import argparse, datetime as dt, email.utils, json, os, re, shutil, subprocess, sys, uuid
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(ROOT, "podcast.json")
MANIFEST = os.path.join(ROOT, "episodes.json")
EP_DIR = os.path.join(ROOT, "episodes")
MAX_BYTES = 95 * 1024 * 1024  # GitHub hard limit is 100 MB per file


def load(path, default):
    return json.load(open(path)) if os.path.exists(path) else default


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "episode"


def duration_seconds(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    return int(round(float(out.stdout.strip())))


def hms(sec):
    h, r = divmod(sec, 3600); m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------- object storage (S3-compatible, e.g. Cloudflare R2) ----------
def storage_conf(conf):
    st = conf.get("storage")
    return st if st and st.get("bucket") else None


def media_key(st, name):
    return f"{st.get('prefix', 'episodes/')}{name}"


def media_url(conf, name):
    st = storage_conf(conf)
    if st:
        return f"{st['public_base_url'].rstrip('/')}/{media_key(st, name)}"
    return f"{conf['base_url'].rstrip('/')}/episodes/{name}"


def s3_client(st):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        sys.exit("boto3 is required for object storage: python3 -m pip install --user boto3")
    env = st.get("credentials_env", {})
    ak, sk, acct = (os.environ.get(env.get(k, d)) for k, d in
                    (("access_key_id", "R2_ACCESS_KEY_ID"), ("secret_access_key", "R2_SECRET_ACCESS_KEY"),
                     ("account_id", "R2_ACCOUNT_ID")))
    endpoint = st.get("endpoint") or (f"https://{acct}.r2.cloudflarestorage.com" if acct else None)
    if not (ak and sk and endpoint):
        sys.exit("storage credentials missing: set R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_ACCOUNT_ID")
    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=ak, aws_secret_access_key=sk,
                        region_name=st.get("region", "auto"),
                        config=Config(signature_version="s3v4", retries={"max_attempts": 5}))


def remote_size(cli, st, name):
    try:
        return cli.head_object(Bucket=st["bucket"], Key=media_key(st, name))["ContentLength"]
    except Exception as e:
        if getattr(e, "response", {}).get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def upload_media(conf, src, name):
    st = storage_conf(conf); cli = s3_client(st); size = os.path.getsize(src)
    if remote_size(cli, st, name) == size:
        print(f"already in storage: {media_key(st, name)}")
    else:
        cli.upload_file(src, st["bucket"], media_key(st, name), ExtraArgs={
            "ContentType": "audio/mpeg",
            "CacheControl": st.get("cache_control", "public, max-age=31536000, immutable")})
        if remote_size(cli, st, name) != size: sys.exit(f"upload size mismatch for {name}")
        print(f"uploaded {media_key(st, name)} ({size} bytes)")
    check_public(media_url(conf, name), size)


def check_public(url, size, tries=10):
    import time
    for _ in range(tries):
        out = subprocess.run(["curl", "-sI", "-o", "/dev/null", "-w", "%{http_code} %header{content-length}",
                              url], capture_output=True, text=True).stdout.split()
        if out and out[0] == "200" and len(out) > 1 and int(out[1]) == size:
            return
        time.sleep(3)
    sys.exit(f"public URL check failed for {url}: {out}")


def delete_media(conf, name):
    st = storage_conf(conf); cli = s3_client(st)
    cli.delete_object(Bucket=st["bucket"], Key=media_key(st, name))
    print(f"deleted {media_key(st, name)} from storage")


def build_feed(conf, eps):
    tz = ZoneInfo(conf.get("timezone", "UTC"))
    base = conf["base_url"].rstrip("/")
    t = lambda s: escape(str(s))
    now = email.utils.format_datetime(dt.datetime.now(tz))
    items = []
    for e in sorted(eps, key=lambda e: e["pub_date"], reverse=True):
        pub = email.utils.format_datetime(dt.datetime.fromisoformat(e["pub_date"]).astimezone(tz))
        url = media_url(conf, e["file"])
        items.append(f"""    <item>
      <title>{t(e['title'])}</title>
      <description>{t(e['description'])}</description>
      <itunes:summary>{t(e['description'])}</itunes:summary>
      <enclosure url="{t(url)}" length="{e['length']}" type="audio/mpeg"/>
      <guid isPermaLink="false">{t(e['guid'])}</guid>
      <pubDate>{pub}</pubDate>
      <itunes:duration>{hms(e['duration'])}</itunes:duration>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:explicit>false</itunes:explicit>
    </item>""")
    block = "\n    <itunes:block>Yes</itunes:block>" if conf.get("block", True) else ""
    cover = f"{base}/{conf['cover']}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{t(conf['title'])}</title>
    <link>{t(base)}/</link>
    <atom:link href="{t(base)}/feed.xml" rel="self" type="application/rss+xml"/>
    <description>{t(conf['description'])}</description>
    <language>{t(conf.get('language', 'en-us'))}</language>
    <lastBuildDate>{now}</lastBuildDate>
    <itunes:author>{t(conf['author'])}</itunes:author>
    <itunes:summary>{t(conf['description'])}</itunes:summary>
    <itunes:type>episodic</itunes:type>
    <itunes:explicit>false</itunes:explicit>{block}
    <itunes:category text="{t(conf.get('category', 'Technology'))}"/>
    <itunes:image href="{t(cover)}"/>
    <image>
      <url>{t(cover)}</url>
      <title>{t(conf['title'])}</title>
      <link>{t(base)}/</link>
    </image>
{chr(10).join(items)}
  </channel>
</rss>
"""


def write_feed():
    conf = load(CONF, None)
    if not conf: sys.exit(f"missing {CONF}")
    eps = load(MANIFEST, [])
    feed = build_feed(conf, eps)
    import xml.dom.minidom; xml.dom.minidom.parseString(feed.encode())  # sanity: well-formed
    open(os.path.join(ROOT, "feed.xml"), "w", encoding="utf-8").write(feed)
    return len(eps)


def git(*args):
    subprocess.run(["git", "-C", ROOT, *args], check=True)


def commit_push(msg, push=True):
    git("add", "-A")
    if subprocess.run(["git", "-C", ROOT, "diff", "--cached", "--quiet"]).returncode == 0:
        print("nothing to commit"); return
    git("commit", "-q", "-m", msg)
    if push: git("push", "-q", "origin", "HEAD")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("mp3"); a.add_argument("--title", required=True)
    a.add_argument("--description", required=True); a.add_argument("--date", help="ISO 8601; default now")
    a.add_argument("--slug"); a.add_argument("--no-push", action="store_true")
    m = sub.add_parser("migrate"); m.add_argument("--no-push", action="store_true")
    r = sub.add_parser("remove"); r.add_argument("episode", help="file name, slug (file without .mp3) or guid")
    r.add_argument("--keep-file", action="store_true", help="leave the MP3 in storage (and in local episodes/)")
    r.add_argument("--no-push", action="store_true")
    b = sub.add_parser("build"); b.add_argument("--no-push", action="store_true")
    args = ap.parse_args()
    if args.cmd == "add":
        src = os.path.abspath(args.mp3)
        size = os.path.getsize(src)
        if size > MAX_BYTES: sys.exit(f"{src} is {size/1e6:.1f} MB; GitHub limit is 100 MB per file")
        conf = load(CONF, {}); tz = ZoneInfo(conf.get("timezone", "UTC"))
        eps = load(MANIFEST, [])
        st = storage_conf(conf)
        base = args.slug or slugify(args.title); name = f"{base}.mp3"; n = 2
        while any(e["file"] == name for e in eps) or os.path.exists(os.path.join(EP_DIR, name)):
            name = f"{base}-{n}.mp3"; n += 1
        if st:
            cli = s3_client(st)
            while remote_size(cli, st, name) is not None:  # never overwrite another episode's audio
                name = f"{base}-{n}.mp3"; n += 1
            upload_media(conf, src, name)
        else:
            os.makedirs(EP_DIR, exist_ok=True)
            shutil.copy2(src, os.path.join(EP_DIR, name))
        when = dt.datetime.fromisoformat(args.date) if args.date else dt.datetime.now(tz)
        if when.tzinfo is None: when = when.replace(tzinfo=tz)
        eps.append({"guid": str(uuid.uuid4()), "title": args.title, "description": args.description,
                    "file": name, "length": size, "duration": duration_seconds(src),
                    "pub_date": when.isoformat()})
        json.dump(eps, open(MANIFEST, "w"), indent=2)
        write_feed()
        commit_push(f"Add episode: {args.title}", push=not args.no_push)
        print(f"published {media_url(conf, name)} ({size} bytes)")
    elif args.cmd == "remove":
        eps = load(MANIFEST, [])
        key = args.episode
        hits = [e for e in eps if key in (e["file"], e["guid"], os.path.splitext(e["file"])[0])]
        if len(hits) != 1: sys.exit(f"expected exactly one episode matching {key!r}, found {len(hits)}")
        e = hits[0]
        eps.remove(e)
        json.dump(eps, open(MANIFEST, "w"), indent=2)
        path = os.path.join(EP_DIR, e["file"])
        if not args.keep_file:
            if storage_conf(load(CONF, {})): delete_media(load(CONF, {}), e["file"])
            if os.path.exists(path): os.remove(path)
        write_feed()
        commit_push(f"Remove episode: {e['title']}", push=not args.no_push)
        print(f"removed {e['file']} ({e['title']}); {len(eps)} episodes remain")
    elif args.cmd == "migrate":
        conf = load(CONF, {})
        if not storage_conf(conf): sys.exit("no storage block in podcast.json")
        for e in load(MANIFEST, []):
            path = os.path.join(EP_DIR, e["file"])
            if os.path.exists(path):
                if os.path.getsize(path) != e["length"]: sys.exit(f"{path} size differs from episodes.json")
                upload_media(conf, path, e["file"])
            else:
                check_public(media_url(conf, e["file"]), e["length"])
        print(f"feed.xml rebuilt with {write_feed()} episodes (enclosures -> storage)")
        commit_push("Move episode audio to object storage", push=not args.no_push)
    else:
        print(f"feed.xml rebuilt with {write_feed()} episodes")
        commit_push("Rebuild feed", push=not args.no_push)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Publish a podcast episode to a static (GitHub Pages) RSS feed.

  publish_episode.py add EPISODE.mp3 --title "Title" --description "Text" [--date ISO8601] [--no-push]
  publish_episode.py remove FILE|SLUG|GUID [--keep-file] [--no-push]   # drop an episode + its MP3
  publish_episode.py build [--no-push]      # regenerate feed.xml from episodes.json only

Copies the MP3 into episodes/, records it in episodes.json (byte length, duration, guid,
pubDate), regenerates feed.xml from podcast.json + episodes.json, then git commit + push.
Requires: python3, ffprobe (ffmpeg), git with push access.
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


def build_feed(conf, eps):
    tz = ZoneInfo(conf.get("timezone", "UTC"))
    base = conf["base_url"].rstrip("/")
    t = lambda s: escape(str(s))
    now = email.utils.format_datetime(dt.datetime.now(tz))
    items = []
    for e in sorted(eps, key=lambda e: e["pub_date"], reverse=True):
        pub = email.utils.format_datetime(dt.datetime.fromisoformat(e["pub_date"]).astimezone(tz))
        url = f"{base}/episodes/{e['file']}"
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
    r = sub.add_parser("remove"); r.add_argument("episode", help="file name, slug (file without .mp3) or guid")
    r.add_argument("--keep-file", action="store_true", help="leave the MP3 in episodes/")
    r.add_argument("--no-push", action="store_true")
    b = sub.add_parser("build"); b.add_argument("--no-push", action="store_true")
    args = ap.parse_args()
    if args.cmd == "add":
        src = os.path.abspath(args.mp3)
        size = os.path.getsize(src)
        if size > MAX_BYTES: sys.exit(f"{src} is {size/1e6:.1f} MB; GitHub limit is 100 MB per file")
        conf = load(CONF, {}); tz = ZoneInfo(conf.get("timezone", "UTC"))
        eps = load(MANIFEST, [])
        base = args.slug or slugify(args.title); name = f"{base}.mp3"; n = 2
        while any(e["file"] == name for e in eps) or os.path.exists(os.path.join(EP_DIR, name)):
            name = f"{base}-{n}.mp3"; n += 1
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
        print(f"published episodes/{name} ({size} bytes)")
    elif args.cmd == "remove":
        eps = load(MANIFEST, [])
        key = args.episode
        hits = [e for e in eps if key in (e["file"], e["guid"], os.path.splitext(e["file"])[0])]
        if len(hits) != 1: sys.exit(f"expected exactly one episode matching {key!r}, found {len(hits)}")
        e = hits[0]
        eps.remove(e)
        json.dump(eps, open(MANIFEST, "w"), indent=2)
        path = os.path.join(EP_DIR, e["file"])
        if not args.keep_file and os.path.exists(path): os.remove(path)
        write_feed()
        commit_push(f"Remove episode: {e['title']}", push=not args.no_push)
        print(f"removed {e['file']} ({e['title']}); {len(eps)} episodes remain")
    else:
        print(f"feed.xml rebuilt with {write_feed()} episodes")
        commit_push("Rebuild feed", push=not args.no_push)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""AI Brief: fetch feeds, optionally summarize with Claude, render a phone-friendly page.

Standard library only. Optional environment variables:
  ANTHROPIC_API_KEY  enables the AI summary at the top of the page
  ANTHROPIC_MODEL    model for the summary (default: claude-haiku-5-5)
  NTFY_TOPIC         sends a morning push notification via ntfy.sh
  SITE_URL           your GitHub Pages URL (used as the notification tap target)
"""
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "site"
NOW = dt.datetime.now(dt.timezone.utc)
UA = "Mozilla/5.0 (AI-Brief personal reader; +https://github.com)"
ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
DC_DATE = "{http://purl.org/dc/elements/1.1/}date"


# ---------- fetching & parsing ----------

def fetch(url):
    if url.startswith("file://") or os.path.exists(url):  # used for local testing
        return Path(url.removeprefix("file://")).read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def clean(text, limit=400):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:limit]


def parse_feed(raw):
    root = ET.fromstring(raw)
    items = []
    if root.tag == f"{ATOM}feed":
        for e in root.findall(f"{ATOM}entry"):
            link = ""
            for l in e.findall(f"{ATOM}link"):
                if l.get("rel", "alternate") == "alternate":
                    link = l.get("href", "")
                    break
            items.append({
                "title": clean(e.findtext(f"{ATOM}title"), 200),
                "url": link,
                "date": parse_date(e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated")),
                "snippet": clean(e.findtext(f"{ATOM}summary") or e.findtext(f"{ATOM}content")),
            })
    else:
        for i in root.iter("item"):
            items.append({
                "title": clean(i.findtext("title"), 200),
                "url": (i.findtext("link") or "").strip(),
                "date": parse_date(i.findtext("pubDate") or i.findtext(DC_DATE)),
                "snippet": clean(i.findtext("description") or i.findtext(CONTENT)),
            })
    return items


def collect(config):
    sections, errors = [], []
    for sec in config["sections"]:
        cutoff = NOW - dt.timedelta(days=sec["days"])
        items = []
        for f in sec["feeds"]:
            try:
                fresh = [it for it in parse_feed(fetch(f["url"]))
                         if it["url"] and it["date"] and it["date"] >= cutoff]
                fresh.sort(key=lambda x: x["date"], reverse=True)
                for it in fresh[: f.get("max", sec.get("max_per_feed", 5))]:
                    it["source"] = f["name"]
                    it["id"] = hashlib.sha1(it["url"].encode()).hexdigest()[:12]
                    items.append(it)
            except Exception as ex:  # one bad feed should never break the brief
                errors.append(f"{f['name']}: {type(ex).__name__}")
                print(f"WARN {f['name']}: {ex}", file=sys.stderr)
        items.sort(key=lambda x: x["date"], reverse=True)
        sections.append({"name": sec["name"], "items": items})
    return sections, errors


# ---------- optional AI summary ----------

def summarize(sections):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    pool = [dict(section=s["name"], source=i["source"], title=i["title"],
                 url=i["url"], snippet=i["snippet"][:300])
            for s in sections for i in s["items"][:15]]
    if not pool:
        return None
    prompt = (
        "You are writing a 2-minute morning AI brief for a reader who already knows "
        "ML theory deeply. Skip basics. Focus on what changed: usage, industry, policy, "
        "economics, compute, and practical engineering.\n\n"
        "Return ONLY JSON: {\"headline\": str (<=12 words), \"bullets\": [3-5 strings, "
        "each <=30 words], \"must_read\": {\"title\": str, \"url\": str, \"why\": str (<=25 words)}}. "
        "must_read must be one of the provided URLs.\n\nItems:\n" + json.dumps(pool)
    )
    body = json.dumps({
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        "max_tokens": 800,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text = json.load(r)["content"][0]["text"]
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        valid = {p["url"] for p in pool}
        if data.get("must_read", {}).get("url") not in valid:
            data.pop("must_read", None)
        return data
    except Exception as ex:
        print(f"WARN summary failed: {ex}", file=sys.stderr)
        return None


def notify(summary, total):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    title = (summary or {}).get("headline") or f"AI Brief: {total} new items"
    msg = "\n".join("• " + b for b in (summary or {}).get("bullets", [])[:3]) or "Tap to open."
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=msg.encode(), method="POST",
        headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"),
                 "Click": os.environ.get("SITE_URL", ""), "Tags": "newspaper"})
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as ex:
        print(f"WARN notify failed: {ex}", file=sys.stderr)


# ---------- rendering ----------

E = html.escape


def render(sections, links, summary, errors):
    total = sum(len(s["items"]) for s in sections)
    parts = []
    if summary:
        bl = "".join(f"<li>{E(b)}</li>" for b in summary.get("bullets", []))
        mr = summary.get("must_read")
        mr_html = (f'<a class="must" href="{E(mr["url"])}" target="_blank" rel="noopener">'
                   f'<span class="lbl">Read in full</span><strong>{E(mr["title"])}</strong>'
                   f'<span>{E(mr.get("why", ""))}</span></a>') if mr else ""
        parts.append(f'<section class="card summary"><h2>{E(summary.get("headline", "Today in AI"))}</h2>'
                     f'<ul>{bl}</ul>{mr_html}</section>')
    for s in sections:
        rows = "".join(
            f'<li class="item" data-id="{i["id"]}"><a href="{E(i["url"])}" target="_blank" rel="noopener">'
            f'<span class="meta"><span class="src">{E(i["source"])}</span>'
            f'<time datetime="{i["date"].isoformat()}"></time></span>'
            f'<span class="title">{E(i["title"])}</span></a></li>'
            for i in s["items"]) or '<li class="empty">Nothing new.</li>'
        parts.append(f'<section class="sec"><div class="sec-head"><h2>{E(s["name"])}</h2>'
                     f'<button class="mark" type="button">Mark read</button></div><ul>{rows}</ul></section>')
    lk = "".join(f'<a class="chip" href="{E(l["url"])}" target="_blank" rel="noopener">{E(l["name"])}</a>'
                 for l in links)
    parts.append(f'<section class="sec"><div class="sec-head"><h2>Check-ins</h2></div><div class="chips">{lk}</div></section>')
    err = f'<p class="err">Could not load: {E(", ".join(errors))}</p>' if errors else ""
    tpl = (ROOT / "template.html").read_text()
    return (tpl.replace("{{BODY}}", "\n".join(parts))
               .replace("{{UPDATED}}", NOW.isoformat())
               .replace("{{TOTAL}}", str(total))
               .replace("{{ERRORS}}", err))


def main():
    config = json.loads((ROOT / os.environ.get("FEEDS_FILE", "feeds.json")).read_text())
    sections, errors = collect(config)
    summary = summarize(sections)
    OUT.mkdir(exist_ok=True)
    (OUT / "index.html").write_text(render(sections, config.get("links", []), summary, errors))
    notify(summary, sum(len(s["items"]) for s in sections))
    print(f"Built site/index.html: {sum(len(s['items']) for s in sections)} items, "
          f"summary={'yes' if summary else 'no'}, errors={len(errors)}")


if __name__ == "__main__":
    main()

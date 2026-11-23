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


def collect(section_configs):
    sections, errors = [], []
    for sec in section_configs:
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

def summarize(name, focus, sections):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    pool = [dict(section=s["name"], source=i["source"], title=i["title"],
                 url=i["url"], snippet=i["snippet"][:300])
            for s in sections for i in s["items"][:15]]
    if not pool:
        return None
    prompt = (
        f"You are writing a 2-minute morning brief for the \"{name}\" tab of a personal "
        f"news reader. {focus or 'Focus on what is new and important.'}\n\n"
        "Return ONLY JSON: {\"headline\": str (<=12 words), \"bullets\": [3-4 strings, "
        "each <=30 words], \"must_read\": {\"title\": str, \"url\": str, \"why\": str (<=25 words)}}. "
        "must_read must be one of the provided URLs.\n\nItems:\n" + json.dumps(pool)
    )
    body = json.dumps({
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        "max_tokens": 4000,  # includes the model's (hidden) thinking, so leave headroom
        "output_config": {"effort": "low"},  # simple summary: keep thinking short
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.load(r)
        # Replies can start with a "thinking" block; join only the text blocks.
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        if not text:
            raise ValueError(f"no text in response (stop_reason={resp.get('stop_reason')})")
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        valid = {p["url"] for p in pool}
        if data.get("must_read", {}).get("url") not in valid:
            data.pop("must_read", None)
        return data
    except Exception as ex:
        print(f"WARN {name} summary failed: {ex}", file=sys.stderr)
        return None


def notify(tabs, total):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    title = f"AI Brief: {total} new items"
    msg = "\n".join(f"• {t['name']}: {t['summary']['headline']}" for t in tabs
                    if (t["summary"] or {}).get("headline")) or f"{total} new items. Tap to open."
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


def slug(name, taken):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "tab"
    s, n = base, 2
    while s in taken:
        s, n = f"{base}-{n}", n + 1
    taken.add(s)
    return s


def render_tab(tab):
    summary, parts = tab["summary"], []
    if summary:
        bl = "".join(f"<li>{E(b)}</li>" for b in summary.get("bullets", [])[:4])
        mr = summary.get("must_read")
        mr_html = (f'<a class="must" href="{E(mr["url"])}" target="_blank" rel="noopener">'
                   f'<span class="must-label">Worth reading</span><span class="must-title">{E(mr["title"])}</span>'
                   f'<span class="must-why">{E(mr.get("why", ""))}</span></a>') if mr else ""
        parts.append(f'<section class="brief"><h2>{E(summary.get("headline", "Today in " + tab["name"]))}</h2>'
                     f'<ul>{bl}</ul>{mr_html}</section>')
    for s in tab["sections"]:
        rows = "".join(
            f'<li class="item" data-id="{i["id"]}"><a href="{E(i["url"])}" target="_blank" rel="noopener">'
            f'<span class="meta"><span class="src">{E(i["source"])}</span>'
            f'<time datetime="{i["date"].isoformat()}"></time></span>'
            f'<span class="title">{E(i["title"])}</span></a></li>'
            for i in s["items"]) or '<li class="empty">Nothing new.</li>'
        parts.append(f'<section class="sec"><div class="sec-head"><h2>{E(s["name"])}</h2>'
                     f'<button class="mark" type="button">Mark all read</button></div><ul>{rows}</ul></section>')
    if tab["links"]:
        lk = "".join(f'<a class="chip" href="{E(l["url"])}" target="_blank" rel="noopener">{E(l["name"])}</a>'
                     for l in tab["links"])
        parts.append(f'<section class="sec"><div class="sec-head"><h2>Links</h2></div><div class="chips">{lk}</div></section>')
    return "\n".join(parts)


def render(tabs, errors):
    total = sum(len(s["items"]) for t in tabs for s in t["sections"])
    nav = "".join(
        f'<button class="tab" type="button" role="tab" id="t-{t["id"]}" data-tab="{t["id"]}" '
        f'aria-controls="p-{t["id"]}" aria-selected="{"true" if i == 0 else "false"}">'
        f'{E(t["name"])}</button>'
        for i, t in enumerate(tabs))
    panels = "\n".join(
        f'<div class="panel" role="tabpanel" id="p-{t["id"]}" aria-labelledby="t-{t["id"]}"'
        f'{"" if i == 0 else " hidden"}>\n{render_tab(t)}\n</div>'
        for i, t in enumerate(tabs))
    body = (f'<nav class="tabs" role="tablist">{nav}</nav>\n' if len(tabs) > 1 else "") + panels
    err = f'<p class="err">Could not load: {E(", ".join(errors))}</p>' if errors else ""
    tpl = (ROOT / "template.html").read_text()
    return (tpl.replace("{{BODY}}", body)
               .replace("{{UPDATED}}", NOW.isoformat())
               .replace("{{TOTAL}}", str(total))
               .replace("{{ERRORS}}", err))


def load_tabs(config):
    """Tabbed config, or the original single-page format (shown as one 'AI' tab)."""
    if "tabs" in config:
        return config["tabs"]
    return [{"name": "AI", "sections": config["sections"], "links": config.get("links", [])}]


def main():
    config = json.loads((ROOT / os.environ.get("FEEDS_FILE", "feeds.json")).read_text())
    tabs, errors, taken = [], [], set()
    for t in load_tabs(config):
        sections, errs = collect(t["sections"])
        errors += errs
        tabs.append({"name": t["name"], "id": slug(t["name"], taken), "sections": sections,
                     "links": t.get("links", []),
                     "summary": summarize(t["name"], t.get("focus", ""), sections)})
    OUT.mkdir(exist_ok=True)
    (OUT / "index.html").write_text(render(tabs, errors))
    total = sum(len(s["items"]) for t in tabs for s in t["sections"])
    notify(tabs, total)
    print(f"Built site/index.html: {len(tabs)} tabs, {total} items, "
          f"summaries={sum(1 for t in tabs if t['summary'])}/{len(tabs)}, errors={len(errors)}")


if __name__ == "__main__":
    main()

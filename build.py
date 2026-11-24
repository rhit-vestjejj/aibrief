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
            title = clean(i.findtext("title"), 200)
            # News aggregators (e.g. Google News) name the original outlet in <source>
            # and append " - Outlet" to the title; show the outlet as the source instead.
            via = clean(i.findtext("source"), 60)
            if via and title.endswith(f" - {via}"):
                title = title[: -len(via) - 3]
            items.append({
                "title": title,
                "url": (i.findtext("link") or "").strip(),
                "date": parse_date(i.findtext("pubDate") or i.findtext(DC_DATE)),
                "snippet": clean(i.findtext("description") or i.findtext(CONTENT)),
                "via": via,
            })
    return items


def collect(section_configs):
    sections, errors = [], []
    for sec in section_configs:
        cutoff = NOW - dt.timedelta(hours=sec.get("hours", sec.get("days", 1) * 24))
        items = []
        for f in sec["feeds"]:
            try:
                fresh = [it for it in parse_feed(fetch(f["url"]))
                         if it["url"] and it["date"] and it["date"] >= cutoff]
                fresh.sort(key=lambda x: x["date"], reverse=True)
                for it in fresh[: f.get("max", sec.get("max_per_feed", 5))]:
                    it["source"] = it.get("via") or f["name"]
                    it["id"] = hashlib.sha1(it["url"].encode()).hexdigest()[:12]
                    items.append(it)
            except Exception as ex:  # one bad feed should never break the brief
                errors.append(f"{f['name']}: {type(ex).__name__}")
                print(f"WARN {f['name']}: {ex}", file=sys.stderr)
        items.sort(key=lambda x: x["date"], reverse=True)
        sections.append({"name": sec["name"], "top": sec.get("top"), "items": items})
    return sections, errors


# ---------- optional AI summary ----------

def ask_claude(prompt, label):
    """Send one prompt to Claude and return the JSON object in its reply."""
    body = json.dumps({
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        "max_tokens": 4000,  # includes the model's (hidden) thinking, so leave headroom
        "output_config": {"effort": "low"},  # simple tasks: keep thinking short
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.load(r)
    # Replies can start with a "thinking" block; join only the text blocks.
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    if not text:
        raise ValueError(f"no text in response (stop_reason={resp.get('stop_reason')})")
    u = resp.get("usage", {})
    print(f"{label}: {u.get('input_tokens')} input / {u.get('output_tokens')} output tokens")
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def summarize(name, focus, sections):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    pool, items = [], []  # items[id] = (section name, item)
    for s in sections:
        # Sections with "top" send every candidate so the model can pick the most important ones.
        for i in (s["items"] if s.get("top") else s["items"][:15]):
            pool.append(dict(id=len(items), section=s["name"], source=i["source"], title=i["title"],
                             hours_ago=round((NOW - i["date"]).total_seconds() / 3600, 1),
                             snippet=i["snippet"][:200]))
            items.append((s["name"], i))
    if not pool:
        return None
    top = {s["name"]: s["top"] for s in sections if s.get("top") and s["items"]}
    prompt = (
        f"You are writing a 2-minute morning brief for the \"{name}\" tab of a personal "
        f"news reader. {focus or 'Focus on what is new and important.'}\n\n"
        "Return ONLY JSON with these keys:\n"
        "- headline: string, at most 12 words\n"
        "- bullets: 3-4 strings, each at most 30 words\n"
        "- must_read: {\"id\": item id, \"why\": string of at most 25 words}\n")
    if top:
        prompt += (
            "- top: an object mapping section name to a list of item ids from that section, most "
            f"important first. Sections and maximum counts: {json.dumps(top)}. Pick what an informed "
            "reader most needs to know. Leave out deals and shopping, gift guides, minor product "
            "reviews, and listicles. When several items cover the same story, keep only the best one. "
            "When two items matter about equally, prefer the newer one.\n")
    prompt += "\nItems:\n" + json.dumps(pool, ensure_ascii=False)
    try:
        data = ask_claude(prompt, f"{name} summary")

        def lookup(n):  # model-supplied id -> (section, item), or None if invalid
            try:
                n = int(n)
            except (TypeError, ValueError):
                return None
            return items[n] if 0 <= n < len(items) else None

        mr = data.pop("must_read", None) or {}
        hit = lookup(mr.get("id"))
        if hit:
            data["must_read"] = {"title": hit[1]["title"], "url": hit[1]["url"], "why": mr.get("why", "")}
        picks = {}
        for sec, ids in (data.get("top") or {}).items():
            if sec in top and isinstance(ids, list):
                chosen, seen = [], set()
                for n in ids:
                    hit = lookup(n)
                    if hit and hit[0] == sec and hit[1]["id"] not in seen:
                        seen.add(hit[1]["id"])
                        chosen.append(hit[1])
                picks[sec] = chosen[: top[sec]]
        data["top"] = picks
        return data
    except Exception as ex:
        print(f"WARN {name} summary failed: {ex}", file=sys.stderr)
        return None


def notification(tabs, total):
    msg = "\n".join(f"• {t['name']}: {t['summary']['headline']}" for t in tabs
                    if (t["summary"] or {}).get("headline")) or f"{total} new items. Tap to open."
    return {"title": f"AI Brief: {total} new items", "message": msg}


def send_notification(note):
    """Post to ntfy. note: title, message, and optionally click (URL), tags, priority (1-5)."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    headers = {"Title": note["title"].encode("utf-8").decode("latin-1", "ignore"),
               "Click": note.get("click") or os.environ.get("SITE_URL", ""),
               "Tags": note.get("tags", "newspaper")}
    if note.get("priority"):
        headers["Priority"] = str(note["priority"])
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=note["message"].encode(), method="POST", headers=headers)
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
    tabs, errors, taken, ranked = [], [], set(), [0, 0]
    for t in load_tabs(config):
        sections, errs = collect(t["sections"])
        errors += errs
        summary = summarize(t["name"], t.get("focus", ""), sections)
        for s in sections:
            if s["top"] and s["items"]:
                ranked[1] += 1
                picks = ((summary or {}).get("top") or {}).get(s["name"])
                if picks:  # most important first; without a summary, keep everything newest first
                    s["items"] = picks
                    ranked[0] += 1
        tabs.append({"name": t["name"], "id": slug(t["name"], taken), "sections": sections,
                     "links": t.get("links", []), "summary": summary})
    OUT.mkdir(exist_ok=True)
    (OUT / "index.html").write_text(render(tabs, errors))
    total = sum(len(s["items"]) for t in tabs for s in t["sections"])
    note = notification(tabs, total)
    if os.environ.get("GITHUB_OUTPUT"):
        # In GitHub Actions, hand the message to the deploy job, which sends it once the page is live.
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"note={json.dumps(note)}\n")
    else:
        send_notification(note)
    print(f"Built site/index.html: {len(tabs)} tabs, {total} items, "
          f"summaries={sum(1 for t in tabs if t['summary'])}/{len(tabs)}, "
          f"ranked sections={ranked[0]}/{ranked[1]}, errors={len(errors)}")


if __name__ == "__main__":
    if sys.argv[1:] == ["--notify"]:  # the workflow's deploy job: send the message the build job prepared
        send_notification(json.loads(os.environ["NOTE"]))
    else:
        main()

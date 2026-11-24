#!/usr/bin/env python3
"""Breaking-news alerts: push a phone notification when something truly huge happens.

Run every ~15 minutes during the day (see README). A story alerts only if ALL of these hold:
  1. Breadth: at least `min_outlets` different outlets reported it in the last `hours` hours.
  2. Size: Claude scores it at least `min_score` out of 10 against a strict front-page rubric.
  3. Guardrails: not already alerted, under `max_alerts` in the last 12 hours, not quiet hours.
Settings live under "alerts" in feeds.json. Needs ANTHROPIC_API_KEY and NTFY_TOPIC.
Past alerts are read back from the ntfy topic itself (ntfy.sh keeps 12 hours of messages),
so no other storage is needed.

  python3 alerts.py          check now
  python3 alerts.py --test   send a sample alert, to see what one looks like on the phone
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

import build

TAG = "rotating_light"  # every real alert carries this tag; later runs look for it in the topic

RUBRIC = """Score each event from 1 to 10 for how big it is:
10 = historic: a market crash or a major index moving more than 5% in a day, a war starting, a world leader killed or removed, emergency central-bank action.
9 = huge: front-page news everywhere today, the kind people text each other about. For example: the CEO of a top-20 company fired, reinstated, or dying; a leading AI lab releasing a new frontier model; a major index moving more than 3%; a landmark Supreme Court ruling; a merger over $50 billion; a major terror attack or a disaster with mass casualties; an outage or hack hitting millions of people; a major quant fund or bank collapsing.
7-8 = big, but normal for a news cycle: large earnings surprises, $5-50 billion deals, major product launches, big policy proposals, notable executive departures, a top AI lab's smaller release.
1-6 = everything else.
Be strict: on most days nothing scores 9 or 10. Score the event itself, not how dramatic the headline sounds. Opinion, analysis, previews, and "what it means" pieces are not events."""


def outlet(source):
    """Rough outlet identity, so NYT US / NYT World / NYT count once ("nyt")."""
    s = re.sub(r"\.com\b|^the\s+", "", source.lower()).strip()
    return (s.split() or [s])[0]


def candidate_feeds(config):
    """Every feed from the tabs' short-window ('hours') sections, plus alerts.extra_feeds."""
    feeds = {f["url"]: f["name"] for t in build.load_tabs(config) for s in t["sections"]
             if "hours" in s for f in s["feeds"]}
    feeds.update({f["url"]: f["name"] for f in config["alerts"].get("extra_feeds", [])})
    return feeds


def recent_items(feeds, hours):
    cutoff = build.NOW - dt.timedelta(hours=hours)

    def one(url_name):
        url, name = url_name
        try:
            return [dict(i, source=i.get("via") or name) for i in build.parse_feed(build.fetch(url))
                    if i["url"] and i["date"] and i["date"] >= cutoff]
        except Exception as ex:
            print(f"WARN {name}: {ex}", file=sys.stderr)
            return []

    items, seen = [], set()
    with cf.ThreadPoolExecutor(12) as pool:
        for batch in pool.map(one, feeds.items()):
            for i in batch:
                if i["url"] not in seen:
                    seen.add(i["url"])
                    items.append(i)
    items.sort(key=lambda i: i["date"], reverse=True)
    return items


def sent_alerts(topic):
    """Alerts already sent in the last 12 hours, read back from the ntfy topic."""
    req = urllib.request.Request(f"https://ntfy.sh/{topic}/json?poll=1&since=12h&tags={TAG}",
                                 headers={"User-Agent": build.UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        events = [json.loads(line) for line in r.read().decode().splitlines() if line.strip()]
    return [e for e in events if e.get("event") == "message"]


def judge(items, sent, hours):
    pool = [dict(id=n, source=i["source"], hours_ago=round((build.NOW - i["date"]).total_seconds() / 3600, 1),
                 title=i["title"]) for n, i in enumerate(items)]
    prev = [e.get("title", "") for e in sent]
    prompt = (
        "You watch the news for a reader who follows AI, tech, markets, quant finance, and US and world "
        "news. Your job is to decide whether anything is huge enough to interrupt their day with a "
        "phone alert.\n\n" + RUBRIC + "\n\n"
        "Group the headlines that report the same event. Return ONLY JSON: {\"stories\": [{\"ids\": [ids of "
        "every headline about this event], \"best\": id of the most authoritative headline to open, "
        "\"headline\": plain and factual, at most 12 words, \"why\": at most 20 words on what happened and "
        "why it matters, \"score\": 1-10}]}, listing only events that score 7 or higher, highest first. "
        "An empty list is the normal answer.\n\n"
        "Alerts already sent in the last 12 hours. Never alert on the same event again unless there is a "
        f"major new development, such as a reversal: {json.dumps(prev, ensure_ascii=False)}\n\n"
        f"Headlines from the last {hours} hours:\n" + json.dumps(pool, ensure_ascii=False))
    return build.ask_claude(prompt, "alert check").get("stories") or []


def main():
    config = json.loads((build.ROOT / os.environ.get("FEEDS_FILE", "feeds.json")).read_text())
    a = config["alerts"]
    if not (os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("NTFY_TOPIC")):
        print("Alerts need ANTHROPIC_API_KEY and NTFY_TOPIC; nothing to do.")
        return
    local = build.NOW.astimezone(ZoneInfo(a.get("timezone", "America/Chicago")))
    quiet_from, quiet_to = a.get("quiet_hours", [23, 6])
    if local.hour >= quiet_from or local.hour < quiet_to:
        print(f"Quiet hours ({local:%H:%M} local); not checking.")
        return
    try:
        sent = sent_alerts(os.environ["NTFY_TOPIC"])
    except Exception as ex:  # without history we could repeat an alert, so stay silent this round
        print(f"WARN could not read past alerts ({ex}); skipping this round.", file=sys.stderr)
        return
    room = a.get("max_alerts", 3) - len(sent)
    if room <= 0:
        print(f"Already sent {len(sent)} alerts in the last 12 hours; not checking.")
        return

    hours = a.get("hours", 6)
    items = recent_items(candidate_feeds(config), hours)
    print(f"{len(items)} headlines from {len({outlet(i['source']) for i in items})} outlets in the last {hours}h; "
          f"{len(sent)} alert(s) already sent in the last 12h")
    if not items:
        return
    sent_urls = {e.get("click") for e in sent}

    for s in judge(items, sent, hours):
        ids = [int(n) for n in s.get("ids", []) if str(n).isdigit() and int(n) < len(items)]
        if not ids:
            continue
        group = [items[n] for n in ids]
        outlets = []
        for i in group:
            if outlet(i["source"]) not in [outlet(o) for o in outlets]:
                outlets.append(i["source"])
        best = items[int(s["best"])] if str(s.get("best", "")).isdigit() and int(s["best"]) in ids else group[0]
        try:
            score = int(float(s.get("score", 0)))
        except (TypeError, ValueError):
            score = 0
        ok = (score >= a.get("min_score", 9) and len(outlets) >= a.get("min_outlets", 3)
              and not any(i["url"] in sent_urls for i in group) and room > 0)
        print(f"{'ALERT' if ok else 'skip '} {score}/10, {len(outlets)} outlets: {s.get('headline', '')}")
        if ok:
            named = ", ".join(outlets[:3]) + (f" and {len(outlets) - 3} more" if len(outlets) > 3 else "")
            build.send_notification({
                "title": s.get("headline") or best["title"],
                "message": f"{s.get('why', '').strip()}\nReported by {named}.",
                "click": best["url"], "tags": TAG, "priority": 5})
            room -= 1


def send_test():
    build.send_notification({
        "title": "Test alert: this is what breaking news looks like",
        "message": "Real alerts only fire for huge stories covered by several outlets. Tap to open the brief.",
        "tags": "test_tube", "priority": 5})  # not tagged as an alert, so it never counts toward the cap
    print("Sent a test alert.")


if __name__ == "__main__":
    send_test() if sys.argv[1:] == ["--test"] else main()

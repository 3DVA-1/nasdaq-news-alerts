"""Separate Nasdaq news emails with Lithuanian context explanations.

Run once from a scheduler; persist news_state independently of NQ candle state.
Public RSS/Atom feeds and topic-based explanations; no AI API is used.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import logging
from pathlib import Path
import re
import smtplib
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from monitor import mail_settings, send_mail

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("nasdaq-news")
MAX_FEED_BYTES = 2_000_000
MAX_SEEN = 6000
STATE_VERSION = 1

CATEGORIES = {
    "fed": ("FED ir palūkanos", "FED sprendimai ir komentarai gali keisti palūkanų lūkesčius, technologijų bendrovių finansavimo sąlygas ir jų vertinimą. Reikšmė Nasdaq priklauso nuo pranešimo turinio ir to, ko rinka jau tikėjosi."),
    "macro": ("JAV ekonomikos duomenys", "Ekonomikos duomenys padeda vertinti vartotojų paklausą, įmonių sąnaudas ir būsimas palūkanas. Svarbu, kuo paskelbti rezultatai skiriasi nuo ankstesnių duomenų bei rinkos lūkesčių."),
    "tech": ("Didžiosios technologijų bendrovės", "Stambių technologijų bendrovių naujienos gali keisti jų pajamų ir pelno perspektyvas bei viso sektoriaus vertinimą. Reikšmė Nasdaq priklauso nuo paveiktų bendrovių ir nuo to, ar naujiena keičia jų veiklos perspektyvas."),
    "trade": ("Prekyba ir sankcijos", "Muitai, sankcijos ir eksporto apribojimai gali keisti technologijų bendrovių pardavimus, sąnaudas bei lustų tiekimą. Reikšmę Nasdaq lemia konkrečios priemonės, jų apimtis ir paveiktos įmonės."),
    "geopolitics": ("Geopolitinis įvykis", "Geopolitiniai įvykiai gali paveikti tarptautinę prekybą, tiekimo grandines ir investuotojų pasirengimą prisiimti riziką. Reikšmė Nasdaq priklauso nuo įvykio masto ir jo pasekmių įmonių veiklai."),
    "energy": ("Energijos rinkos sukrėtimas", "Energijos rinkos pokyčiai gali keisti įmonių sąnaudas ir infliacijos lūkesčius, todėl jų reikšmė pasiekia ir technologijų sektorių. Nasdaq poveikis priklauso nuo pokyčio masto, trukmės ir platesnių ekonominių aplinkybių."),
    "financial": ("Finansų sistemos įvykis", "Bankų ir valstybės finansavimo problemos gali keisti paskolų prieinamumą ir investuotojų rizikos vertinimą visoje akcijų rinkoje. Reikšmė Nasdaq priklauso nuo to, ar problema lieka pavienė, ar paliečia daugiau finansų įstaigų bei įmonių."),
}


def plain(value, limit=1200):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", value or ""))).strip()[:limit]


def normalized_title(title):
    return " ".join(re.findall(r"[\w]+", html.unescape(title).casefold(), flags=re.UNICODE))


def canonical_url(url):
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Invalid article URL")
    tracking = {"fbclid", "gclid", "ocid", "cmpid", "cid", "ref", "referrer", "output", "at_medium", "at_campaign"}
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if not k.casefold().startswith("utm_") and k.casefold() not in tracking)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(query), ""))


def story_id(url, title=""):
    identity = canonical_url(url) + "\n" + normalized_title(title)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def timestamp(value):
    try:
        stamp = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
    if stamp.tzinfo is None:
        return None
    return stamp.timestamp()


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(data, feed):
    if len(data) > MAX_FEED_BYTES or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("Feed is too large or contains a DTD/entity declaration")
    root = ET.fromstring(data)
    if local_name(root.tag) not in {"rss", "feed", "RDF"}:
        raise ValueError("Response is not RSS/Atom")
    articles = []
    for entry in root.iter():
        if local_name(entry.tag) not in {"item", "entry"}:
            continue
        values = {}
        links = []
        for child in entry:
            name = local_name(child.tag)
            values.setdefault(name, "".join(child.itertext()).strip())
            if name == "link":
                if child.attrib.get("href") and child.attrib.get("rel", "alternate") == "alternate":
                    links.insert(0, child.attrib["href"])
                elif child.text:
                    links.append(child.text.strip())
        title = plain(values.get("title"), 300)
        link = next((v for v in links if v.startswith(("https://", "http://"))), None)
        if not title or not link:
            continue
        try:
            link = canonical_url(link)
        except ValueError:
            continue
        # published is authoritative; updated is a fallback only for feeds that omit it.
        published = timestamp(values.get("pubDate") or values.get("published") or values.get("date") or values.get("updated"))
        articles.append({"id": story_id(link, title), "url": link, "title": title,
                         "normalized": normalized_title(title), "published": published,
                         "excerpt": plain(values.get("description") or values.get("summary") or values.get("content")),
                         "source_id": feed["id"], "source": feed["name"], "feed_category": feed.get("category", "general")})
    return articles


def fetch_feed(feed):
    if urlsplit(feed["url"]).scheme != "https":
        raise ValueError("Feeds must use HTTPS")
    request = Request(feed["url"], headers={"User-Agent": "NasdaqNewsMonitor/1.0 (public RSS reader)",
                                          "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    with urlopen(request, timeout=25) as response:
        if urlsplit(response.url).scheme != "https":
            raise ValueError("Feed redirected to unencrypted HTTP")
        data = response.read(MAX_FEED_BYTES + 1)
    return parse_feed(data, feed)


def matches(pattern, text):
    return re.search(pattern, text, re.IGNORECASE) is not None


def classify(article):
    """Conservative relevance from the headline, never market direction."""
    title = article["title"]
    source_category = article.get("feed_category", "general")
    if source_category == "fed":
        return "fed"
    if source_category == "macro":
        return "macro"
    # Opinion, evergreen guidance and price targets are not fresh economic events.
    if matches(r"\b(opinion|what to (?:buy|watch)|stocks? to buy|should you|price target|could (?:rise|soar|surge)|how to invest|best stocks?|stock picks?|is it time to buy|why i|my top)\b", title):
        return None
    if matches(r"\b(federal reserve|fomc|fed|powell)\b", title) and matches(r"\b(rate|rates|cut|cuts|hike|hikes|inflation|policy|speech|speaks|says|warns|minutes|decision|chair)\b", title):
        return "fed"
    if matches(r"\b(u\.?s\.?|united states|american)\b", title) and matches(r"\b(cpi|pce|inflation|nonfarm|payrolls?|unemployment|jobs report|employment report|gdp|gross domestic product|retail sales|producer prices|jobless claims)\b", title):
        return "macro"
    if matches(r"^(?:the )?(?:u\.?s\.?|united states|american) (?:economy )?(?:adds?|added|creates?|created|loses?|lost)\b.*\bjobs\b", title):
        return "macro"
    if matches(r"\b(tariffs?|export (?:controls?|ban|bans|restrictions?)|trade (?:war|deal|agreement)|sanctions?|chip (?:ban|bans)|semiconductor restrictions)\b", title) and matches(r"\b(u\.?s\.?|united states|china|chinese|taiwan|europe|eu|japan|chips?|semiconductors?|tech|trump|president|russia|iran)\b", title):
        return "trade"
    company = r"\b(nvidia|apple|microsoft|amazon|alphabet|google|meta|tesla|broadcom|amd|intel|tsmc|taiwan semiconductor|asml|micron|qualcomm)\b"
    catalyst = r"\b(earnings|results|revenue|profit|profits|forecast|forecasts|guidance|outlook|antitrust|lawsuit|regulator|regulators|investigation|ban|banned|recall|outage|merger|acquisition|acquire|acquires|acquiring|export|data breach|cyberattack|layoffs|investment|invests)\b"
    if matches(company, title) and matches(catalyst, title):
        return "tech"
    if matches(r"\b(china|taiwan|iran|israel|russia|ukraine|middle east|strait of hormuz|red sea|u\.?s\.?|united states|nato)\b", title) and matches(r"\b(invades?|invasion|military strikes?|airstrikes?|missile attack|missile strikes?|declares? war|ceasefire|blockade|nuclear (?:attack|threat|deal)|war escalat\w*|troops? (?:enter|attack)|closes? (?:the )?(?:strait|airspace)|peace deal)\b", title):
        return "geopolitics"
    actor = r"(?:china|taiwan|iran|israel|russia|ukraine|u\.?s\.?|united states|nato)"
    target = r"(?:china|chinese|taiwan|taiwanese|iran|iranian|israel|israeli|russia|russian|ukraine|ukrainian|u\.?s\.?|united states|nato)"
    if matches(r"\b" + actor + r"\b(?: (?:forces|military|army))? (?:attacks?|strikes?|bombs?|bombards?|invades?)(?: (?:on|against))? " + target + r"\b", title):
        return "geopolitics"
    if matches(r"\b(oil|crude|natural gas|opec|hormuz)\b", title) and matches(r"\b(surges?|plunges?|spikes?|soars?|output cut|production cut|supply (?:shock|disruption)|blockade|suspends? (?:exports?|production))\b", title):
        return "energy"
    if matches(r"\b(bank|banks|banking|treasury|debt ceiling|sovereign|government shutdown)\b", title) and matches(r"\b(collapse|collapses|failure|fails|bailout|crisis|default|defaults|shutdown|emergency)\b", title):
        return "financial"
    return None


def new_state():
    return {"version": STATE_VERSION, "sources": {}, "seen": {}, "pending": []}


def load_state(path):
    if not path.exists():
        return new_state()
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION or not isinstance(state.get("sources"), dict) or not isinstance(state.get("seen"), dict) or not isinstance(state.get("pending"), list):
        raise ValueError("Invalid news state; refusing to reset delivery history")
    if len(state["seen"]) > MAX_SEEN * 2 or len(state["pending"]) > 1000:
        raise ValueError("News state exceeds supported bounds")
    for item in state["seen"].values():
        if not isinstance(item, dict) or not isinstance(item.get("normalized"), str) or not isinstance(item.get("observed"), (int, float)):
            raise ValueError("Invalid seen history")
    for item in state["pending"]:
        if not isinstance(item, dict) or not {"id", "url", "title", "published", "detected", "category", "source"}.issubset(item) or item["category"] not in CATEGORIES or not isinstance(item["published"], (int, float)):
            raise ValueError("Invalid pending news item")
    return state


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def duplicate(article, state, now):
    if article["id"] in state["seen"]:
        return True
    candidate = article["normalized"]
    for prior in state["seen"].values():
        if prior["observed"] < now - 86400:
            continue
        title = prior["normalized"]
        if title == candidate:
            return True
        # Preserve number changes: revised figures/earnings are a material update.
        if (len(candidate) >= 45
                and re.findall(r"\d+(?:\.\d+)?", candidate) == re.findall(r"\d+(?:\.\d+)?", title)
                and action_fingerprint(candidate) == action_fingerprint(title)
                and SequenceMatcher(None, candidate, title).ratio() >= 0.94):
            return True
    return False


def action_fingerprint(title):
    groups = (
        r"\b(cut|cuts|cutting|lowers?|lowering)\b",
        r"\b(hike|hikes|hiking|raises?|raising)\b",
        r"\b(rise|rises|rising|rose|surge|surges|surging|gain|gains|gained|up)\b",
        r"\b(fall|falls|falling|fell|drop|drops|dropping|plunge|plunges|down)\b",
        r"\b(beat|beats|beating|exceed|exceeds)\b",
        r"\b(miss|misses|missing)\b",
        r"\b(ceasefire|peace|ends?|ending)\b",
        r"\b(attack|attacks|strike|strikes|invasion)\b",
        r"\b(not|no|never|denies|denied|rejects|rejected)\b",
    )
    return tuple(bool(matches(pattern, title)) for pattern in groups)


def remember(article, state, now):
    state["seen"][article["id"]] = {"normalized": article["normalized"], "observed": now}


def collect(results, cfg, state, now):
    """results contains successful source fetches only; failures never bootstrap."""
    maximum_age = cfg["maximum_news_age_seconds"]
    known = [(feed, articles) for feed, articles in results if feed["id"] in state["sources"]]
    baseline = [(feed, articles) for feed, articles in results if feed["id"] not in state["sources"]]
    added = 0
    for feed, articles in known:
        for article in sorted(articles, key=lambda value: value["published"] or 0):
            published = article["published"]
            if published is None or published > now or now - published > maximum_age:
                continue
            if duplicate(article, state, now):
                continue
            category = classify(article)
            remember(article, state, now)
            if category:
                state["pending"].append(dict(article, category=category, detected=now))
                added += 1
                LOG.info("Queued news: source=%s category=%s title=%s", feed["id"], category, article["title"])
    for feed, articles in baseline:
        for article in articles:
            remember(article, state, now)
        state["sources"][feed["id"]] = {"initialized_at": now}
        LOG.info("Initialized source %s: %d existing articles recorded silently", feed["id"], len(articles))
    # Outbox owns its records; trim old history without dropping pending messages.
    state["seen"] = dict(sorted(((key, value) for key, value in state["seen"].items() if value["observed"] >= now - 7 * 86400), key=lambda item: item[1]["observed"])[-MAX_SEEN:])
    return added


def explain_event(article):
    """Select a Lithuanian explanation of economic links, using the news topic.

    This explains context without inventing facts, forecasts or market reaction.
    """
    title = article["title"]
    category = article["category"]
    if category == "macro":
        if matches(r"\b(cpi|pce|inflation|consumer pric\w*|producer pric\w*)\b", title):
            return "Infliacijos duomenys rodo kainų spaudimą ir gali keisti lūkesčius dėl FED palūkanų politikos. Nasdaq svarbus paskelbtų duomenų skirtumas nuo rinkos lūkesčių."
        if matches(r"\b(jobs?|payrolls?|nonfarm|employment|unemployment|jobless|hiring)\b", title):
            return "Darbo rinkos duomenys padeda vertinti ekonomikos aktyvumą, vartojimą ir atlyginimų spaudimą. Nasdaq reikšmė priklauso nuo to, ką duomenys pasako apie įmonių paklausą ir būsimas palūkanas."
    if category == "tech":
        if matches(r"\b(outage|cyberattack|data breach|disruption)\b", title):
            return "Paslaugų sutrikimas ar kibernetinis incidentas gali paveikti bendrovės pajamas, išlaidas ir klientų pasitikėjimą. Reikšmė Nasdaq priklauso nuo sutrikimo masto, trukmės ir paveiktų paslaugų."
        if matches(r"\b(earnings|results|revenue|profits?|forecasts?|guidance|outlook)\b", title):
            return "Pajamų, pelno ir veiklos prognozių pranešimai padeda vertinti bendrovės verslo perspektyvas. Nasdaq svarbu, kaip šie rezultatai bei prognozės skiriasi nuo rinkos lūkesčių."
    return CATEGORIES[category][1]


def payload(article, cfg):
    zone = ZoneInfo(cfg["display_timezone"])
    def display(stamp):
        return datetime.fromtimestamp(stamp, timezone.utc).astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")
    category = CATEGORIES[article["category"]][0]
    relevance = explain_event(article)
    subject = f"[NASDAQ ŽINIOS] {article['title']}"[:240]
    lines = ["Atskiras Nasdaq naujienų perspėjimas", "", f"Tema: {category}",
             f"Antraštė (šaltinio kalba): {article['title']}", f"Šaltinis: {article['source']}",
             f"Paskelbta: {display(article['published'])}", f"Aptikta: {display(article['detected'])}", "",
             f"Ką tai gali reikšti Nasdaq: {relevance}"]
    if article.get("excerpt"):
        lines += ["", "Trumpa šaltinio ištrauka (originalo kalba):", article["excerpt"][:350]]
    lines += ["", f"Skaityti: {article['url']}"]
    lines += ["", "Paaiškinimas parodo galimą naujienos ryšį su Nasdaq. Kainos kilimo ar kritimo kryptis neprognozuojama."]
    lines += ["Tai atskiras pranešimas nuo NQ POC signalų. Vienai naujienai siunčiamas vienas laiškas."]
    return subject, "\n".join(lines), "news-" + article["id"]


def deliver_pending(state, path, cfg, now, sender=None):
    sender = sender or send_mail
    sent = 0
    for article in list(state["pending"]):
        if now - article["published"] > cfg["maximum_news_age_seconds"] or article["published"] > now:
            state["pending"].remove(article)
            save_state(path, state)
            LOG.warning("Expired pending news %s without sending", article["id"])
            continue
        if sent >= cfg["maximum_emails_per_run"]:
            break
        sender(*payload(article, cfg))
        state["pending"].remove(article)
        save_state(path, state)
        sent += 1
        LOG.info("News email accepted by SMTP: %s", article["id"])
    return sent


def read_config(path):
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cfg.get("feeds"), list) or not cfg["feeds"] or len(cfg["feeds"]) > 30:
        raise ValueError("Configure 1-30 feeds")
    ids = set()
    for feed in cfg["feeds"]:
        if not isinstance(feed, dict) or any(not isinstance(feed.get(key), str) or not feed[key] for key in ("id", "name", "url")) or feed["id"] in ids or urlsplit(feed["url"]).scheme != "https" or feed.get("category", "general") not in {"general", "fed", "macro"}:
            raise ValueError("Invalid or duplicated feed configuration")
        ids.add(feed["id"])
    for key, default, high in (("maximum_news_age_seconds", 10800, 86400), ("maximum_emails_per_run", 6, 20)):
        value = cfg.setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
            raise ValueError(f"Invalid {key}")
    ZoneInfo(cfg.setdefault("display_timezone", "Europe/Vilnius"))
    return cfg


def run(cfg, path, now, dry_run=False, fetcher=None, sender=None):
    fetcher = fetcher or fetch_feed
    state = load_state(path)
    if not dry_run:
        mail_settings()
    successes, errors = [], []
    with ThreadPoolExecutor(max_workers=6) as pool:
        future_sources = {pool.submit(fetcher, feed): feed for feed in cfg["feeds"]}
        for future in as_completed(future_sources):
            feed = future_sources[future]
            try:
                articles = future.result()
                successes.append((feed, articles))
                LOG.info("Read %s: %d articles", feed["id"], len(articles))
            except Exception as exc:
                errors.append(feed["id"])
                LOG.warning("Source %s unavailable (%s)", feed["id"], type(exc).__name__)
    if not successes:
        raise RuntimeError("All news sources failed; delivery history preserved")
    # Stable source order makes duplicate selection deterministic.
    order = {feed["id"]: index for index, feed in enumerate(cfg["feeds"])}
    successes.sort(key=lambda item: order[item[0]["id"]])
    added = collect(successes, cfg, state, now)
    if dry_run:
        LOG.info("DRY RUN: %d new eligible stories; %d pending; no email/state writes", added, len(state["pending"]))
        return {"new": added, "sent": 0, "sources_ok": len(successes), "sources_failed": errors, "pending": len(state["pending"])}
    save_state(path, state)  # Durable outbox before any SMTP submission.
    sent = deliver_pending(state, path, cfg, now, sender)
    return {"new": added, "sent": sent, "sources_ok": len(successes), "sources_failed": errors, "pending": len(state["pending"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "news_config.json")
    parser.add_argument("--state", type=Path, default=ROOT / "news_state" / "state.json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--test-email", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = read_config(args.config)
        now = time.time()
        if args.test_email:
            mail_settings()
            body = ("Nasdaq naujienų perspėjimai paruošti.\n\n"
                    "Naujienos bus siunčiamos atskirais laiškais su tema [NASDAQ ŽINIOS].\n"
                    "Stebimos FED/palūkanų, JAV ekonomikos, didžiųjų technologijų bendrovių, prekybos ir geopolitinės naujienos.\n"
                    "Tikrinimo tikslas – maždaug kas 15 min.; šaltinių ir planuoklio vėlavimas galimas.\n"
                    "Pirmas šaltinio nuskaitymas įsimena jau esančias naujienas jų nesiųsdamas.\n"
                    "Vienai naujienai siunčiamas vienas laiškas. Tai atskira sistema nuo NQ POC signalų.\n"
                    "Antraštės ir šaltinio santraukos pateikiamos originalo kalba; aktualumo paaiškinimas – lietuviškai.\n"
                    "Tai bandomasis laiškas, o ne pranešimas apie konkretų rinkos įvykį.")
            send_mail("[NASDAQ ŽINIOS] Bandomasis perspėjimų laiškas", body, f"news-test-{int(now)}")
            LOG.info("Single news setup email accepted by SMTP")
            return 0
        result = run(cfg, args.state, now, dry_run=args.dry_run)
        LOG.info("News check complete: %s", json.dumps(result))
        return 0
    except (OSError, ValueError, RuntimeError, smtplib.SMTPException, ET.ParseError) as exc:
        # SMTP responses may contain addresses; log type rather than credentials/server text.
        LOG.error("News check failed: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

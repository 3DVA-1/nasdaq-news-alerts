import copy
from datetime import datetime, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import news_monitor as news


NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc).timestamp()
FEED = {"id": "business", "name": "Business", "url": "https://example.com/feed", "category": "general"}
OTHER = {"id": "world", "name": "World", "url": "https://example.net/feed", "category": "general"}
CFG = {"feeds": [FEED], "maximum_news_age_seconds": 10800, "maximum_emails_per_run": 6,
       "display_timezone": "Europe/Vilnius"}


def article(title="Microsoft earnings beat forecasts as revenue rises 15%", url="https://example.com/story", published=NOW - 60, feed=None):
    source = feed or FEED
    return {"id": news.story_id(url, title), "url": news.canonical_url(url), "title": title,
            "normalized": news.normalized_title(title), "published": published,
            "excerpt": "A source-provided summary.", "source_id": source["id"], "source": source["name"],
            "feed_category": source["category"]}


def known_state(*sources):
    state = news.new_state()
    for source in sources or (FEED,):
        state["sources"][source["id"]] = {"initialized_at": NOW - 3600}
    return state


class FeedParsingTests(unittest.TestCase):
    def test_rss_date_html_and_tracking(self):
        data = b'''<rss version="2.0"><channel><item><title>Apple earnings &amp; revenue rise</title><link>https://example.com/story/?utm_source=rss&amp;id=5#main</link><pubDate>Sat, 07 Nov 2026 19:00:00 GMT</pubDate><description>&lt;p&gt;Actual &amp;amp; quoted.&lt;/p&gt;</description></item></channel></rss>'''
        result = news.parse_feed(data, FEED)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "https://example.com/story?id=5")
        self.assertEqual(result[0]["title"], "Apple earnings & revenue rise")
        self.assertEqual(result[0]["excerpt"], "Actual & quoted.")
        self.assertEqual(result[0]["published"], NOW - 3600)

    def test_atom_namespace_and_published_precedence(self):
        data = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>US inflation rises</title><link rel="self" href="https://example.com/api"/><link href="https://example.com/report"/><published>2026-11-07T19:00:00Z</published><updated>2026-11-07T19:59:00Z</updated></entry></feed>'''
        result = news.parse_feed(data, FEED)
        self.assertEqual(result[0]["url"], "https://example.com/report")
        self.assertEqual(result[0]["published"], NOW - 3600)

    def test_no_missing_or_naive_date_assumption(self):
        self.assertIsNone(news.timestamp(None))
        self.assertIsNone(news.timestamp("2026-11-07T19:00:00"))
        self.assertIsNone(news.timestamp("not a date"))

    def test_reject_dtd_and_non_feed(self):
        with self.assertRaises(ValueError):
            news.parse_feed(b'<!DOCTYPE rss [<!ENTITY x "bad">]><rss/>', FEED)
        with self.assertRaises(ValueError):
            news.parse_feed(b"<html><body>Denied</body></html>", FEED)

    def test_reject_unsafe_url(self):
        for value in ("javascript:alert(1)", "https://name:secret@example.com/story", "file:///story"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                news.canonical_url(value)


class RelevanceTests(unittest.TestCase):
    def test_topic_explanations_distinguish_economic_links(self):
        cases = [
            ('US inflation accelerates in latest CPI report', 'macro', 'Infliacijos duomenys'),
            ('US adds jobs in October', 'macro', 'Darbo rinkos duomenys'),
            ('Microsoft earnings beat forecasts', 'tech', 'Pajamų, pelno'),
            ('Microsoft outage disrupts cloud services', 'tech', 'Paslaugų sutrikimas'),
            ('US imposes chip export restrictions on China', 'trade', 'lustų tiekimą'),
        ]
        for title, category, expected in cases:
            item = dict(article(title), category=category)
            with self.subTest(title=title):
                explanation = news.explain_event(item)
                self.assertIn(expected, explanation)
                self.assertIn('Nasdaq', explanation)
                self.assertNotIn('kryptis:', explanation.lower())

    def test_important_headline_categories(self):
        cases = {
            "Fed cuts interest rates after policy meeting": "fed",
            "US inflation accelerates in latest CPI report": "macro",
            "US adds 150,000 jobs in October": "macro",
            "US economy loses 100,000 jobs in October": "macro",
            "Nvidia earnings beat forecasts": "tech",
            "US imposes new chip export restrictions on China": "trade",
            "China launches military strikes near Taiwan": "geopolitics",
            "China attacks Taiwan": "geopolitics",
            "Israel strikes Iran": "geopolitics",
            "Russia bombs Ukrainian power plants": "geopolitics",
            "Oil prices surge as supply disruption grows": "energy",
            "Large US bank collapses amid funding crisis": "financial",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(news.classify(article(title)), expected)

    def test_commentary_and_unrelated_descriptions_do_not_trigger(self):
        for title in ("AI says this stock could rise 200%", "The best Nvidia stocks to buy", "Apple pie recipe wins a local prize", "Five things to do in London this weekend", "Amazon rainforest conservation project opens", "Trump attacks Fed over handling of office renovation", "Critics attack China over festival rules", "US retailer adds 150,000 jobs in October", "Israel strikes deal with Iran on sports exchanges"):
            value = article(title)
            value["excerpt"] = "Fed rates inflation Nvidia earnings and China military strikes"
            with self.subTest(title=title):
                self.assertIsNone(news.classify(value))

    def test_official_sources_are_recognized(self):
        value = article("FOMC statement issued", feed=dict(FEED, category="fed"))
        self.assertEqual(news.classify(value), "fed")
        value = article("Personal income and outlays, October", feed=dict(FEED, category="macro"))
        self.assertEqual(news.classify(value), "macro")


class StateAndQueueTests(unittest.TestCase):
    def test_first_fetch_baselines_without_historical_email(self):
        state = news.new_state()
        self.assertEqual(news.collect([(FEED, [article()])], CFG, state, NOW), 0)
        self.assertIn(FEED["id"], state["sources"])
        self.assertEqual(state["pending"], [])
        self.assertEqual(news.collect([(FEED, [article()])], CFG, state, NOW + 60), 0)

    def test_new_source_does_not_suppress_new_story_from_initialized_source(self):
        state = known_state(FEED)
        value = article()
        second = article(feed=OTHER, url="https://example.net/syndicated")
        self.assertEqual(news.collect([(OTHER, [second]), (FEED, [value])], CFG, state, NOW), 1)
        self.assertEqual(len(state["pending"]), 1)
        self.assertIn(OTHER["id"], state["sources"])

    def test_future_missing_and_old_items_skip(self):
        state = known_state()
        values = [article(url="https://example.com/old", published=NOW - 10801),
                  article(url="https://example.com/future", published=NOW + 1),
                  article(url="https://example.com/missing", published=None)]
        self.assertEqual(news.collect([(FEED, values)], CFG, state, NOW), 0)
        self.assertEqual(state["pending"], [])

    def test_duplicate_across_sources_and_tracking_params(self):
        state = known_state(FEED, OTHER)
        first = article(url="https://example.com/story?utm_source=one")
        second = article(url="https://example.net/copy?utm_source=two", feed=OTHER)
        self.assertEqual(news.collect([(FEED, [first]), (OTHER, [second])], CFG, state, NOW), 1)
        self.assertEqual(news.collect([(FEED, [article(url="https://example.com/story?utm_source=three")])], CFG, state, NOW + 1), 0)

    def test_fuzzy_minor_edit_is_suppressed(self):
        state = known_state()
        first = article("Microsoft earnings beat forecasts as quarterly revenue rises 15%")
        news.collect([(FEED, [first])], CFG, state, NOW)
        # A modest punctuation change normalizes exactly; very different wording is a new item.
        second = article("Microsoft earnings beat forecasts, as quarterly revenue rises 15%", url="https://example.net/copy")
        self.assertEqual(news.collect([(FEED, [second])], CFG, state, NOW + 1), 0)

    def test_new_numbers_even_same_url_are_new_material_update(self):
        state = known_state()
        first = article("US raises tariffs on China from 10% to 20%")
        second = article("US raises tariffs on China from 10% to 30%")
        self.assertEqual(news.collect([(FEED, [first])], CFG, state, NOW), 1)
        self.assertEqual(news.collect([(FEED, [second])], CFG, state, NOW + 1), 1)

    def test_changed_policy_action_not_fuzzy_suppressed(self):
        state = known_state()
        first = article("Federal Reserve cuts interest rates following its latest policy meeting")
        second = article("Federal Reserve hikes interest rates following its latest policy meeting", url="https://example.com/update")
        news.collect([(FEED, [first])], CFG, state, NOW)
        self.assertEqual(news.collect([(FEED, [second])], CFG, state, NOW + 1), 1)

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text('{"version": 1, "sources": {}, "seen": {}, "pending": [{}]}', encoding="utf-8")
            with self.assertRaises(ValueError):
                news.load_state(path)
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                news.load_state(path)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.state = known_state()
        news.collect([(FEED, [article()])], CFG, self.state, NOW)

    def test_smtp_failure_leaves_durable_pending_for_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            news.save_state(path, self.state)
            failed = Mock(side_effect=OSError("offline"))
            with self.assertRaises(OSError):
                news.deliver_pending(self.state, path, CFG, NOW, failed)
            retry = news.load_state(path)
            self.assertEqual(len(retry["pending"]), 1)
            sender = Mock()
            self.assertEqual(news.deliver_pending(retry, path, CFG, NOW + 1, sender), 1)
            self.assertEqual(news.load_state(path)["pending"], [])
            self.assertEqual(sender.call_count, 1)
            self.assertTrue(sender.call_args.args[0].startswith("[NASDAQ ŽINIOS]"))

    def test_expired_pending_not_sent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            sender = Mock()
            self.assertEqual(news.deliver_pending(self.state, path, CFG, NOW + 10800, sender), 0)
            sender.assert_not_called()
            self.assertEqual(news.load_state(path)["pending"], [])

    def test_delivery_cap_retains_rest(self):
        for i in range(1, 4):
            value = article(f"Nvidia earnings revenue hits {i} billion", url=f"https://example.com/{i}")
            news.collect([(FEED, [value])], CFG, self.state, NOW)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            sender = Mock()
            self.assertEqual(news.deliver_pending(self.state, path, dict(CFG, maximum_emails_per_run=2), NOW, sender), 2)
            self.assertEqual(sender.call_count, 2)
            self.assertEqual(len(news.load_state(path)["pending"]), 2)

    def test_payload_clear_source_time_and_no_claimed_translation(self):
        subject, body, identity = news.payload(self.state["pending"][0], CFG)
        self.assertIn("[NASDAQ ŽINIOS]", subject)
        self.assertIn("2026-11-07 21:59", body)
        self.assertIn("šaltinio kalba", body)
        self.assertIn("https://example.com/story", body)
        self.assertIn("Kainos kilimo ar kritimo kryptis neprognozuojama", body)
        self.assertTrue(identity.startswith("news-"))


class RunTests(unittest.TestCase):
    def test_dry_run_never_writes_or_sends(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "news_state" / "state.json"
            sender = Mock()
            with patch.object(news, "mail_settings") as settings:
                result = news.run(CFG, path, NOW, dry_run=True, fetcher=lambda _: [article()], sender=sender)
            self.assertFalse(path.exists())
            sender.assert_not_called()
            settings.assert_not_called()
            self.assertEqual(result["sources_ok"], 1)

    def test_failed_source_only_baselines_when_recovered(self):
        cfg = dict(CFG, feeds=[FEED, OTHER])
        with tempfile.TemporaryDirectory() as folder, patch.object(news, "mail_settings"):
            path = Path(folder) / "state.json"
            def fetch(feed):
                if feed["id"] == OTHER["id"]:
                    raise OSError("temporary error")
                return [article()]
            result = news.run(cfg, path, NOW, fetcher=fetch, sender=Mock())
            self.assertEqual(result["sources_failed"], [OTHER["id"]])
            state = news.load_state(path)
            self.assertNotIn(OTHER["id"], state["sources"])
            sender = Mock()
            def recovered(feed):
                return [article(url="https://example.net/recovered", feed=feed)]
            result = news.run(cfg, path, NOW + 60, fetcher=recovered, sender=sender)
            sender.assert_not_called()
            self.assertIn(OTHER["id"], news.load_state(path)["sources"])

    def test_total_failure_does_not_replace_existing_state(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(news, "mail_settings"):
            path = Path(folder) / "state.json"
            news.save_state(path, known_state())
            before = path.read_bytes()
            def fail(_):
                raise OSError("offline")
            with self.assertRaises(RuntimeError):
                news.run(CFG, path, NOW, fetcher=fail, sender=Mock())
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

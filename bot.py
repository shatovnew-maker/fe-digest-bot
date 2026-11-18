#!/usr/bin/env python3
"""Ежедневная сводка в Telegram: упоминания Владивостока, Приморья и Дальнего Востока
России в международных СМИ — со ссылками и цитатами в оригинале и переводе.

Запуск:
    python bot.py               # собрать сводку и отправить в Telegram
    python bot.py --dry-run     # только показать сводку в консоли, ничего не отправлять
    python bot.py --hours 48    # искать за последние 48 часов

Переменные окружения:
    TELEGRAM_BOT_TOKEN   токен бота от @BotFather (обязательно, кроме --dry-run)
    TELEGRAM_CHAT_ID     куда слать: ваш chat id или @канал (обязательно, кроме --dry-run)
    ANTHROPIC_API_KEY    необязательно: если задан, переводит Claude (качественнее)
    CLAUDE_MODEL         необязательно: модель для перевода, по умолчанию claude-haiku-5-5
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state" / "seen.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
TG_LIMIT = 3900  # лимит Telegram 4096 символов, оставляем запас
KEYWORDS_PER_QUERY = 7

log = logging.getLogger("digest")


# ---------------------------------------------------------------- модель данных

@dataclass
class Item:
    title: str
    url: str
    published: dt.datetime
    source_name: str
    domain: str
    lang: str
    feed: str                      # google | bing
    snippet: str = ""
    outlet: str | None = None      # название издания из списка
    region: str | None = None      # раздел списка (страна)
    quotes: list[str] = field(default_factory=list)
    verified: bool | None = None   # True — нашли упоминание в тексте; None — текст не открылся
    title_tr: str = ""
    quotes_tr: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        norm = re.sub(r"\W+", "", self.title.lower())[:80]
        return f"{self.domain}|{norm}"


# ---------------------------------------------------------------- настройки

def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    outlet_index = []
    for region, outlets in cfg["outlets"].items():
        for o in outlets:
            for d in o["domains"]:
                outlet_index.append((d.lower(), o["name"], region))
    cfg["_outlet_index"] = outlet_index
    cfg["_region_order"] = list(cfg["outlets"].keys())
    all_kw = [k for lang in cfg["languages"].values() for k in lang["keywords"]]
    cfg["_keyword_re"] = compile_keywords(all_kw)
    return cfg


def compile_keywords(keywords: list[str]) -> re.Pattern:
    parts = []
    for k in sorted(set(keywords), key=len, reverse=True):
        esc = re.escape(k).replace(r"\ ", r"\s+")
        # латиница — с границей слова в начале, CJK — как есть
        parts.append(rf"\b{esc}" if re.match(r"[A-Za-z]", k) else esc)
    return re.compile("|".join(parts), re.IGNORECASE)


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def domain_matches(domain: str, pattern: str) -> bool:
    if pattern.startswith("."):          # «.ru» — вся доменная зона
        return domain.endswith(pattern)
    return domain == pattern or domain.endswith("." + pattern)


def find_outlet(domain: str, cfg: dict) -> tuple[str, str] | None:
    for pattern, name, region in cfg["_outlet_index"]:
        if domain_matches(domain, pattern):
            return name, region
    return None


def is_excluded(domain: str, cfg: dict) -> bool:
    return any(domain_matches(domain, p) for p in cfg.get("exclude_domains", []))


# ---------------------------------------------------------------- ленты новостей

def chunked(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def build_query(keywords: list[str]) -> str:
    return " OR ".join(f'"{k}"' if " " in k else k for k in keywords)


def http_get(url: str, timeout: int = 25) -> requests.Response:
    return requests.get(url, headers={"User-Agent": USER_AGENT,
                                      "Accept-Language": "en,ja,ko,zh;q=0.8"},
                        timeout=timeout)


def google_feed_urls(cfg: dict) -> list[tuple[str, str]]:
    urls = []
    for lang, lc in cfg["languages"].items():
        for group in chunked(lc["keywords"], KEYWORDS_PER_QUERY):
            q = quote(build_query(group) + " when:2d")
            for ed in lc["editions"]:
                urls.append((lang, f"https://news.google.com/rss/search?q={q}"
                                   f"&hl={ed['hl']}&gl={ed['gl']}&ceid={quote(ed['ceid'])}"))
    return urls


def bing_feed_urls(cfg: dict) -> list[tuple[str, str]]:
    urls = []
    for lang, lc in cfg["languages"].items():
        b = lc.get("bing")
        if not b:
            continue
        for group in chunked(lc["keywords"], KEYWORDS_PER_QUERY):
            q = quote(build_query(group))
            urls.append((lang, f"https://www.bing.com/news/search?q={q}&format=rss"
                               f"&qft=interval%3d%227%22&setlang={b['setlang']}&cc={b['cc']}"))
    return urls


def entry_time(entry) -> dt.datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)


def strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def parse_google(feed, lang: str) -> list[Item]:
    items = []
    for e in feed.entries:
        src = e.get("source") or {}
        src_name = src.get("title", "")
        src_url = src.get("href", "")
        title = e.get("title", "")
        if src_name and title.endswith(" - " + src_name):
            title = title[: -len(src_name) - 3]
        published = entry_time(e)
        if not (published and src_url):
            continue
        items.append(Item(title=title.strip(), url=e.get("link", ""), published=published,
                          source_name=src_name, domain=domain_of(src_url), lang=lang,
                          feed="google"))
    return items


def parse_bing(feed, lang: str) -> list[Item]:
    items = []
    for e in feed.entries:
        link = e.get("link", "")
        qs = parse_qs(urlparse(link).query)
        if "url" in qs:                  # ссылки Bing ведут через apiclick.aspx?url=...
            link = qs["url"][0]
        published = entry_time(e)
        if not published:
            continue
        items.append(Item(title=strip_html(e.get("title", "")), url=link, published=published,
                          source_name=e.get("news_source", "") or domain_of(link),
                          domain=domain_of(link), lang=lang, feed="bing",
                          snippet=strip_html(e.get("summary", ""))))
    return items


def collect(cfg: dict, hours: int) -> tuple[list[Item], int, int]:
    """Возвращает (материалы, сколько лент прочитано, сколько лент не открылось)."""
    jobs = []
    if cfg["sources"].get("google_news", True):
        jobs += [("google", lang, u) for lang, u in google_feed_urls(cfg)]
    if cfg["sources"].get("bing_news", True):
        jobs += [("bing", lang, u) for lang, u in bing_feed_urls(cfg)]

    def run(job):
        kind, lang, url = job
        try:
            r = http_get(url)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            return (parse_google if kind == "google" else parse_bing)(feed, lang), True
        except Exception as exc:  # одна упавшая лента не должна ронять всю сводку
            log.warning("Лента не открылась (%s): %s", kind, exc)
            return [], False

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, jobs))
    ok = sum(1 for _, good in results if good)
    failed = len(results) - ok

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    items = [it for batch, _ in results for it in batch if it.published >= cutoff]
    return items, ok, failed


# ---------------------------------------------------------------- отбор

def select(items: list[Item], cfg: dict, seen: dict) -> tuple[list[Item], int]:
    unique: dict[str, Item] = {}
    skipped_seen = 0
    for it in sorted(items, key=lambda x: (x.feed != "bing", -x.published.timestamp())):
        # bing даёт прямые ссылки и сниппеты — при дубле предпочитаем его
        if not it.domain or is_excluded(it.domain, cfg):
            continue
        if it.key in unique:
            continue
        if it.key in seen or it.url in seen:
            skipped_seen += 1
            continue
        found = find_outlet(it.domain, cfg)
        if found:
            it.outlet, it.region = found
        elif not cfg.get("include_other_sources", True):
            continue
        unique[it.key] = it

    listed = [i for i in unique.values() if i.outlet]
    others = [i for i in unique.values() if not i.outlet]

    per_outlet: dict[str, int] = {}
    capped = []
    for it in sorted(listed, key=lambda x: -x.published.timestamp()):
        n = per_outlet.get(it.outlet, 0)
        if n < cfg.get("max_items_per_outlet", 4):
            per_outlet[it.outlet] = n + 1
            capped.append(it)
    # прочих берём с запасом: часть отсеется при проверке текста
    others = sorted(others, key=lambda x: -x.published.timestamp())
    others = others[: cfg.get("max_other_sources", 15) * 2]
    return capped + others, skipped_seen


def resolve_google_links(items: list[Item]) -> None:
    """Ссылки Google News — переадресации; превращаем их в адреса самих статей."""
    todo = [it for it in items if it.feed == "google" and "news.google.com" in it.url]
    if not todo:
        return
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        log.warning("googlenewsdecoder не установлен — оставляю ссылки Google News")
        return
    for it in todo:
        try:
            res = gnewsdecoder(it.url, interval=1)
            if res.get("success") and res.get("decoded_url"):
                it.url = res["decoded_url"]
        except Exception as exc:
            log.info("Не удалось раскрыть ссылку Google News: %s", exc)


# ---------------------------------------------------------------- цитаты

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])|\n+")


def trim_around(sentence: str, match: re.Match, limit: int) -> str:
    if len(sentence) <= limit:
        return sentence
    mid = (match.start() + match.end()) // 2
    start = max(0, mid - limit // 2)
    end = min(len(sentence), start + limit)
    start = max(0, end - limit)
    return ("…" if start > 0 else "") + sentence[start:end].strip() + ("…" if end < len(sentence) else "")


def extract_quotes(text: str, kw: re.Pattern, max_quotes: int, max_chars: int) -> list[str]:
    quotes = []
    for raw in SENTENCE_SPLIT.split(text or ""):
        s = re.sub(r"\s+", " ", raw).strip()
        if len(s) < 15:
            continue
        m = kw.search(s)
        if m and s not in quotes:
            quotes.append(trim_around(s, m, max_chars))
            if len(quotes) >= max_quotes:
                break
    return quotes


def fetch_article_text(url: str) -> str | None:
    try:
        import trafilatura
        r = http_get(url, timeout=20)
        if r.status_code >= 400:
            return None
        return trafilatura.extract(r.text, url=url, include_comments=False,
                                   include_tables=False, favor_recall=True)
    except Exception as exc:
        log.info("Статья не открылась %s: %s", url, exc)
        return None


def enrich(items: list[Item], cfg: dict) -> list[Item]:
    kw = cfg["_keyword_re"]
    mq, mc = cfg.get("max_quotes_per_item", 2), cfg.get("max_quote_chars", 320)

    def work(it: Item) -> Item:
        text = fetch_article_text(it.url)
        if text and len(text) > 400:
            it.quotes = extract_quotes(text, kw, mq, mc)
            # статья открылась, но в тексте и заголовке ключевых слов нет — ложное срабатывание
            it.verified = bool(it.quotes) or bool(kw.search(it.title))
        else:
            it.verified = None
        if not it.quotes and it.snippet and kw.search(it.snippet):
            it.quotes = extract_quotes(it.snippet, kw, 1, mc)
        return it

    with ThreadPoolExecutor(max_workers=8) as pool:
        done = list(pool.map(work, items))

    kept = [it for it in done if it.verified is not False]
    listed = [i for i in kept if i.outlet]
    others = [i for i in kept if not i.outlet]
    # у «прочих» оставляем только подтверждённые, у изданий из списка — и неподтверждённые с пометкой
    others = [i for i in others if i.verified or i.quotes][: cfg.get("max_other_sources", 15)]
    return listed + others


# ---------------------------------------------------------------- перевод

def translate_claude(texts: list[str], target: str) -> list[str] | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    model = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
    prompt = (
        f"Translate every string in this JSON array into the language with code '{target}'. "
        "Keep names of people, places and outlets accurate; use the established Russian "
        "spellings of East Asian names. Return ONLY a JSON array of translated strings, "
        "same length and order.\n\n" + json.dumps(texts, ensure_ascii=False)
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 8000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120,
        )
        r.raise_for_status()
        out = "".join(b.get("text", "") for b in r.json()["content"])
        out = out[out.find("["): out.rfind("]") + 1]
        result = json.loads(out)
        if isinstance(result, list) and len(result) == len(texts):
            return [str(x) for x in result]
        log.warning("Claude вернул перевод не той длины — переключаюсь на Google Translate")
    except Exception as exc:
        log.warning("Перевод через Claude не удался: %s", exc)
    return None


def translate_google(texts: list[str], target: str) -> list[str]:
    from deep_translator import GoogleTranslator
    tr = GoogleTranslator(source="auto", target=target)
    out = []
    for t in texts:
        try:
            out.append(tr.translate(t) or "")
        except Exception as exc:
            log.info("Перевод не удался: %s", exc)
            out.append("")
        time.sleep(0.2)
    return out


def translate(items: list[Item], target: str) -> None:
    texts: list[str] = []
    for it in items:
        texts.append(it.title)
        texts.extend(it.quotes)
    texts = list(dict.fromkeys(t for t in texts if t))
    if not texts:
        return
    translated = translate_claude(texts, target) or translate_google(texts, target)
    mapping = dict(zip(texts, translated))
    for it in items:
        it.title_tr = mapping.get(it.title, "")
        it.quotes_tr = [mapping.get(q, "") for q in it.quotes]


# ---------------------------------------------------------------- сообщение

def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def same_text(a: str, b: str) -> bool:
    return re.sub(r"\W+", "", a.lower()) == re.sub(r"\W+", "", b.lower())


def render_item(it: Item, tz: ZoneInfo) -> str:
    when = it.published.astimezone(tz).strftime("%d.%m %H:%M")
    name = it.outlet or it.source_name or it.domain
    lines = [f"▪️ <b>{esc(name)}</b> · {when}",
             f'<a href="{html.escape(it.url, quote=True)}">{esc(it.title)}</a>']
    if it.title_tr and not same_text(it.title_tr, it.title):
        lines.append(f"<i>{esc(it.title_tr)}</i>")
    for i, q in enumerate(it.quotes):
        lines.append(f"<blockquote>{esc(q)}</blockquote>")
        tr = it.quotes_tr[i] if i < len(it.quotes_tr) else ""
        if tr and not same_text(tr, q):
            lines.append(f"<blockquote>🇷🇺 {esc(tr)}</blockquote>")
    if it.verified is None:
        lines.append("<i>⚠️ Текст статьи не открылся (платный доступ?) — "
                     "упоминание видно только по поисковой выдаче.</i>")
    return "\n".join(lines)


def build_messages(items: list[Item], cfg: dict, hours: int, skipped: int,
                   failed_feeds: int) -> list[str]:
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    today = dt.datetime.now(tz).strftime("%d.%m.%Y")
    listed = [i for i in items if i.outlet]
    others = [i for i in items if not i.outlet]

    header = (f"<b>🗞 Дальний Восток в зарубежных СМИ · {today}</b>\n"
              f"За {hours} ч: {len(items)} материалов, из них {len(listed)} — издания из списка.")
    blocks: list[str] = [header]

    if not items:
        blocks.append("Новых упоминаний не нашлось.")
    else:
        if listed:
            blocks.append("<b>━━ Издания из списка ━━</b>")
            for region in cfg["_region_order"]:
                group = sorted([i for i in listed if i.region == region],
                               key=lambda x: -x.published.timestamp())
                if group:
                    blocks.append(f"<b>{esc(region)}</b>")
                    blocks.extend(render_item(i, tz) for i in group)
        if others:
            blocks.append("<b>━━ Другие издания ━━</b>")
            blocks.extend(render_item(i, tz)
                          for i in sorted(others, key=lambda x: -x.published.timestamp()))

    footer = "<i>Источники поиска: Google News, Bing News"
    if skipped:
        footer += f" · уже присылались раньше: {skipped}"
    if failed_feeds:
        footer += f" · не открылось лент: {failed_feeds}"
    blocks.append(footer + "</i>")

    messages, current = [], ""
    for block in blocks:
        if len(block) > TG_LIMIT:
            block = block[:TG_LIMIT - 20] + "…"
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > TG_LIMIT:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


# ---------------------------------------------------------------- Telegram и состояние

def send_telegram(messages: list[str]) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    for text in messages:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True},
                          timeout=30)
        if not r.ok:
            raise RuntimeError(f"Telegram ответил {r.status_code}: {r.text[:300]}")
        time.sleep(1.1)


def load_seen() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen: dict, items: list[Item], keep_days: int = 21) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    for it in items:
        seen[it.key] = now.isoformat()
        seen[it.url] = now.isoformat()
    cutoff = now - dt.timedelta(days=keep_days)
    seen = {k: v for k, v in seen.items() if dt.datetime.fromisoformat(v) >= cutoff}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")


# ---------------------------------------------------------------- запуск

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--hours", type=int, default=int(os.environ.get("HOURS") or 0) or None)
    ap.add_argument("--dry-run", action="store_true", help="не отправлять, только показать")
    ap.add_argument("--no-translate", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config(Path(args.config))
    hours = args.hours or cfg.get("lookback_hours", 30)

    if not args.dry_run:
        missing = [v for v in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(v)]
        if missing:
            log.error("Не заданы переменные: %s", ", ".join(missing))
            return 2

    seen = load_seen()
    raw, ok_feeds, failed_feeds = collect(cfg, hours)
    log.info("Лент прочитано: %d, не открылось: %d, материалов: %d", ok_feeds, failed_feeds, len(raw))

    if ok_feeds == 0:
        msg = ["<b>⚠️ Сводка не собрана</b>\nНи одна новостная лента не открылась. "
               "Попробую в следующий запуск; если повторится — проверьте журнал GitHub Actions."]
        if args.dry_run:
            print(msg[0])
        else:
            send_telegram(msg)
        return 1

    candidates, skipped = select(raw, cfg, seen)
    resolve_google_links(candidates)
    # после раскрытия ссылок Google сверяемся с отправленным ещё раз — уже по адресу статьи
    fresh = [c for c in candidates if c.url not in seen]
    skipped += len(candidates) - len(fresh)
    items = enrich(fresh, cfg)
    log.info("После проверки текстов осталось: %d", len(items))

    if items and not args.no_translate:
        translate(items, cfg.get("translate_to", "ru"))

    if not items and not cfg.get("send_empty", True):
        return 0
    messages = build_messages(items, cfg, hours, skipped, failed_feeds)

    if args.dry_run:
        print("\n\n────────── следующее сообщение ──────────\n\n".join(messages))
        return 0
    send_telegram(messages)
    save_seen(seen, items)
    log.info("Отправлено сообщений: %d", len(messages))
    return 0


if __name__ == "__main__":
    sys.exit(main())

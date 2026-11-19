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
    flag: str = ""                 # флаг страны для поста рубрики
    state_media: bool = False      # госСМИ — в посте помечается «(госиздание)»
    quotes: list[str] = field(default_factory=list)
    verified: bool | None = None   # True — нашли упоминание в тексте; None — текст не открылся
    title_tr: str = ""
    quotes_tr: list[str] = field(default_factory=list)
    text: str = ""                 # текст статьи (для заготовок рубрики)
    prep: dict = field(default_factory=dict)   # заготовки для пересказа

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
                outlet_index.append((d.lower(), o["name"], region,
                                     o.get("flag", ""), bool(o.get("state", False))))
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


def find_outlet(domain: str, cfg: dict) -> tuple[str, str, str, bool] | None:
    for pattern, name, region, flag, state in cfg["_outlet_index"]:
        if domain_matches(domain, pattern):
            return name, region, flag, state
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


def google_feed_urls(cfg: dict, hours: int) -> list[tuple[str, str]]:
    urls = []
    when = "2d" if hours <= 48 else "7d"
    for lang, lc in cfg["languages"].items():
        for group in chunked(lc["keywords"], KEYWORDS_PER_QUERY):
            q = quote(build_query(group) + f" when:{when}")
            for ed in lc["editions"]:
                urls.append((lang, f"https://news.google.com/rss/search?q={q}"
                                   f"&hl={ed['hl']}&gl={ed['gl']}&ceid={quote(ed['ceid'])}"))
    return urls


def bing_feed_urls(cfg: dict, hours: int) -> list[tuple[str, str]]:
    urls = []
    interval = "7" if hours <= 24 else "8"   # 7 — последние сутки, 8 — неделя
    for lang, lc in cfg["languages"].items():
        b = lc.get("bing")
        if not b:
            continue
        for group in chunked(lc["keywords"], KEYWORDS_PER_QUERY):
            q = quote(build_query(group))
            urls.append((lang, f"https://www.bing.com/news/search?q={q}&format=rss"
                               f"&qft=interval%3d%22{interval}%22&setlang={b['setlang']}&cc={b['cc']}"))
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
        jobs += [("google", lang, u) for lang, u in google_feed_urls(cfg, hours)]
    if cfg["sources"].get("bing_news", True):
        jobs += [("bing", lang, u) for lang, u in bing_feed_urls(cfg, hours)]

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
            it.outlet, it.region, it.flag, it.state_media = found
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
            it.text = text[:12000]
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


# Бесплатные переводчики пробуются по очереди: если первый не ответил, берётся следующий.
LANG_CODES = {"en": "en", "ja": "ja", "ko": "ko", "zh-Hans": "zh-CN", "zh-Hant": "zh-TW"}
TRANSLATION_STATS = {"total": 0, "ok": 0, "engines": set()}


def tr_google_gtx(text: str, src: str, target: str) -> str:
    r = requests.get("https://translate.googleapis.com/translate_a/single",
                     params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text},
                     headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])


def tr_google_web(text: str, src: str, target: str) -> str:
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source="auto", target=target).translate(text) or ""


def tr_mymemory(text: str, src: str, target: str) -> str:
    r = requests.get("https://api.mymemory.translated.net/get",
                     params={"q": text[:500], "langpair": f"{LANG_CODES.get(src, 'en')}|{target}"},
                     timeout=20)
    r.raise_for_status()
    data = r.json()
    if str(data.get("responseStatus")) != "200":
        raise RuntimeError(data.get("responseDetails"))
    return data["responseData"]["translatedText"] or ""


FREE_ENGINES = [("Google", tr_google_gtx), ("Google (веб)", tr_google_web), ("MyMemory", tr_mymemory)]


def translate_free(pairs: list[tuple[str, str]], target: str) -> list[str]:
    failures = {name: 0 for name, _ in FREE_ENGINES}
    out = []
    for text, src in pairs:
        result = ""
        for name, fn in FREE_ENGINES:
            if failures[name] >= 3:          # переводчик трижды подряд не ответил — больше не ждём его
                continue
            try:
                result = (fn(text, src, target) or "").strip()
            except Exception as exc:
                log.info("Переводчик %s не ответил: %s", name, exc)
                result = ""
            if result:
                failures[name] = 0
                TRANSLATION_STATS["engines"].add(name)
                break
            failures[name] += 1
        out.append(result)
        time.sleep(0.15)
    return out


def translate(items: list[Item], target: str) -> None:
    pairs: list[tuple[str, str]] = []
    for it in items:
        pairs.append((it.title, it.lang))
        pairs.extend((q, it.lang) for q in it.quotes)
    pairs = list(dict.fromkeys(p for p in pairs if p[0]))
    if not pairs:
        return
    texts = [t for t, _ in pairs]
    translated = translate_claude(texts, target)
    if translated:
        TRANSLATION_STATS["engines"].add("Claude")
    else:
        translated = translate_free(pairs, target)
    TRANSLATION_STATS["total"] = len(texts)
    TRANSLATION_STATS["ok"] = sum(1 for t in translated if t)
    log.info("Переведено %d из %d (%s)", TRANSLATION_STATS["ok"], len(texts),
             ", ".join(sorted(TRANSLATION_STATS["engines"])) or "ни один переводчик не ответил")
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
    total, ok = TRANSLATION_STATS["total"], TRANSLATION_STATS["ok"]
    if total and ok == 0:
        footer += " · ⚠️ перевод не удался: ни один переводчик не ответил"
    elif total and ok < total:
        footer += f" · переведено {ok} из {total} фрагментов"
    blocks.append(footer + "</i>")

    return chunk_blocks(blocks)


def chunk_blocks(blocks: list[str]) -> list[str]:
    """Склеивает блоки в сообщения не длиннее лимита Telegram."""
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


# ---------------------------------------------------------------- пост рубрики

CAPTION_LIMIT = 1024   # лимит подписи к фото у ботов Telegram
TLD_FLAGS = {"jp": "🇯🇵", "kr": "🇰🇷", "cn": "🇨🇳", "tw": "🇹🇼", "hk": "🇭🇰", "sg": "🇸🇬",
             "in": "🇮🇳", "vn": "🇻🇳", "th": "🇹🇭", "id": "🇮🇩", "ph": "🇵🇭", "my": "🇲🇾",
             "mn": "🇲🇳", "au": "🇦🇺", "uk": "🇬🇧", "de": "🇩🇪", "fr": "🇫🇷", "it": "🇮🇹",
             "es": "🇪🇸", "nl": "🇳🇱", "ua": "🇺🇦", "kz": "🇰🇿", "ca": "🇨🇦"}
LANG_FLAGS = {"ja": "🇯🇵", "ko": "🇰🇷", "zh-Hans": "🇨🇳", "zh-Hant": "🇹🇼"}


def tg_len(html_text: str) -> int:
    """Длина так, как её считает Telegram: видимый текст в единицах UTF-16."""
    visible = html.unescape(re.sub(r"<[^>]+>", "", html_text))
    return len(visible.encode("utf-16-le")) // 2


def guess_flag(it: Item) -> str:
    if it.flag:
        return it.flag
    tld = it.domain.rsplit(".", 1)[-1]
    return TLD_FLAGS.get(tld) or LANG_FLAGS.get(it.lang) or "🌐"


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", (text or "").lower()) if len(w) > 3}


def same_story(a: Item, b: Item) -> bool:
    wa, wb = _words(a.title_tr or a.title), _words(b.title_tr or b.title)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.45


def rank_for_post(items: list[Item]) -> list[Item]:
    """Издания из списка — первыми; затем подтверждённые по тексту; затем свежие.
    Из одного издания и об одном сюжете берём только один материал."""
    def score(it: Item):
        return ((2 if it.outlet else 0) + (1 if it.verified else 0) + (1 if it.quotes else 0),
                it.published.timestamp())
    picked: list[Item] = []
    for it in sorted(items, key=score, reverse=True):
        name = it.outlet or it.domain
        if any((p.outlet or p.domain) == name for p in picked):
            continue
        if any(same_story(p, it) for p in picked):
            continue
        picked.append(it)
    return picked


def weekly_line(it: Item, max_chars: int) -> str:
    name = it.outlet or it.source_name or it.domain
    summary = (it.title_tr or it.title).strip().rstrip(".")
    if len(summary) > max_chars:
        summary = summary[:max_chars].rsplit(" ", 1)[0] + "…"
    state = " (госиздание)" if it.state_media else ""
    return (f'{guess_flag(it)} <b><a href="{html.escape(it.url, quote=True)}">{esc(name)}</a></b>'
            f"{state} — {esc(summary)}")


def build_weekly_post(items: list[Item], cfg: dict) -> tuple[str, list[Item], list[Item]]:
    """Возвращает (подпись к обложке, сюжеты в посте, запасные сюжеты)."""
    w = cfg.get("weekly", {})
    max_items = w.get("max_items", 6)
    max_chars = w.get("max_summary_chars", 150)
    intro = (w.get("intro") or "").strip()
    tail = "\n\n".join(p for p in [(w.get("hashtag") or "").strip(), (w.get("footer") or "").strip()] if p)

    def assemble(lines: list[str]) -> str:
        return "\n\n".join(p for p in [intro, "\n\n".join(lines), tail] if p)

    ranked = rank_for_post(items)
    lines, used = [], []
    for it in ranked:
        # в публичный пост — только то, где упоминание подтверждено текстом статьи
        if not (it.verified or it.quotes):
            continue
        if len(used) >= max_items:
            break
        line = weekly_line(it, max_chars)
        if tg_len(assemble(lines + [line])) <= CAPTION_LIMIT:
            lines.append(line)
            used.append(it)
    reserve = [it for it in ranked if it not in used][: w.get("reserve_items", 8)]
    return assemble(lines), used, reserve


# --- заготовки для пересказа: цифры, цитата эксперта, первоисточник, оговорки

UNITS = (r"%|％|percent|per cent|million|billion|trillion|thousand|mln|bn|km|kilomet(?:er|re)s?|"
         r"met(?:er|re)s?|tons?|tonnes?|passengers|people|visitors|tourists|trips|flights|containers|"
         r"TEU|yuan|rubles?|roubles?|dollars|won|yen|hectares?|"
         r"万|億|亿|千万|百万|명|만|억|천|원|엔|위안|달러|톤|킬로미터|미터|人|元|円|ドル|トン|キロ|メートル|件|台|隻|척|편")
NUM_RE = re.compile(rf"(?:[$€¥£₽]\s?\d[\d,.]*(?:\s?(?:million|billion|mln|bn))?)|"
                    rf"(?:\d[\d,.]*\s?(?:{UNITS}))", re.IGNORECASE)
QUOTE_RE = re.compile(r"[“\"«「『]([^”\"»」』]{12,240})[”\"»」』]")
SPEECH_RE = re.compile(r"\b(said|says|told|added|noted|believes|according to)\b|表示|指出|认为|说|述べ|語っ|話し|"
                       r"によると|말했|밝혔|강조했|설명했", re.IGNORECASE)
NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+){1,2})\b")
SOURCE_RES = [
    re.compile(r"\baccording to (?:the |a )?([A-Z][\w'’-]*(?: [A-Z][\w'’-]*){0,4})"),
    re.compile(r"([^\s、。「」]{2,16})(?:の報道)?によると"),
    re.compile(r"([^\s,.]{2,16})에 따르면"),
    re.compile(r"据([^\s，。“”]{2,16}?)(?:报道|消息|称|介绍)"),
]
CAVEATS = [
    (re.compile(r"\b(no|not yet|yet to be)\b[^.]{0,50}\b(date|timeline|schedule)\b|"
                r"\b(has|have|was|were)(n't| not) (yet )?(been )?(announced|specified|disclosed|confirmed|set)\b|"
                r"未定|明らかにしていない|未公表|尚未|待定|미정|밝히지 않|확정되지 않", re.IGNORECASE),
     "сроки или детали не названы"),
    (re.compile(r"\b(could|would|may|might|expected to|is set to|plans? to|aims? to|projected|forecast)\b|"
                r"見通し|予定|可能性|方針|计划|预计|有望|将会|전망|예정|계획|가능성", re.IGNORECASE),
     "есть прогнозы или планы — пересказывать как «может / планируется», не «будет»"),
]
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])|\n+")


def _sentences(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", s).strip() for s in SENT_SPLIT.split(text or "") if len(s.strip()) > 15]


def _shorten_quote(q: str) -> str:
    """Цитата до ~15 слов (для CJK — до 50 знаков), обрезка по границе фразы."""
    q = q.strip(" ,.;:")
    if re.search(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]", q):
        return q if len(q) <= 50 else q[:50].rsplit("，", 1)[0].rsplit("、", 1)[0] + "…"
    words = q.split()
    if len(words) <= 15:
        return q
    cut = " ".join(words[:15])
    return (cut.rsplit(",", 1)[0] if "," in cut[20:] else cut) + "…"


def build_prep(it: Item, kw: re.Pattern) -> dict:
    """Короткие заготовки из текста статьи — не перевод абзацев, а опорные факты."""
    text = it.text or it.snippet or ""
    sents = _sentences(text)
    prep: dict = {"facts": [], "quote": "", "speaker": "", "source": "", "caveats": []}

    # цифры: сначала из предложений с упоминанием региона, затем из начала статьи
    ranked = sorted(enumerate(sents), key=lambda p: (not kw.search(p[1]), p[0]))
    for _, sent in ranked:
        m = NUM_RE.search(sent)
        if not m or re.fullmatch(r"(19|20)\d\d", m.group(0).strip()):
            continue
        frag = trim_around(sent, m, 110)
        if frag not in prep["facts"]:
            prep["facts"].append(frag)
        if len(prep["facts"]) >= 3:
            break

    # цитата: прямая речь в предложении с глаголом речи
    for sent in sents:
        for qm in QUOTE_RE.finditer(sent):
            if SPEECH_RE.search(sent) or kw.search(sent):
                prep["quote"] = _shorten_quote(qm.group(1))
                rest = sent[:qm.start()] + " " + sent[qm.end():]
                nm = NAME_RE.search(rest)
                prep["speaker"] = nm.group(1) if nm else ""
                break
        if prep["quote"]:
            break

    for rx in SOURCE_RES:
        m = rx.search(text)
        if m:
            src = m.group(1).strip(" ,.")
            if 2 <= len(src) <= 60 and src.lower() not in ("the", "a"):
                prep["source"] = src
                break

    for rx, label in CAVEATS:
        if rx.search(text):
            prep["caveats"].append(label)
    return prep


def prepare_items(items: list[Item], cfg: dict, translate_on: bool = True) -> None:
    kw = cfg["_keyword_re"]
    for it in items:
        it.prep = build_prep(it, kw)
    if not translate_on:
        return
    pairs = []
    for it in items:
        p = it.prep
        pairs += [(f, it.lang) for f in p["facts"]]
        pairs += [(x, it.lang) for x in (p["quote"], p["speaker"], p["source"]) if x]
    pairs = list(dict.fromkeys(pairs))
    if not pairs:
        return
    texts = [t for t, _ in pairs]
    translated = translate_claude(texts, cfg.get("translate_to", "ru")) or \
        translate_free(pairs, cfg.get("translate_to", "ru"))
    tr = dict(zip(texts, translated))
    for it in items:
        p = it.prep
        p["facts_tr"] = [tr.get(f, "") for f in p["facts"]]
        for k in ("quote", "speaker", "source"):
            p[f"{k}_tr"] = tr.get(p[k], "") if p[k] else ""


def render_prep(n: int, it: Item, tz: ZoneInfo) -> str:
    p = it.prep or {}
    when = it.published.astimezone(tz).strftime("%d.%m")
    name = it.outlet or it.source_name or it.domain
    state = " (госиздание)" if it.state_media else ""
    lines = [f"<b>{n}. {guess_flag(it)} {esc(name)}</b>{state} · {when}",
             f"<b>Суть (черновик):</b> {esc(it.title_tr or it.title)}"]
    facts = [t or o for o, t in zip(p.get("facts", []), p.get("facts_tr", p.get("facts", [])))]
    if facts:
        lines.append("<b>Цифры:</b> " + " · ".join(esc(f) for f in facts))
    if p.get("quote"):
        who = p.get("speaker_tr") or p.get("speaker")
        q_tr = p.get("quote_tr", "")
        line = f"<b>Цитата:</b> «{esc(q_tr or p['quote'])}»"
        if q_tr and not same_text(q_tr, p["quote"]):
            line += f" <i>(ориг.: {esc(p['quote'])})</i>"
        line += f" — {esc(who)} (проверить)" if who else " — автор не определён, проверить"
        lines.append(line)
    if p.get("source"):
        lines.append(f"<b>Ссылается на:</b> {esc(p.get('source_tr') or p['source'])}")
    if p.get("caveats"):
        lines.append("<b>⚠️ Оговорки:</b> " + "; ".join(esc(c) for c in p["caveats"]))
    if it.verified is None:
        lines.append("<i>⚠️ Текст статьи не открылся — упоминание и детали не проверены.</i>")
    lines.append(f'<a href="{html.escape(it.url, quote=True)}">→ статья</a>')
    return "\n".join(lines)


STYLE_HINT = ("Правила: 1) суть своими словами — что и где; 2) главная цифра или оговорка; "
              "3) цитата — по желанию, до 15 слов, с автором; 4) модальность как в оригинале; "
              "5) пункт — до 250 знаков.")


def build_weekly_notes(used: list[Item], reserve: list[Item], cfg: dict) -> list[str]:
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    blocks = ["<b>Черновик рубрики готов</b>\n"
              "В посте выше — машинный перевод заголовков. Ниже заготовки к каждому сюжету: "
              "перепишите пункты по правилам рубрики или вставьте это сообщение в чат с Claude "
              "со словами «оформи рубрику».\n<i>" + STYLE_HINT + "</i>"]
    if used:
        blocks.append("<b>━━ Сюжеты в посте ━━</b>")
        blocks.extend(render_prep(n, it, tz) for n, it in enumerate(used, 1))
    if reserve:
        blocks.append("<b>━━ Запасные сюжеты ━━</b>")
        blocks.extend(render_prep(n, it, tz) for n, it in enumerate(reserve, len(used) + 1))
    return chunk_blocks(blocks)


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


def send_photo(path: Path, caption: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    with open(path, "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                          data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                          files={"photo": (path.name, f, "image/jpeg")}, timeout=60)
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
    ap.add_argument("--weekly", action="store_true",
                    help="собрать еженедельный пост рубрики вместо ежедневной сводки")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config(Path(args.config))
    weekly = args.weekly or os.environ.get("MODE", "").strip().lower() == "weekly"
    if weekly:
        wcfg = cfg.get("weekly", {})
        hours = args.hours or wcfg.get("lookback_hours", 168)
        cfg["include_other_sources"] = wcfg.get("include_other_sources", True)
    else:
        hours = args.hours or cfg.get("lookback_hours", 30)

    if not args.dry_run:
        missing = [v for v in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(v)]
        if missing:
            log.error("Не заданы переменные: %s", ", ".join(missing))
            return 2

    # для поста рубрики берём всё за неделю, даже то, что уже было в ежедневных сводках
    seen = {} if weekly else load_seen()
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

    if weekly:
        return publish_weekly(items, cfg, args.dry_run, not args.no_translate)

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


def publish_weekly(items: list[Item], cfg: dict, dry_run: bool, translate_on: bool = True) -> int:
    caption, used, reserve = build_weekly_post(items, cfg)
    prepare_items(used + reserve, cfg, translate_on)
    if not used:
        msg = "<b>Рубрика «О чём пишут иностранные СМИ»</b>\nЗа неделю подходящих материалов не нашлось."
        print(msg) if dry_run else send_telegram([msg])
        return 0
    notes = build_weekly_notes(used, reserve, cfg)
    cover = BASE_DIR / cfg.get("weekly", {}).get("cover", "cover.jpg")
    log.info("Пост рубрики: %d сюжетов, %d знаков из %d", len(used), tg_len(caption), CAPTION_LIMIT)
    if dry_run:
        print(f"[обложка: {cover.name}{'' if cover.exists() else ' — ФАЙЛ НЕ НАЙДЕН'}]\n")
        print(caption)
        print("\n\n────────── служебное сообщение ──────────\n\n" + "\n\n".join(notes))
        return 0
    if cover.exists():
        send_photo(cover, caption)
    else:
        send_telegram([f"⚠️ Обложка {cover.name} не найдена — пост без картинки:\n\n{caption}"])
    send_telegram(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())

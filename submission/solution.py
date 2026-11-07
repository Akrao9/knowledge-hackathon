"""Index Markdown sections and answer from the selected website snapshot."""
import os
import re
import sys
from calendar import monthrange
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from math import log1p
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlsplit

# The scorer puts its harness on the import path; a checkout of the kit finds it in a parent folder.
_HARNESS = next((d / "harness" for d in Path(__file__).resolve().parents if (d / "harness" / "contract.py").exists()), None)
if _HARNESS is not None and str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))
import markdown_blocks  # noqa: E402
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402
from luna import select_passages  # noqa: E402
from rate_tables import RateIndex  # noqa: E402
from snapshot_facts import SnapshotFacts  # noqa: E402

RANK_WORD = re.compile(r"[a-z0-9]+")
MODEL_YEAR = re.compile(r"(?:19|20)[0-9]{2}")
MAX_SECTION_CHARS = 5000
MAX_FALLBACK_CHARS = 600
# Search answers from the top 8 sections. Luna also sees the next 8, each by its two passages that share
# the rarest question words, so a section just below the cut can still answer without doubling the prompt.
SEARCH_SECTIONS = 8
LUNA_SECTIONS = 16
COMPACT_PASSAGES = 2
STOP_WORDS = frozenset(
    "a an and are as at be can could did do does for from how i in is it me my of on or s "
    "tell that the their this to was were what when where which who will with would you your".split()
    # Words that only frame a question; matching them pulls in unrelated articles.
    + "about also any generally get give going got has have know like need now offer offered offers please provide "
    "typically use using usually want way work".split()
    # Leftovers of contractions such as "I'm", "we'll" and "don't".
    + "d ll m re t ve".split()
)
WORD_FORMS = {
    "accounts": "account",
    "branches": "branch",
    "cars": "vehicle",
    "car": "vehicle",
    "cd": "certificate",
    "cds": "certificate",
    "certificates": "certificate",
    "fees": "fee",
    "loans": "loan",
    "months": "month",
    "rates": "rate",
    "vehicles": "vehicle",
    "years": "year",
}
PHONE = re.compile(r"(?<!\d)(?:1[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.]\d{4}(?!\d)")
# "Phone number" or "call", not the phone itself ("phone wallets", "pay with my phone"). "Extension" counts only
# beside a word for dialing or reaching someone, so browser or scholarship extensions keep ordinary search.
PHONE_QUESTION = re.compile(
    r"\bphone (?:number|line)s?\b|\b(?:telephone|toll[- ]free|call)\b"
    r"|\bextensions?\b[^?]*\b(?:dial\w*|reach\w*|phone|contact\w*)\b"
    r"|\b(?:dial\w*|reach\w*|phone|contact\w*)\b[^?]*\bextensions?\b",
    re.IGNORECASE,
)
NOT_A_PHONE_QUESTION = re.compile(r"\b(?:routing|aba|account number|card number)\b", re.IGNORECASE)
# A search term no word can produce: sections with a phone number carry it, and questions asking for one
# search only those sections.
PHONE_TERM = "<phone>"
HOURS_LABEL = re.compile(r"^\*\*(Lobby|Drive[- ]up)\*\*[ \t]*", re.IGNORECASE | re.MULTILINE)
HOURS_DAY = re.compile(
    r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b[^:\n]{0,24}:\s+\S",
    re.IGNORECASE,
)
HOURS_QUESTION = re.compile(r"\b(?:hours|what time)\b", re.IGNORECASE)
# The service a schedule is for, by word stem, so "lobbies" and "drive-thru" match "Lobby" and "Drive-up" labels.
HOURS_SERVICE = re.compile(r"\b(lobb|driv)(?:y|ies|e)\b", re.IGNORECASE)
# "What time do lobbies open?", "When are lobbies open?", not "When can I open an account?" or "How many hours".
SCHEDULE_QUESTION = re.compile(r"\b(?:hours|what time)\b|\bwhen (?:is|are)\b[^?]*\bopen\b", re.IGNORECASE)
NOT_A_SCHEDULE_QUESTION = re.compile(r"\bhow (?:many|much|long)\b|\d\s*hours\b", re.IGNORECASE)
SCHEDULE_TERM = "<schedule>"
TABLE_LINE = re.compile(r"^\s*\|")
TABLE_RULE = re.compile(r"^[\s|:-]*-{3}[\s|:-]*$")
BLANK_LINES = re.compile(r"\n\s*\n(?:\s*\n)+")
MARKDOWN_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^)\s]*\)")
TEASER_HEADINGS = frozenset({"Handpicked for You"})
FAQ_QUESTION = re.compile(r"(?:[-*+]\s+)?\**\[?([^\[\]\n]{5,200}?\?)\]?(?:\([^)\s]*\))?\**")
LINKED_ITEM = re.compile(r"^(?:[-*+]|\d+\\?\.)\s+\[")
LINKED_HEADING = re.compile(r"^\**\[[^\]\n]+\]\((\S+?)(?:\s+\"[^\"]*\")?\)\**$")
# Articles, press releases and calculators explain or announce; product pages state current terms.
ARTICLE_PATHS = ("/learn/", "/newsroom/", "/business/resources/", "/business-resources/", "/calculators/", "/hubfs/")
DATED_PATH = re.compile(r"^/(?:19|20)\d{2}/")
ARTICLE_WEIGHT = 0.6
# A question that doesn't say "business" is about the personal product, as in the rate tables, so business
# pages rank lower and answer only when no personal page does.
BUSINESS_WEIGHT = 0.6
MARKDOWN_LINK = re.compile(r"(?<!!)\[([^\]\n]+)\]\(([^)\s]+)\)")
LINK_QUESTION = re.compile(r"\b(?:link|url)\b", re.IGNORECASE)
LINK_WORDS = frozenset("dupaco link look see show should use url view web website".split())
# "the Mullan Avenue branch", "Galena Branch": a named location the answer must be about.
# Branch questions name a place before "branch" ("the Mullan Avenue branch") or after a preposition
# ("lobby hours at Mullan Avenue"). The answer must then be about that place.
BRANCH_QUESTION = re.compile(r"\b(?:hours?|open|closes?|closed|closing|address|located|location|directions)\b", re.I)
PLACE_BEFORE_BRANCH = re.compile(r"((?:[\w.'&-]+\s+){1,3})(?:branch|location|office)\b(?!es)", re.IGNORECASE)
PLACE_AFTER_PREPOSITION = re.compile(r"\b(?:at|for|in|near)\s+((?:[\w.'&-]+\s*){1,3})", re.IGNORECASE)
PLACE_STOP_WORDS = frozenset(
    "a about all an and any are at banking branch branches can close closed closes closing do does dupaco "
    "each every for full general hour hours how in is it its location locations lobby main member members "
    "my near now of office on open our s same service still than that the their this time times to today "
    "tomorrow up week weekend weekends what when where which who will with you your drive "
    "monday tuesday wednesday thursday friday saturday sunday holiday holidays morning afternoon evening".split()
)
STREET_WORDS = {"ave": "avenue", "st": "street", "rd": "road", "dr": "drive", "blvd": "boulevard", "ln": "lane"}
MONTHS = {name: number for number, name in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
# "as of May 2026", "as of May 15, 2026", "as of 2026-05-15", "as of 5/15/2026"
AS_OF_DATE = re.compile(
    r"\bas of\s+(?:(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(?:(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+)?(?P<year>(?:19|20)\d{2})"
    r"|(?P<iso>(?:19|20)\d{2}-\d{2}-\d{2})|(?P<us_month>\d{1,2})/(?P<us_day>\d{1,2})/(?P<us_year>(?:19|20)\d{2}))\b",
    re.IGNORECASE,
)
# A member's own balance or an individual loan decision, which no public page can answer.
PERSONAL_QUESTION = re.compile(
    # Asking for the balance itself ("What is the current balance in my checking account?"), not how to check it.
    r"\bwhat(?:'s| is)\s+(?:the\s+)?(?:current\s+|exact\s+)?balance\s+(?:in|of|on)\s+my\b"
    r"|\bhow much (?:money )?(?:is|do i have) in my\b"
    # A decision about this member ("What APR would I be approved for?", "Will I qualify?"), not a published
    # requirement ("What credit score would I need to qualify?", "What do I need to be approved?").
    r"|\b(?:would|will)\s+i\s+(?:personally\s+)?(?:be|get)\s+approved\b|\b(?:would|will)\s+i\s+(?:personally\s+)?qualify\b"
    r"|\bam\s+i\s+(?:pre-?)?(?:approved|qualified)\b"
    # An approval for one applicant's stated score ("approved ... with a 612 credit score").
    r"|\b(?:approved|qualify)\b[^?]*\b\d{3}\s+credit score\b|\b\d{3}\s+credit score\b[^?]*\b(?:approved|qualify)\b",
    re.IGNORECASE,
)
PERSONAL_ANSWER = "I don't know. Dupaco's public website doesn't show personal account details or individual loan decisions."
COMPARISON = re.compile(
    r"\bcompare\b|\b(?:all|every|each)\b.{0,100}\b(?:same|identical)\b|\b(?:same|identical)\b.{0,60}\b(?:all|every|each)\b"
    r"|\b(?:differ|vary)\b.{0,60}\b(?:by|between|across|from)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Section:
    section_id: str
    snapshot_id: str
    url: str
    page_title: str
    heading_path: tuple[str, ...]
    text: str
    link_target: Optional[str] = None


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"  # "lobbies" -> "lobby", "policies" -> "policy"
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _wants_phone(question: str) -> bool:
    return bool(PHONE_QUESTION.search(question)) and not NOT_A_PHONE_QUESTION.search(question)


def _wants_schedule(question: str) -> bool:
    return bool(SCHEDULE_QUESTION.search(question)) and not NOT_A_SCHEDULE_QUESTION.search(question)


def _has_schedule(text: str) -> bool:
    """At least two weekday lines ("Monday-Thursday: 8:30 a.m.-5 p.m."), so a section lists opening hours."""
    return sum(bool(HOURS_DAY.match(line.lstrip())) for line in text.splitlines()) >= 2


def _question_terms(question: str, *, phrases: bool = False) -> dict[str, float]:
    """A question's search terms and their weights.

    A plural word also searches its singular at half weight, so "students" finds "Student Loans" while
    pages keep their own words. A question asking for a number to call also searches for phone numbers.
    """
    terms = dict.fromkeys(_rank_terms(question, phrases=phrases), 1.0)
    for term in list(terms):
        if " " not in term:
            terms.setdefault(_singular(term), 0.5)
    if _wants_phone(question):
        terms[PHONE_TERM] = 1.0
    if _wants_schedule(question):
        terms[SCHEDULE_TERM] = 1.0
    return terms


def _passage_terms(text: str) -> set[str]:
    markers = {PHONE_TERM} if PHONE.search(text) else set()
    if _has_schedule(text):
        markers.add(SCHEDULE_TERM)
    return _rank_terms(text) | markers


def _rank_terms(text: str, *, phrases: bool = False) -> set[str]:
    words = [WORD_FORMS.get(word, word) for word in RANK_WORD.findall(text.lower()) if word not in STOP_WORDS]
    terms = set(words)
    if phrases:
        terms.update(f"{left} {right}" for left, right in zip(words, words[1:]))
    return terms


def _is_teaser(url: str, headings: tuple[str, ...], text: str) -> bool:
    """Blurbs that advertise other articles ("Handpicked for You", "Keep reading", card titles linking away)."""
    if any(heading in TEASER_HEADINGS for heading in headings) or "[Keep reading](" in text:
        return True
    card = LINKED_HEADING.match(headings[-1]) if headings else None
    return bool(card) and urlsplit(card[1]).path.rstrip("/") != urlsplit(url).path.rstrip("/")


def _has_prose(passage: str) -> bool:
    """True unless the passage is only images and links, or a bulleted list of links to other pages."""
    lines = [line.strip() for line in passage.splitlines() if line.strip()]
    if len(lines) >= 2 and all(LINKED_ITEM.match(line) for line in lines):
        return False
    stripped = MARKDOWN_LINK.sub("", MARKDOWN_IMAGE.sub("", passage))
    return len(RANK_WORD.findall(stripped.lower())) >= 4


def _is_business(url: str) -> bool:
    return urlsplit(url).path.startswith(("/business/", "/business-"))


def _is_article(url: str) -> bool:
    path = urlsplit(url).path
    return path.startswith(ARTICLE_PATHS) or bool(DATED_PATH.match(path))


def _markdown_sections(markdown: str) -> Iterator[tuple[tuple[str, ...], str]]:
    """Yield the content under each heading with its parent headings."""
    lines = markdown.splitlines()
    headings: list[tuple[int, str]] = []
    start = 0
    for index, (level, title) in sorted(markdown_blocks.parse(markdown).headings.items()):
        text = "\n".join(lines[start:index]).strip("\n")
        if text.strip():
            yield tuple(title for _, title in headings), text
        while headings and headings[-1][0] >= level:
            headings.pop()
        if title:
            headings.append((level, title))
        start = index + 1

    text = "\n".join(lines[start:]).strip("\n")
    if text.strip():
        yield tuple(title for _, title in headings), text


def _search_sections(markdown: str, path: str) -> Iterator[tuple[tuple[str, ...], str, str]]:
    for headings, text in _markdown_sections(markdown):
        locator_label = ""
        if path == "/contact":
            fields = text.split("\n\n", 2)
            if len(fields) >= 2 and fields[0].strip() == "Dupaco Branch":
                address = fields[1].rsplit(",", 3)
                if len(address) == 4:
                    locator_label = f"{address[1].strip()} branch address"
        yield headings, text, locator_label

        for label in HOURS_LABEL.finditer(text):
            end = label.end()
            days = 0
            for line in text[end:].splitlines(keepends=True):
                if line.strip():
                    if not HOURS_DAY.match(line.lstrip()):
                        break
                    days += 1
                end += len(line)
            if days >= 2:
                kind = label[1].lower().replace(" ", "-")
                yield headings, text[label.start():end].rstrip(), f"Branch {kind} hours"


def _navigation_sections(section: Section) -> Iterator[Section]:
    source = urlsplit(section.url)
    for index, link in enumerate(MARKDOWN_LINK.finditer(section.text), start=1):
        try:
            target = urljoin(section.url, link[2])
            destination = urlsplit(target)
        except ValueError:
            continue
        if (
            destination.scheme != source.scheme or destination.netloc != source.netloc
            or destination.path.rstrip("/") != source.path.rstrip("/") or not destination.fragment
        ):
            continue
        yield Section(
            section_id=f"{section.section_id}:link:{index}",
            snapshot_id=section.snapshot_id,
            url=section.url,
            page_title=section.page_title,
            heading_path=(*section.heading_path, f"Link {link[1]} {destination.fragment}"),
            text=link[0],
            link_target=target,
        )


def _rank_links(question: str, links: list[Section]) -> list[Section]:
    terms = _rank_terms(question) - LINK_WORDS
    if not terms:
        return []
    minimum_hits = min(2, len(terms))
    best_by_target: dict[str, tuple[int, Section]] = {}
    for link in links:
        hits = len(terms & _rank_terms(" ".join(link.heading_path)))
        if hits < minimum_hits or link.link_target is None:
            continue
        previous = best_by_target.get(link.link_target)
        if previous is None or hits > previous[0]:
            best_by_target[link.link_target] = (hits, link)
    return [link for _, link in sorted(best_by_target.values(), key=lambda match: -match[0])[:3]]


def _with_subject(question: str, section: Section, quote: str) -> str:
    """Name the product or topic when a prose quote answers without saying what it is about.

    The nearest heading above the quote that carries question words the quote lacks is added, so a
    "Flexible Repayment" paragraph on the student loan page is named by the page's own heading. A
    heading with a number or a link is never used, so it cannot add a stale figure or year to the answer.
    """
    if section.link_target or quote.lstrip().startswith(("|", "**")):
        return quote
    missing = _rank_terms(question) - _rank_terms(quote)
    for heading in reversed(section.heading_path or (section.page_title,)):
        heading = heading.strip("*# ")
        if not heading or "](" in heading or any(char.isdigit() for char in heading):
            continue
        if missing & _rank_terms(heading):
            return f"{heading}: {quote}"
    return quote


def _numbered_passages(section: Section) -> list[str]:
    """The pieces of a section Luna can choose: a short section whole, a long one by paragraph and table row."""
    text = section.text[:MAX_SECTION_CHARS]
    if len(text) <= MAX_FALLBACK_CHARS:
        return [text.strip()]
    pieces = []
    for paragraph in text.split("\n\n"):
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if lines and all(TABLE_LINE.match(line) for line in lines):
            pieces.extend(line for line in lines if not TABLE_RULE.fullmatch(line))
        elif lines:
            pieces.append(paragraph.strip())
    return pieces


def _best_pieces(pieces: list[str], rarity: dict[str, float], count: int) -> list[int]:
    """Indexes of the pieces sharing the rarest question words, in page order; none if no piece shares any."""
    scores = [(sum(weight for term, weight in rarity.items() if term in _passage_terms(text)), index)
              for index, text in enumerate(pieces)]
    best = sorted((item for item in scores if item[0] > 0), key=lambda item: (-item[0], item[1]))[:count]
    return sorted(index for _, index in best)


def _quote_answer(question: str, evidence: list[tuple[Section, str]]) -> Answer:
    limit = 3 if COMPARISON.search(question) else 1
    quotes = []
    sources = []
    seen = set()
    for section, quote in evidence:
        # Images say nothing to a member reading the answer; the source keeps the quote as published.
        shown = BLANK_LINES.sub("\n\n", MARKDOWN_IMAGE.sub("", quote)).strip()
        key = tuple(RANK_WORD.findall(shown.lower()))
        if not shown or key in seen:
            continue
        seen.add(key)
        if section.link_target and section.link_target not in quote:
            quotes.append(f"[View page]({section.link_target})")
        else:
            quotes.append(_with_subject(question, section, shown))
        sources.append(Source(url=section.url, snapshot_id=section.snapshot_id, quote=quote))
        if len(quotes) == limit:
            break
    return Answer(text="\n\n".join(quotes), sources=sources)


def _question_date(question: str) -> Optional[str]:
    """The date a question asks about in its own words, if any; "as of May 2026" means the end of May."""
    match = AS_OF_DATE.search(question)
    if not match:
        return None
    if match["iso"]:
        year, month, day = (int(part) for part in match["iso"].split("-"))
    elif match["us_year"]:
        year, month, day = int(match["us_year"]), int(match["us_month"]), int(match["us_day"])
    else:
        year, month = int(match["year"]), MONTHS[match["month"][:3].lower()]
        day = int(match["day"]) if match["day"] else monthrange(year, month)[1]
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _place_words(text: str) -> set[str]:
    return {STREET_WORDS.get(word, word) for word in RANK_WORD.findall(text.lower())}


def _named_place(question: str) -> Optional[frozenset[str]]:
    """The words naming a branch or location the question asks about, if any."""
    if not BRANCH_QUESTION.search(question):
        return None
    for pattern in (PLACE_BEFORE_BRANCH, PLACE_AFTER_PREPOSITION):
        for match in pattern.finditer(question):
            words = _place_words(match[1]) - PLACE_STOP_WORDS
            if words and not all(word.isdigit() for word in words):
                return frozenset(words)
    return None


def _mentions(section: Section, place: Optional[frozenset[str]], passage: str) -> bool:
    return place is None or place <= _place_words(f"{section.page_title} {' '.join(section.heading_path)} {passage}")


def _supports_hours(question: str, passage: str) -> bool:
    service = HOURS_SERVICE.search(question)
    label = HOURS_LABEL.match(passage)
    if service and (not label or not label[1].lower().startswith(service[1].lower())):
        return False
    schedule = passage[label.end():] if label else passage
    return any(HOURS_DAY.match(line.lstrip()) for line in schedule.splitlines())


def _ranked_passages(
    question: str, sections: list[Section], rarity: dict[str, float]
) -> Iterator[tuple[float, Section, str]]:
    """Yield passages that share enough words with the question, in section rank order.

    Each passage is scored by the rarity of the question words it shares.
    """
    terms = _rank_terms(question)
    forms = {term: {term, _singular(term)} for term in terms}
    wants_phone = _wants_phone(question)
    if wants_phone:
        forms[PHONE_TERM] = {PHONE_TERM}
    wants_schedule = _wants_schedule(question)
    if wants_schedule:
        forms[SCHEDULE_TERM] = {SCHEDULE_TERM}
    place = _named_place(question)
    needs_schedule = (place is not None and HOURS_QUESTION.search(question)) or _wants_schedule(question)
    for section in sections:
        if len(section.text) <= MAX_FALLBACK_CHARS:
            passages = [section.text]
            heading_terms = _rank_terms(" ".join(section.heading_path))
        else:
            passages = section.text.split("\n\n")
            heading_terms = set()
        faq_terms: set[str] = set()
        for passage in passages:
            quote = passage.strip()[:MAX_FALLBACK_CHARS]
            # An FAQ question line ("- [Can I use both cards in my mobile wallet?](#)") names the topic
            # of the answer paragraph below it, the way a heading does.
            faq = FAQ_QUESTION.fullmatch(quote)
            context, faq_terms = heading_terms | faq_terms, _rank_terms(faq[1]) if faq else set()
            if faq or not quote or not _has_prose(quote) or not _mentions(section, place, quote):
                continue
            if needs_schedule and not _supports_hours(question, quote):
                continue
            if wants_phone and not PHONE.search(quote):
                continue
            found = _passage_terms(quote) | context
            matched = [form & found for form in forms.values() if form & found]
            # A weekly schedule answers "open", "close" and "what time" itself, so it needs one more
            # question word (the service or the place) instead of the usual share of them.
            enough = SCHEDULE_TERM in found if wants_schedule else len(matched) >= 0.4 * len(forms)
            if len(matched) >= 2 and enough:
                yield sum(max(rarity.get(term, 0) for term in hit) for hit in matched), section, quote


def _ranked_fallback(
    question: str, sections: list[Section], rarity: dict[str, float], product_words: frozenset[str] = frozenset(),
    skip_rate_rows: bool = False,
) -> Answer:
    """The best passage, or "I don't know".

    A rate question reaches search only after the rate index read every table and found no single product,
    so a table is not quoted again, and the passage must name the words no table used ("signature loan").
    """
    best = None
    best_score = 0.0
    for score, section, quote in _ranked_passages(question, sections, rarity):
        if skip_rate_rows and any(TABLE_LINE.match(line) and "%" in line for line in quote.splitlines()):
            continue
        named = f"{section.page_title} {' '.join(section.heading_path)} {quote}".lower()
        if not all(word in named for word in product_words):
            continue
        if score > best_score:
            best, best_score = (section, quote), score
    if best is None:
        return Answer(text="I don't know.", confident=False)
    return _quote_answer(question, [best])


def _comparison_answer(question: str, sections: list[Section], rarity: dict[str, float]) -> Answer:
    """Answer "do all branches keep the same hours"-style questions with the top distinct passages.

    This runs locally, without the API, so the same question always shows the same passages.
    """
    evidence = []
    seen = set()
    for _, section, quote in _ranked_passages(question, sections, rarity):
        # Pages type the same schedule with different dashes and spacing; those copies are one passage.
        key = " ".join(quote.replace("–", "-").replace("—", "-").split()).lower()
        if key not in seen:
            seen.add(key)
            evidence.append((section, quote))
        if len(evidence) == 3:
            break
    if not evidence:
        return Answer(text="I don't know.", confident=False)
    return _quote_answer(question, evidence)


class SnapshotKnowledge:
    def __init__(self):
        self.snapshots: list[Snapshot] = []
        self._sections_by_snapshot: dict[str, dict[str, Section]] = {}
        self._postings_by_snapshot: dict[str, dict[str, dict[str, float]]] = {}
        self._links_by_snapshot: dict[str, list[Section]] = {}
        self._rates_by_snapshot: dict[str, RateIndex] = {}
        self._facts_by_snapshot: dict[str, SnapshotFacts] = {}
        self._answers: dict[tuple[str, Optional[str]], Answer] = {}

    def ingest(self, snapshot: Snapshot) -> None:
        self._answers.clear()
        sections: dict[str, Section] = {}
        postings: dict[str, dict[str, float]] = {}
        links: list[Section] = []
        for page in snapshot.pages:
            page_path = urlsplit(page.url).path
            page_terms = _rank_terms(f"{page.title} {page_path}")
            path = page_path.rstrip("/")
            scale = ARTICLE_WEIGHT if _is_article(page.url) else 1
            for headings, text, label in _search_sections(page.markdown, path):
                if _is_teaser(page.url, headings, text):
                    continue
                section_id = f"{snapshot.snapshot_id}:{len(sections) + 1}"
                heading_path = (*headings, label) if label else headings
                section = Section(
                    section_id=section_id,
                    snapshot_id=snapshot.snapshot_id,
                    url=page.url,
                    page_title=page.title,
                    heading_path=heading_path,
                    text=text,
                )
                sections[section_id] = section
                links.extend(_navigation_sections(section))
                heading_terms = _rank_terms(" ".join(headings)) | _rank_terms(label, phrases=True)
                for weight, terms in ((3, heading_terms), (2, page_terms), (1, _passage_terms(text))):
                    for term in terms:
                        matches = postings.setdefault(term, {})
                        matches[section_id] = matches.get(section_id, 0) + weight * scale
        facts = SnapshotFacts(snapshot)
        self._sections_by_snapshot[snapshot.snapshot_id] = sections
        self._postings_by_snapshot[snapshot.snapshot_id] = postings
        self._links_by_snapshot[snapshot.snapshot_id] = links
        self._rates_by_snapshot[snapshot.snapshot_id] = RateIndex(snapshot)
        self._facts_by_snapshot[snapshot.snapshot_id] = facts
        self.snapshots.append(snapshot)

    def _snapshot_for(self, as_of: Optional[str]) -> Optional[Snapshot]:
        for snapshot in reversed(self.snapshots):
            if as_of is None or snapshot.taken_on <= as_of:
                return snapshot
        return None

    def _sections_for(self, as_of: Optional[str] = None) -> dict[str, Section]:
        snapshot = self._snapshot_for(as_of)
        return self._sections_by_snapshot[snapshot.snapshot_id] if snapshot else {}

    def _rarity(self, question: str, snapshot: Snapshot, *, phrases: bool = False) -> dict[str, float]:
        """How rare each question term is in the snapshot, for terms that occur at all."""
        count = len(self._sections_by_snapshot[snapshot.snapshot_id])
        postings = self._postings_by_snapshot[snapshot.snapshot_id]
        rarity = {}
        for term, weight in _question_terms(question, phrases=phrases).items():
            matches = postings.get(term)
            if matches:
                rarity[term] = weight * log1p((count - len(matches) + 0.5) / (len(matches) + 0.5))
                if MODEL_YEAR.fullmatch(term):
                    # The matching table row may express the requested year as a range.
                    rarity[term] *= 0.25
        return rarity

    def _rank_sections(self, question: str, as_of: Optional[str] = None, limit: int = 8) -> list[Section]:
        """Return sections ranked by term rarity and the fields where each term appears."""
        snapshot = self._snapshot_for(as_of)
        if snapshot is None or limit <= 0:
            return []

        sections = self._sections_by_snapshot[snapshot.snapshot_id]
        postings = self._postings_by_snapshot[snapshot.snapshot_id]
        scores: dict[str, float] = {}
        # A question asking for a number to call is answered only by a section that has one, and a
        # question asking when something is open only by a section with a weekly schedule.
        allowed = None
        for wanted, marker in ((_wants_phone(question), PHONE_TERM), (_wants_schedule(question), SCHEDULE_TERM)):
            if wanted:
                with_marker = set(postings.get(marker, {}))
                allowed = with_marker if allowed is None else allowed & with_marker
        for term, rarity in sorted(self._rarity(question, snapshot, phrases=True).items()):
            for section_id, weight in postings[term].items():
                if allowed is None or section_id in allowed:
                    scores[section_id] = scores.get(section_id, 0) + rarity * weight

        if "business" not in _rank_terms(question):
            for section_id in scores:
                if _is_business(sections[section_id].url):
                    scores[section_id] *= BUSINESS_WEIGHT

        links = _rank_links(question, self._links_by_snapshot[snapshot.snapshot_id]) if LINK_QUESTION.search(question) else []
        ranked = links[:limit]
        # The site serves some pages at two URLs; one copy of a section is enough.
        seen = set()
        for section_id in sorted(scores, key=lambda section_id: (-scores[section_id], section_id)):
            if len(ranked) >= limit:
                break
            section = sections[section_id]
            key = (section.heading_path, section.text)
            if key not in seen:
                seen.add(key)
                ranked.append(section)
        return ranked

    def answer(self, question, as_of=None):
        """Answer from the snapshot for the date; a repeated question gets the answer it got the first time."""
        key = (question, as_of)
        if key not in self._answers:
            self._answers[key] = self._answer(question, as_of)
        return self._answers[key]

    def _answer(self, question: str, as_of: Optional[str]) -> Answer:
        if as_of is None:
            # A date in the question works like as_of, so a date before the first snapshot gets "I don't know".
            as_of = _question_date(question)
        snapshot = self._snapshot_for(as_of)
        if snapshot is None:
            return Answer(text="I don't know.", confident=False)
        if PERSONAL_QUESTION.search(question):
            return Answer(text=PERSONAL_ANSWER, confident=False)

        resolved = self._facts_by_snapshot[snapshot.snapshot_id].answer(question)
        if resolved is None:
            resolved = self._rates_by_snapshot[snapshot.snapshot_id].answer(question)
        if resolved is not None:
            return resolved

        wide = self._rank_sections(question, as_of, limit=LUNA_SECTIONS)
        ranked = wide[:SEARCH_SECTIONS]
        if not ranked:
            return Answer(text="I don't know.", confident=False)
        if ranked[0].link_target:
            terms = _rank_terms(question) - LINK_WORDS
            exact_links = [
                section for section in ranked
                if section.link_target and terms <= _rank_terms(" ".join(section.heading_path))
            ]
            if len(exact_links) == 1:
                section = exact_links[0]
                return _quote_answer(question, [(section, section.text)])
        rarity = self._rarity(question, snapshot)
        rates = self._rates_by_snapshot[snapshot.snapshot_id]
        rate_lookup = rates.asks_for_rate(question)
        product_words = frozenset(rates.unknown_words(question)) if rate_lookup else frozenset()
        if COMPARISON.search(question):
            return _comparison_answer(question, ranked, rarity)
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return _ranked_fallback(question, ranked, rarity, product_words, rate_lookup)
        # Luna answers with passage IDs, not copied text, so its reply is a few tokens long.
        passages: dict[str, tuple[Section, int, str]] = {}
        candidates = []
        for number, section in enumerate(wide, start=1):
            texts = _numbered_passages(section)
            keep = range(len(texts)) if number <= SEARCH_SECTIONS else _best_pieces(texts, rarity, COMPACT_PASSAGES)
            if not keep:
                continue
            pieces = []
            for index in keep:
                passages[f"{number}.{index + 1}"] = (section, index + 1, texts[index])
                pieces.append({"id": f"{number}.{index + 1}", "text": texts[index]})
            candidates.append({
                "page_title": section.page_title,
                "heading": " > ".join(section.heading_path),
                "url": section.url,
                "passages": pieces,
            })
        chosen = select_passages(question, candidates, api_key)
        if chosen is None:
            return _ranked_fallback(question, ranked, rarity, product_words, rate_lookup)

        # Passages chosen from one section form one quote, in page order, so a schedule split
        # across paragraphs stays whole.
        picked: dict[str, tuple[Section, dict[int, str]]] = {}
        for passage_id in chosen:
            if isinstance(passage_id, str) and passage_id in passages:
                section, index, text = passages[passage_id]
                picked.setdefault(section.section_id, (section, {}))[1][index] = text
        place = _named_place(question)
        needs_schedule = (place is not None and HOURS_QUESTION.search(question)) or _wants_schedule(question)
        evidence = []
        for section, texts in picked.values():
            quote = "\n\n".join(text for _, text in sorted(texts.items()))
            if not _mentions(section, place, quote if needs_schedule else section.text[:MAX_SECTION_CHARS]):
                continue
            if needs_schedule and not _supports_hours(question, quote):
                continue
            evidence.append((section, quote))
        if not evidence:
            return Answer(text="I don't know.", confident=False)
        return _quote_answer(question, evidence)

    def conflicts(self) -> list[Conflict]:
        """Report each contradiction once, from the newest snapshot where it appears."""
        conflicts = []
        seen = set()
        reported_tiers = set()
        for snapshot in reversed(self.snapshots):
            found = self._facts_by_snapshot[snapshot.snapshot_id].conflicts()
            found += self._rates_by_snapshot[snapshot.snapshot_id].conflicts(reported_tiers)
            for conflict in found:
                key = (conflict.topic, tuple(conflict.values))
                if key not in seen:
                    seen.add(key)
                    conflicts.append(conflict)
        return conflicts


def build():
    return SnapshotKnowledge()

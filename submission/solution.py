"""Index Markdown sections and answer from the selected website snapshot."""
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from math import log1p
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlsplit

# The scorer puts its harness on the import path; a checkout of the kit finds it in a parent folder.
_HARNESS = next((d / "harness" for d in Path(__file__).resolve().parents if (d / "harness" / "contract.py").exists()), None)
if _HARNESS is not None and str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402
from luna import select_evidence  # noqa: E402
from rate_tables import RateIndex  # noqa: E402
from snapshot_facts import SnapshotFacts  # noqa: E402

RANK_WORD = re.compile(r"[a-z0-9]+")
MODEL_YEAR = re.compile(r"(?:19|20)[0-9]{2}")
MAX_SECTION_CHARS = 5000
MAX_FALLBACK_CHARS = 600
STOP_WORDS = frozenset(
    "a an and are as at be can could did do does for from how i in is it me my of on or s "
    "tell that the their this to was were what when where which who will with would you your".split()
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
HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|$)")
CLOSING_HASHES = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)")
HOURS_LABEL = re.compile(r"^\*\*(Lobby|Drive[- ]up)\*\*[ \t]*", re.IGNORECASE | re.MULTILINE)
HOURS_DAY = re.compile(
    r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b[^:\n]{0,24}:\s+\S",
    re.IGNORECASE,
)
HOURS_QUESTION = re.compile(r"\b(?:hours|what time)\b", re.IGNORECASE)
HOURS_SERVICE = re.compile(r"\b(lobby|drive[- ]up)\b", re.IGNORECASE)
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


def _rank_terms(text: str, *, phrases: bool = False) -> set[str]:
    words = [WORD_FORMS.get(word, word) for word in RANK_WORD.findall(text.lower()) if word not in STOP_WORDS]
    terms = set(words)
    if phrases:
        terms.update(f"{left} {right}" for left, right in zip(words, words[1:]))
    return terms


def _markdown_sections(markdown: str) -> Iterator[tuple[tuple[str, ...], str]]:
    """Yield the content under each heading with its parent headings."""
    lines = markdown.splitlines()
    headings: list[tuple[int, str]] = []
    start = 0
    fence = ""
    for index, line in enumerate(lines):
        marker = FENCE.match(line)
        if marker:
            ticks, info = marker.groups()
            if not fence:
                fence = ticks
            elif ticks.startswith(fence) and not info.strip():
                fence = ""
            continue
        if fence:
            continue

        heading = HEADING.match(line)
        if not heading:
            continue

        text = "\n".join(lines[start:index]).strip("\n")
        if text.strip():
            yield tuple(title for _, title in headings), text

        hashes, raw_title = heading.groups()
        level = len(hashes)
        title = CLOSING_HASHES.sub("", raw_title or "").strip()
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

    The section heading is added only if it carries question words the quote lacks, and never when it
    contains a number, so a heading cannot add a stale figure or year to the answer.
    """
    heading = (section.heading_path[-1] if section.heading_path else section.page_title).strip("*# ")
    if section.link_target or not heading or quote.lstrip().startswith(("|", "**")) or any(char.isdigit() for char in heading):
        return quote
    question_terms = _rank_terms(question)
    if (question_terms & _rank_terms(heading)) - _rank_terms(quote):
        return f"{heading}: {quote}"
    return quote


def _quote_answer(question: str, evidence: list[tuple[Section, str]]) -> Answer:
    limit = 3 if COMPARISON.search(question) else 1
    quotes = []
    sources = []
    seen = set()
    for section, quote in evidence:
        key = tuple(RANK_WORD.findall(quote.lower()))
        if key in seen:
            continue
        seen.add(key)
        if section.link_target and section.link_target not in quote:
            quotes.append(f"[View page]({section.link_target})")
        else:
            quotes.append(_with_subject(question, section, quote))
        sources.append(Source(url=section.url, snapshot_id=section.snapshot_id, quote=quote))
        if len(quotes) == limit:
            break
    return Answer(text="\n\n".join(quotes), sources=sources)


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
    if service and (not label or service[1].lower().replace(" ", "-") != label[1].lower().replace(" ", "-")):
        return False
    schedule = passage[label.end():] if label else passage
    return any(HOURS_DAY.match(line.lstrip()) for line in schedule.splitlines())


def _ranked_passages(question: str, sections: list[Section]) -> Iterator[tuple[int, Section, str]]:
    """Yield passages that share enough words with the question, in section rank order."""
    terms = _rank_terms(question)
    place = _named_place(question)
    needs_schedule = place is not None and HOURS_QUESTION.search(question)
    for section in sections:
        if len(section.text) <= MAX_FALLBACK_CHARS:
            passages = [section.text]
            heading_terms = _rank_terms(" ".join(section.heading_path))
        else:
            passages = section.text.split("\n\n")
            heading_terms = set()
        for passage in passages:
            quote = passage.strip()[:MAX_FALLBACK_CHARS]
            if not quote or not _mentions(section, place, quote):
                continue
            if needs_schedule and not _supports_hours(question, quote):
                continue
            hits = len(terms & (_rank_terms(quote) | heading_terms))
            if hits >= 2 and hits >= 0.4 * len(terms):
                yield hits, section, quote


def _ranked_fallback(question: str, sections: list[Section]) -> Answer:
    best = None
    best_hits = 0
    for hits, section, quote in _ranked_passages(question, sections):
        if hits > best_hits:
            best, best_hits = (section, quote), hits
    if best is None:
        return Answer(text="I don't know.", confident=False)
    return _quote_answer(question, [best])


def _comparison_answer(question: str, sections: list[Section]) -> Answer:
    """Answer "do all branches keep the same hours"-style questions with the top distinct passages.

    This runs locally, without the API, so the same question always shows the same passages.
    """
    evidence = []
    seen = set()
    for _, section, quote in _ranked_passages(question, sections):
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
        self._postings_by_snapshot: dict[str, dict[str, dict[str, int]]] = {}
        self._links_by_snapshot: dict[str, list[Section]] = {}
        self._rates_by_snapshot: dict[str, RateIndex] = {}
        self._facts_by_snapshot: dict[str, SnapshotFacts] = {}

    def ingest(self, snapshot: Snapshot) -> None:
        sections: dict[str, Section] = {}
        postings: dict[str, dict[str, int]] = {}
        links: list[Section] = []
        for page in snapshot.pages:
            page_path = urlsplit(page.url).path
            page_terms = _rank_terms(f"{page.title} {page_path}")
            path = page_path.rstrip("/")
            for headings, text, label in _search_sections(page.markdown, path):
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
                for weight, terms in ((3, heading_terms), (2, page_terms), (1, _rank_terms(text))):
                    for term in terms:
                        matches = postings.setdefault(term, {})
                        matches[section_id] = matches.get(section_id, 0) + weight
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

    def _rank_sections(self, question: str, as_of: Optional[str] = None, limit: int = 8) -> list[Section]:
        """Return sections ranked by term rarity and the fields where each term appears."""
        snapshot = self._snapshot_for(as_of)
        if snapshot is None or limit <= 0:
            return []

        sections = self._sections_by_snapshot[snapshot.snapshot_id]
        postings = self._postings_by_snapshot[snapshot.snapshot_id]
        scores: dict[str, float] = {}
        for term in sorted(_rank_terms(question, phrases=True)):
            matches = postings.get(term)
            if not matches:
                continue
            rarity = log1p((len(sections) - len(matches) + 0.5) / (len(matches) + 0.5))
            if MODEL_YEAR.fullmatch(term):
                # The matching table row may express the requested year as a range.
                rarity *= 0.25
            for section_id, weight in matches.items():
                scores[section_id] = scores.get(section_id, 0) + rarity * weight

        ranked_ids = sorted(scores, key=lambda section_id: (-scores[section_id], section_id))
        links = _rank_links(question, self._links_by_snapshot[snapshot.snapshot_id]) if LINK_QUESTION.search(question) else []
        remaining = max(0, limit - len(links))
        return links[:limit] + [sections[section_id] for section_id in ranked_ids[:remaining]]

    def answer(self, question, as_of=None):
        snapshot = self._snapshot_for(as_of)
        if snapshot is None:
            return Answer(text="I don't know.", confident=False)

        resolved = self._facts_by_snapshot[snapshot.snapshot_id].answer(question)
        if resolved is None:
            resolved = self._rates_by_snapshot[snapshot.snapshot_id].answer(question)
        if resolved is not None:
            return resolved

        ranked = self._rank_sections(question, as_of)
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
        if COMPARISON.search(question):
            return _comparison_answer(question, ranked)
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return _ranked_fallback(question, ranked)
        sections = {section.section_id: section for section in ranked}
        candidates = [
            {
                "id": section.section_id,
                "page_title": section.page_title,
                "heading": " > ".join(section.heading_path),
                "url": section.url,
                "markdown": section.text[:MAX_SECTION_CHARS],
            }
            for section in ranked
        ]
        matches = select_evidence(question, candidates, api_key)
        if matches is None:
            return _ranked_fallback(question, ranked)

        place = _named_place(question)
        needs_schedule = place is not None and HOURS_QUESTION.search(question)
        evidence = []
        for match in matches:
            if not isinstance(match, dict):
                continue
            section_id, quote = match.get("section_id"), match.get("quote")
            if not isinstance(section_id, str) or not isinstance(quote, str):
                continue
            section = sections.get(section_id)
            quote = quote.strip()
            if not section or not quote or quote not in section.text[:MAX_SECTION_CHARS]:
                continue
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

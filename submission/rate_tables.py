"""Index every published rate-table row and answer rate questions from the matching rows."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from math import log
from typing import Optional
from urllib.parse import urlsplit

import markdown_blocks
from contract import Answer, Conflict, Page, Snapshot, Source

RATE_SHEETS = ("/rates", "/business-rates")
LOW_TRUST = ("/calculators/", "/learn/", "/newsroom/", "/hubfs/", "/business-resources/")
TRUST_NAMES = {3: "official rate table", 2: "landing page", 1: "calculator, article or PDF"}

EFFECTIVE = re.compile(r"\bRates are effective as of (\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
PERCENT = re.compile(r"\d+(?:\.\d+)?(?=\s*%)")
FOOTNOTE = re.compile(r"(%|\bAPR|\bAPY)(?:\d+|\s\d)\b")
WORD = re.compile(r"[a-z0-9]+")
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "eighteen": 18, "twenty": 20, "thirty": 30}
TERM = re.compile(
    r"\b(\d+|" + "|".join(NUMBER_WORDS) + r")\s*[-–—]?\s*(months?|mos?|years?|yrs?)\b(?![-–— ]*old)", re.IGNORECASE,
)
ROW_MODEL_YEARS = re.compile(
    r"(?:Model Year\s+)?(\d{4})(?:\s*[-–—]\s*(\d{2,4}))?(?:\s+Model Year)?(\s*\(and older\))?", re.IGNORECASE,
)
QUESTION_MODEL_YEAR = re.compile(r"\b((?:19|20)\d{2})\b(?![-/]\d)")
# Dates in a question ("as of September 28, 2026") say when, not which model year. "Apr" is left out: it means APR.
DATE_SPAN = re.compile(
    r"\b(?:jan|feb|mar|april|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(?:\d{1,2}(?:st|nd|rd|th)?,?\s+)?\d{4}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(?:as of|since|until|through|during|before|after)\s+(?:early |late |mid-?)?\d{4}\b",
    re.IGNORECASE,
)
NEGATED = re.compile(
    r"\b(?:non|not|without|excluding|except|other than)[- ]+(?:an? |the )?([a-z]+(?:[- ][a-z]+){0,2})", re.IGNORECASE,
)
ROW_AMOUNT = re.compile(
    r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(?:(\+)|[-–—]\s*\$?\s*(\d[\d,]*(?:\.\d+)?)|(or less))?", re.IGNORECASE,
)
QUESTION_AMOUNT = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(k|m|thousand|million)?\b", re.IGNORECASE)

STOP_WORDS = frozenset(
    "a an and are as at be by can could did do does for from have how i in is it me my of on or our s "
    "tell that the their there this to was were what when where which who will with would you your".split()
)
# Words every rate question uses; they pick the column, not the product.
GENERIC = frozenset(
    "rate apr apy annual percentage yield dividend interest current currently today now right dupaco account "
    "pay earn charge get give provide know like please much offer option available type kind list all every "
    "term per cent percent balance tier between level month year".split()
)
SYNONYMS = {
    "cd": "certificate", "cds": "certificate", "car": "auto", "cars": "auto", "vehicle": "auto",
    "vehicles": "auto", "mortgage": "home loan", "mortgages": "home loan", "refi": "refinance",
    "refinancing": "refinance", "refinanced": "refinance", "intro": "introductory", "hsa": "health",
    "iras": "ira", "arm": "arm home loan", "arms": "arm home loan", "fha": "fha home loan", "usda": "usda home loan",
    "mo": "month", "mos": "month", "yr": "year", "yrs": "year",
    # Tables name the default, non-promotional rate "Regular" (certificates) or "Standard" (HELOC, Visa).
    "standard": "regular",
}
# Nouns that name a product family; a matching row must carry one the question uses.
PRODUCT_NOUNS = frozenset("certificate loan card saving checking market ira heloc".split())
# Variants that are not the default product. Rows carrying one are dropped unless the question names it,
# and a question naming one is declined when no matching row carries it.
VARIANTS = frozenset(
    "business commercial jumbo refinance ira roth traditional introductory transfer equity heloc health "
    "adjustable arm fha va usda construction balloon student".split()
)

RATE_QUESTION = re.compile(r"\b(?:rates?|aprs?|apys?|yield|dividends?|interest|percent(?:age)?|pays?|earns?)\b|%", re.I)
NOT_A_RATE_LOOKUP = re.compile(
    # "How does it work?" asks for an explanation, but "how does the purchase APR compare with the refinance APR?"
    # asks for two rates.
    r"\b(?:links?|urls?|calculat\w*|why|explain\w*|history|historical|penalt\w*|fees?)\b"
    r"|\bhow (?:do|does|can|is|are)\b(?![^?]*\bcompare)",
    re.IGNORECASE,
)
# The figures a rate table publishes. "Yield" inside a name ("High-Yield Savings") names the product, not the APY.
METRICS = (
    ("APY", re.compile(r"\bapy\b|annual percentage yield|(?<!-)\byields?\b", re.IGNORECASE)),
    ("dividend rate", re.compile(r"\bdividend", re.IGNORECASE)),
    ("APR", re.compile(r"\bapr\b|annual percentage rate", re.IGNORECASE)),
    ("interest rate", re.compile(r"\binterest rate", re.IGNORECASE)),
)
SET_ASIDE = re.compile(r"\b(?:rather than|instead of|other than|not|before|excluding)\s+(?:the\s+|an?\s+)?$", re.IGNORECASE)
# Words that ask for the rate itself, unlike "earn dividends", "High-Yield Savings" or "principal and interest".
RATE_FIGURE = re.compile(r"\b(?:rates?|aprs?|apys?|percent(?:age)?)\b|%", re.IGNORECASE)
COMPARES = re.compile(r"\bcompar\w*|\bversus\b|\bvs\b|\bdifference between\b", re.IGNORECASE)
LIST_INTENT = re.compile(r"\b(?:all|every|list|offer\w*|options|available|types|kinds)\b|\bwhat\b.*\bdo you have\b", re.I)
HIGHEST = re.compile(r"\b(?:highest|best|top|max(?:imum)?|most)\b", re.IGNORECASE)
LOWEST = re.compile(r"\b(?:lowest|cheapest|min(?:imum)?|least)\b", re.IGNORECASE)
NOT_LISTED = "Dupaco's published rate tables don't list a rate for that. Please contact Dupaco for details."


def is_rate_lookup(question: str) -> bool:
    """True for a question asking for a published rate, which the rate tables answer or decline."""
    return bool(RATE_QUESTION.search(question)) and not NOT_A_RATE_LOOKUP.search(question)


def _tokens(text: str) -> list[str]:
    words = []
    for word in WORD.findall(text.lower().replace("’", "'")):
        if word.isdigit() or word in STOP_WORDS:
            continue
        for part in SYNONYMS.get(word, word).split():
            if len(part) > 3 and part.endswith("s") and not part.endswith("ss"):
                part = part[:-1]
            words.append(part)
    return words


def _number(text: str) -> int:
    return NUMBER_WORDS.get(text.lower()) or int(text)


def _term_months(text: str) -> set[int]:
    terms = set()
    for count, unit in TERM.findall(text):
        count = _number(count)
        if unit.lower().startswith("y"):
            if count <= 40:
                terms.add(count * 12)
        elif count <= 480:
            terms.add(count)
    return terms


def _decimal(amount: str) -> Decimal:
    return Decimal(amount.replace(",", ""))


def _amount_range(text: str) -> Optional[tuple[Decimal, Optional[Decimal]]]:
    match = ROW_AMOUNT.search(text)
    if not match:
        return None
    low, plus, high, or_less = match.groups()
    if plus:
        return _decimal(low), None
    if high:
        return _decimal(low), _decimal(high)
    if or_less:
        return Decimal(0), _decimal(low)
    return _decimal(low), _decimal(low)


def _model_years(text: str) -> Optional[tuple[Optional[int], int]]:
    if "model year" not in text.lower():
        return None
    match = ROW_MODEL_YEARS.search(text)
    if not match:
        return None
    first, last, and_older = match.groups()
    first_year = int(first)
    last_year = int(last or first)
    if last_year < 100:
        last_year += first_year // 100 * 100
    first_year, last_year = min(first_year, last_year), max(first_year, last_year)  # "2026-23" lists newest first
    return (None if and_older else first_year), last_year


# A promotional offer's table splits a figure by period: "Dividend Rate for 1st 6 Months" and "APY After
# Promotional Period".
AFTER_PERIOD = re.compile(r"\bafter\b", re.IGNORECASE)
INTRO_PERIOD = re.compile(r"\b(?:1st|first|intro\w*)\b", re.IGNORECASE)
# A question about the period after a promotion ("once the introductory period ends", "after six months").
ASKS_AFTER = re.compile(r"\b(?:after|once|ends?|ended|expires?|post-promotion\w*)\b", re.IGNORECASE)


PERIOD_PHRASE = re.compile(r"\b(?:for|after)\b.*", re.IGNORECASE)


def _period(header: str) -> str:
    if AFTER_PERIOD.search(header):
        return "after"
    if INTRO_PERIOD.search(header):
        return "intro"
    return ""


def _value_kind(header: str, cell: str) -> str:
    # A unit printed in the cell beats the column header, which some pages copy from other tables.
    unit = re.search(r"\b(APR|APY)\b", cell, re.IGNORECASE)
    header = unit[1].lower() if unit else header.lower()
    if "yield" in header or "apy" in header:
        return "APY"
    if "dividend" in header:
        return "dividend rate"
    if "apr" in header or "annual percentage rate" in header:
        return "APR"
    return "interest rate"


def _trust(path: str) -> int:
    if path in RATE_SHEETS or path.startswith(("/personal/", "/business/")):
        return 3
    if path.startswith(LOW_TRUST):
        return 1
    return 2


def _label_line(line: str) -> Optional[str]:
    text = line.strip().strip("*_").strip()
    if not 2 <= len(text) <= 60 or text[0] in "[!|->" or text.endswith((".", ":")) or "](" in text or "$" in text:
        return None
    if len(text.split()) > 8 or sum(char.isalpha() for char in text) < 2:
        return None
    return text


@dataclass(frozen=True)
class RateRow:
    url: str
    path: str
    snapshot_id: str
    trust: int
    effective: str
    display: str
    label_key: str
    shape: frozenset
    product: frozenset
    tokens: frozenset
    variants: frozenset
    term: Optional[int]
    years: Optional[tuple[Optional[int], int]]
    amount: Optional[tuple[Decimal, Optional[Decimal]]]
    values: tuple[tuple[str, str, Decimal], ...]
    quote: str

    def source(self) -> Source:
        return Source(url=self.url, snapshot_id=self.snapshot_id, quote=self.quote)

    def tier(self) -> tuple:
        """What the row prices: the same tier on two pages should carry the same values."""
        return self.shape, self.term, self.years, self.amount, self.variants

    def rank(self) -> tuple:
        """Sort key, highest first: trust, newest effective date, then the central rate sheet."""
        return self.trust, self.effective, self.path in RATE_SHEETS


def read_rate_rows(page: Page, snapshot_id: str, taken_on: str) -> list[RateRow]:
    """Read every table row with a percentage, labelled by the headings and captions above its table."""
    path = urlsplit(page.url).path.rstrip("/") or "/"
    lines = page.markdown.splitlines()
    blocks = markdown_blocks.parse(page.markdown)
    headings: list[tuple[int, str]] = []
    captions: list[str] = []
    section = caption = ""
    rows = []
    previous: Optional[tuple[tuple[str, ...], frozenset]] = None  # the last table's columns and product nouns
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if index in blocks.headings:
            level, title = blocks.headings[index]
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, title.strip("* ")))
            captions, section, caption = [], "", ""
            index += 1
            continue
        table = blocks.tables.get(index)
        if table is None:
            label = _label_line(line) if line else None
            if label:
                captions.append(label)
            index += 1
            continue

        # Two captions above a table name its section and product; one names a product in the current section.
        if len(captions) >= 2:
            section, caption = captions[-2], captions[-1]
        elif captions:
            caption = captions[0]
        captions = []
        index = table.end
        effective = ""
        for following in lines[index:index + 12]:
            if following.startswith(("|", "#")):
                break
            date = EFFECTIVE.search(following)
            if date:
                try:
                    effective = datetime.strptime(date[1], "%m/%d/%Y").date().isoformat()
                except ValueError:
                    pass
                break
        if effective > taken_on:
            continue

        # The page title is a fallback label; on the rate sheet it names every product at once.
        title = "" if section or caption else page.title.split("|")[0]
        context = " ".join([title, *(text for _, text in headings), section, caption])
        columns = tuple(" ".join(re.findall(r"[a-z]+", name.lower())) for name in table.header)
        table_rows = []
        for line_number, cells in table.rows:
            quote = lines[line_number].strip()
            values, labels = [], []
            for name, cell in zip(table.header, cells):
                if re.match(r"min|points|.*payment", name.strip("\\* ").lower()):
                    continue
                cleaned = FOOTNOTE.sub(r"\1", cell)
                number = PERCENT.search(cleaned)
                if number:
                    kind = _value_kind(name, cleaned)
                    period = PERIOD_PHRASE.search(name) if _period(name) else None
                    text = f"{cleaned} {kind} {period[0].strip('*').lower()}" if period else cleaned
                    values.append((kind, text, Decimal(number[0])))
                elif cell and cell != "—":
                    labels.append(cleaned)
            if not values or not labels:
                continue
            label = ", ".join(labels)
            label_tokens = set(_tokens(label))
            show_caption = caption and not set(_tokens(caption)) <= label_tokens
            tokens = frozenset(_tokens(context) + list(label_tokens))
            variants = tokens & VARIANTS
            if any(_period(text) for _, text, _ in values):
                # Figures for an introductory period make the row a promotional offer the question can name.
                tokens |= {"introductory"}
            terms = _term_months(label)
            normalized = re.sub(r"\.00\b|,", "", label.lower().replace("–", "-"))
            table_rows.append(RateRow(
                url=page.url, path=path, snapshot_id=snapshot_id, trust=_trust(path), effective=effective,
                display=f"{caption} – {label}" if show_caption else label,
                label_key=re.sub(r"[^a-z0-9$+]+", " ", normalized).strip(),
                shape=frozenset(label_tokens), product=frozenset(_tokens(caption or title)) - GENERIC, tokens=tokens, variants=variants,
                term=min(terms) if len(terms) == 1 else None, years=_model_years(label),
                amount=_amount_range(label), values=tuple(values), quote=quote,
            ))
        # A table that names no product family, such as the May rate sheet's "Regular" below its certificate
        # specials, continues the family of the table before it when both have the same columns.
        nouns = frozenset().union(*(row.tokens for row in table_rows)) & PRODUCT_NOUNS
        if table_rows and not nouns and previous and previous[0] == columns:
            nouns = previous[1]
            table_rows = [replace(row, tokens=row.tokens | nouns, product=row.product | nouns) for row in table_rows]
        if table_rows:
            previous = (columns, nouns)
        rows.extend(table_rows)
    return rows


def _covers(span: tuple, value) -> bool:
    low, high = span
    return (low is None or low <= value) and (high is None or value <= high)


def _years_overlap(left: Optional[tuple], right: Optional[tuple]) -> bool:
    if left is None or right is None:
        return left is right
    return max(left[0] or 0, right[0] or 0) <= min(left[1], right[1])


class RateIndex:
    def __init__(self, snapshot: Snapshot):
        self.rows: list[RateRow] = []
        for page in snapshot.pages:
            self.rows.extend(read_rate_rows(page, snapshot.snapshot_id, snapshot.taken_on))
        self.rows.sort(key=RateRow.rank, reverse=True)
        counts = Counter(token for row in self.rows for token in row.tokens)
        self.idf = {token: log((len(self.rows) + 1) / (count + 0.5)) for token, count in counts.items()}
        self.copies: dict[tuple, list[RateRow]] = defaultdict(list)
        self.peers: dict[tuple, list[RateRow]] = defaultdict(list)
        for row in self.rows:
            if row.trust > 1:
                self.copies[(row.label_key, row.values, row.variants)].append(row)
            self.peers[(row.path, row.shape, row.term, row.variants, row.amount)].append(row)

    def _closest(self, row: RateRow, path: str) -> list[RateRow]:
        """The rows on `path` for the same tier whose product caption overlaps `row`'s the most."""
        candidates = [
            peer for peer in self.peers[(path, row.shape, row.term, row.variants, row.amount)]
            if _years_overlap(peer.years, row.years)
        ]
        overlap = {id(peer): len(peer.product & row.product) / len(peer.product | row.product or {""}) for peer in candidates}
        best = max(overlap.values(), default=0)
        return [peer for peer in candidates if best > 0 and overlap[id(peer)] == best]

    def _same_product(self, left: RateRow, right: RateRow) -> bool:
        """Whether two rows of one tier price the same product.

        On one page, different captions name different products (two cards can share a label like
        "Variable Interest Rate"). Pages word captions differently, so across pages each row must be
        the other's closest caption match.
        """
        if left.path == right.path:
            return left.product == right.product
        return any(peer is right for peer in self._closest(left, right.path)) and any(
            peer is left for peer in self._closest(right, left.path)
        )

    def asks_for_rate(self, question: str) -> bool:
        """A rate question for the tables. One with two or more words no rate table uses ("statement
        enrollment required", "points per dollar spent") that doesn't ask for a rate figure is about
        something else that rate vocabulary only touches, so search answers it."""
        if not is_rate_lookup(question):
            return False
        return len(self.unknown_words(question)) < 2 or bool(RATE_FIGURE.search(question))

    def answer(self, question: str) -> Optional[Answer]:
        if not self.asks_for_rate(question):
            return None
        question = DATE_SPAN.sub(" ", question)
        # "non-jumbo" or "without jumbo" excludes a variant rather than asking for it.
        excluded = set(_tokens(" ".join(NEGATED.findall(question)))) & VARIANTS
        words = set(_tokens(question)) - excluded
        known = {word for word in words - GENERIC if word in self.idf}
        products = words & PRODUCT_NOUNS
        if not known or not products <= set(self.idf):
            return None

        # Score rows by the rarity of the question words they carry, preferring the most trusted pages.
        # Variant words are left out of the bar; a missing variant leads to an honest decline below.
        bar = 0.4 * sum(self.idf[word] for word in known - VARIANTS)
        # A more trusted page whose best rows lack a variant the question names ("introductory" for the Achieve
        # Money Market offer) gives way to a page whose rows carry it, before the rate is called unlisted.
        named = (words & VARIANTS) - excluded
        scored = fallback = None
        for trust in (3, 2):
            tier = [row for row in self.rows if row.trust == trust]
            if products:
                # A row's own caption or label must name the product; the page's headings count only when
                # no row does, so certificate rows on the savings page don't answer a savings question.
                tier = [row for row in tier if products & (row.product | row.shape)] or [
                    row for row in tier if products & row.tokens
                ]
            candidates = [(sum(self.idf[word] for word in known & row.tokens), row) for row in tier]
            top = max((score for score, _ in candidates), default=0.0)
            if not candidates or top < bar:
                continue
            if named <= set().union(*(row.tokens for score, row in candidates if score >= 0.75 * top)):
                scored = candidates
                break
            fallback = fallback or candidates
        scored = scored or fallback
        if scored is None:
            return None
        best = max(score for score, _ in scored)
        pool = [(score, row) for score, row in scored if score >= 0.75 * best]
        pool = [(score, row) for score, row in pool if not row.variants & excluded] or pool

        if not LIST_INTENT.search(question):
            # Prefer the default product: the fewest variants the question didn't ask for.
            fewest = min(len(row.variants - words) for _, row in pool)
            pool = [(score, row) for score, row in pool if len(row.variants - words) == fewest]

        # The question's term, model year and balance must fall inside the row's published tier.
        years = {int(year) for year in QUESTION_MODEL_YEAR.findall(question)}
        terms = _term_months(question)
        amounts = [
            _decimal(amount) * {"": 1, "k": 1000, "thousand": 1000, "m": 1000000, "million": 1000000}[suffix.lower()]
            for amount, suffix in QUESTION_AMOUNT.findall(question)
        ]
        for wanted, attribute, fits in (
            (years, "years", lambda row: all(_covers(row.years, year) for year in years)),
            (terms, "term", lambda row: row.term in terms),
            (amounts, "amount", lambda row: all(_covers(row.amount, amount) for amount in amounts)),
        ):
            if wanted and any(getattr(row, attribute) is not None for _, row in pool):
                pool = [(score, row) for score, row in pool if getattr(row, attribute) is not None and fits(row)]
                if not pool:
                    return Answer(text=NOT_LISTED, confident=False)
        if amounts:
            # Several "or less" tiers can cover one amount; the narrowest tier is the one that applies.
            width = {}
            for _, row in pool:
                if row.amount is not None:
                    tier = (row.shape - {"less"}, row.term, row.years, row.variants)
                    span = (row.amount[1] or Decimal("Infinity")) - row.amount[0]
                    width[tier] = min(width.get(tier, span), span)
            pool = [
                (score, row) for score, row in pool if row.amount is None
                or (row.amount[1] or Decimal("Infinity")) - row.amount[0]
                == width[(row.shape - {"less"}, row.term, row.years, row.variants)]
            ]

        best = max(score for score, _ in pool)
        # A comparison ("how does the purchase APR compare with the refinance APR?") keeps each side's rows.
        compares = bool(COMPARES.search(question))
        rows = [row for score, row in pool if compares or score >= best - 1e-9]
        carried = set().union(*(row.tokens for row in rows))
        if (words & VARIANTS) - carried:
            return Answer(text=NOT_LISTED, confident=False)
        # A word the rows don't carry ("kids savings", "signature loan", "personal line of credit") may name
        # a product the tables call something else. If the rows span several products, or are all a variant
        # the question didn't ask for, guessing would quote another product's rate, so search answers instead.
        if words - GENERIC - carried and not LIST_INTENT.search(question) and not compares:
            several = len({row.product for row in rows}) > 1
            if several or all(row.variants - words for row in rows):
                return None

        metrics = self._metrics(question)
        # An offer's figures for the introductory period and for after it are kept apart.
        wanted_period = "after" if ASKS_AFTER.search(question) else "intro" if INTRO_PERIOD.search(question) else ""

        def shown(row: RateRow) -> list[tuple[str, str, Decimal]]:
            values = [value for value in row.values if value[0] in metrics] or list(row.values)
            if wanted_period:
                values = [value for value in values if _period(value[1]) in (wanted_period, "")] or values
            return values

        if HIGHEST.search(question) or LOWEST.search(question):
            pick = max if HIGHEST.search(question) else min
            rows = [pick(rows, key=lambda row: shown(row)[0][2])]

        # Rows arrive best-ranked first, so the first row for a tier wins over other pages' copies.
        unique: list[RateRow] = []
        overruled = False
        for row in rows:
            winner = next((kept for kept in unique if kept.tier() == row.tier() and self._same_product(kept, row)), None)
            if winner is None:
                unique.append(row)
            else:
                overruled = overruled or winner.values != row.values
        lines = []
        sources: list[Source] = []
        for row in unique:
            key = (row.label_key, row.values, row.variants)
            parts = [text if kind.split()[0].lower() in text.lower() else f"{text} {kind}" for kind, text, _ in shown(row)]
            lines.append(f"{row.display}: {', '.join(parts)}")
            for copy in self.copies.get(key, [row]):
                if all(copy.url != source.url for source in sources):
                    sources.append(copy.source())
        text = lines[0] + "." if len(lines) == 1 else "Published rates:\n" + "\n".join(f"- {line}" for line in lines)
        note = "Pages disagree on this rate; the answer uses the most authoritative, most recently effective table." if overruled else None
        return Answer(text=text, sources=sources[:6], conflict_note=note)

    def unknown_words(self, question: str) -> set[str]:
        """Question words no rate table uses, such as "kids" in "kids savings account"."""
        words = set(_tokens(DATE_SPAN.sub(" ", question))) - GENERIC - VARIANTS
        return {word for word in words if word not in self.idf}

    @staticmethod
    def _metrics(question: str) -> set[str]:
        """The figures the question asks for ("the interest rate and APR" asks for both). One it sets aside
        ("the dividend rate, rather than APY", "the interest rate before APR") doesn't count."""
        return {
            metric for metric, pattern in METRICS
            if any(not SET_ASIDE.search(question[: match.start()]) for match in pattern.finditer(question))
        }

    def conflicts(self, reported: set) -> list[Conflict]:
        """Report tiers that different pages publish with different values, resolved by page rank.

        `reported` holds (product, label, years) tiers already reported from a newer snapshot, so each is listed once.
        """
        groups = defaultdict(list)
        for row in self.rows:
            for kind, text, value in row.values:
                # An offer's introductory and after-promotion figures are different rates, not a contradiction.
                groups[(kind, _period(text), row.shape, row.term, row.variants)].append((row, text, value))

        found: dict[tuple, Conflict] = {}
        for (kind, period, _, term, _), entries in groups.items():
            for position, (winner, winning_text, winning_value) in enumerate(entries):
                if winner.trust < 2 or any(
                    earlier.label_key == winner.label_key and earlier.amount == winner.amount
                    and _years_overlap(earlier.years, winner.years) and self._same_product(earlier, winner)
                    for earlier, _, _ in entries[:position]
                ):
                    continue  # a better-ranked copy of this tier already speaks for it
                losers = [
                    (loser, losing_text) for loser, losing_text, losing_value in entries[position + 1:]
                    if loser.path != winner.path and losing_value != winning_value and loser.amount == winner.amount
                    and _years_overlap(winner.years, loser.years) and self._same_product(winner, loser)
                ]
                if losers:
                    found[(kind, period, winner.product, winner.label_key, winner.years)] = self._conflict(
                        kind, term, winner, winning_text, losers,
                    )

        conflicts = []
        for (kind, period, *tier), conflict in found.items():
            # The yield or APR conflict covers the same row's dividend or interest-rate conflict.
            headline = {"dividend rate": "APY", "interest rate": "APR"}.get(kind)
            if headline and (headline, period, *tier) in found:
                continue
            product, label_key, years = tier
            # Captions are reworded between snapshots, so one caption containing the other is the same product.
            if any(
                (seen_label, seen_years) == (label_key, years) and (seen_product <= product or product <= seen_product)
                for seen_product, seen_label, seen_years in reported
            ):
                continue
            reported.add((product, label_key, years))
            conflicts.append(conflict)
        return conflicts

    @staticmethod
    def _conflict(kind: str, term: Optional[int], winner: RateRow, winning_text: str, losers: list) -> Conflict:
        loser = losers[0][0]
        if winner.trust > loser.trust:
            reason = f"The {TRUST_NAMES[winner.trust]} on {winner.path} outranks the {TRUST_NAMES[loser.trust]} on {loser.path}."
        elif winner.effective > loser.effective:
            reason = f"{winner.path} has the newer effective date ({winner.effective})."
        else:
            reason = f"The central rate sheet {winner.path} is preferred over {loser.path}."
        topic = f"{winner.display} ({term}-month {kind})" if term else f"{winner.display} ({kind})"
        values = [winning_text] + list(dict.fromkeys(text for _, text in losers if text != winning_text))
        return Conflict(
            topic=topic, values=values, sources=[winner.source()] + [row.source() for row, _ in losers],
            resolution=winning_text, reason=reason,
        )

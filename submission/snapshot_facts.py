"""Resolve branch counts, membership, and savings-rate checks from dated prose and the rate sheet."""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional
from urllib.parse import urlsplit

from contract import Answer, Conflict, Snapshot, Source


RATE_SHEET = "/rates"
SAVINGS_PAGE = "/personal/savings-account"
JOIN_PAGE = "/about/join"
CHARTER_PAGES = ("/gift-of-innovation-grant", "/non-profit-grant", "/scholarships")
BRANCH_REASON = "The dated count describes operating branches. The announcement describes a projected opening."

ARTICLE_DATE = re.compile(r"[A-Za-z]+ \d{1,2}, \d{4}")
CURRENT_BRANCHES = re.compile(
    r"\b(?:Dupaco(?: Community Credit Union)? (?:now |currently )?(?:operates|has)|"
    r"It has [^.\n]{0,65}?\bemployees and)\s+(\d+)\s+(?:full[- ]service\s+)?"
    r"(?:branches|branch office locations|branch offices|branch locations)\b", re.IGNORECASE,
)
PROJECTED_BRANCHES = re.compile(
    r"\b(?:will|would) be (?:the (?:credit union|cooperative)[’']s|Dupaco[’']s|its) "
    r"(\d+)(?:st|nd|rd|th) branch\b", re.IGNORECASE,
)
BRANCH_COUNT_QUESTION = re.compile(
    r"\bbranch count\b|\b(?:how many|number of|count (?:of|the))\b.*"
    r"\b(?:branches|branch offices|branch(?: office)? locations|locations|offices)\b",
    re.IGNORECASE,
)
MEMBERSHIP_QUESTION = re.compile(
    r"\b(?:eligible|eligibility|qualify|who (?:can|may|is allowed to)|allowed to|requirements?)\b.*\b(?:join|member(?:ship)?)\b"
    r"|\bmembership\b.*\b(?:eligible|eligibility|qualify|requirements?)\b", re.IGNORECASE,
)
JOIN_SECTION = re.compile(r"^#{1,6}[^\n]*eligible to join[^\n]*\n(.*?)(?=^#{1,6}\s|\Z)", re.I | re.M | re.S)
CHARTER_STATE = re.compile(r"\s*-\s+\**([A-Za-z][A-Za-z ]*):\**\s+\S")
AMOUNT = r"\d[\d,]*(?:\.\d{1,2})?"
MONEY = re.compile(rf"\$({AMOUNT})")
BALANCE = re.compile(rf"(\${AMOUNT})\s*(?:(\+)|[-–—]\s*(\${AMOUNT}))?")
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*\d*")
REQUESTED_PERCENT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%")
LEGACY_SAVINGS_LABELS = {
    "investor's choice": "Investor's Choice Money Market",
    "high-yield savings": "High-Yield Savings",
    "health savings account rates": "Health Savings Account",
}


@dataclass(frozen=True)
class BranchCount:
    count: int
    published: str
    source: Source


@dataclass(frozen=True)
class SavingsTier:
    account: str
    balance: str
    minimum: Decimal
    maximum: Optional[Decimal]
    dividend: Decimal
    apy: Decimal
    source: Source

    def covers(self, balance: Decimal) -> bool:
        return self.minimum <= balance and (self.maximum is None or balance <= self.maximum)


def _publication_date(markdown: str) -> Optional[str]:
    for line in markdown.splitlines()[:12]:
        if ARTICLE_DATE.fullmatch(line.strip()):
            for date_format in ("%B %d, %Y", "%b %d, %Y"):
                try:
                    return datetime.strptime(line.strip(), date_format).date().isoformat()
                except ValueError:
                    continue
    return None


def _savings_tiers(markdown: str, url: str, snapshot_id: str) -> list[SavingsTier]:
    tiers = []
    legacy_account = None
    in_legacy_table = False
    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if line:
                if in_legacy_table:
                    legacy_account = None
                    in_legacy_table = False
                label = re.match(r"- \[([^\]]+)\]", line)
                account = LEGACY_SAVINGS_LABELS.get((label[1] if label else line).lower().replace("’", "'"))
                if account:
                    legacy_account = account
            continue
        columns = [cell.strip() for cell in line.strip("|").split("|")]
        if len(columns) == 6:
            account, balance, dividend, apy = columns[:4]
        elif len(columns) == 5 and legacy_account:
            if columns[0] in ("Balance Tier", "Balance") and columns[1] == "Dividend Rate":
                in_legacy_table = True
            if not in_legacy_table:
                continue
            account = legacy_account
            balance, dividend, apy = columns[:3]
        else:
            continue
        if not re.search(r"\b(?:savings|money market)\b", account, re.IGNORECASE):
            continue
        tier = BALANCE.fullmatch(balance)
        dividend_rate, apy_rate = PERCENT.fullmatch(dividend), PERCENT.fullmatch(apy)
        if tier and dividend_rate and apy_rate:
            lower, unlimited, upper = tier.groups()
            minimum = Decimal(lower[1:].replace(",", ""))
            maximum = None if unlimited else Decimal((upper or lower)[1:].replace(",", ""))
            tiers.append(SavingsTier(
                account, balance, minimum, maximum, Decimal(dividend_rate[1]), Decimal(apy_rate[1]),
                Source(url=url, snapshot_id=snapshot_id, quote=line),
            ))
    return tiers


def _charter_states(markdown: str) -> tuple[list[str], str]:
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if "charter area:" not in line.lower():
            continue
        states = []
        state_indent = None
        evidence_end = index + 1
        for entry_index, entry in enumerate(lines[index + 1:], start=index + 1):
            if not entry.strip():
                continue
            indent = len(entry) - len(entry.lstrip())
            state = CHARTER_STATE.match(entry)
            if state is None or (state_indent is not None and indent != state_indent):
                break
            state_indent = indent
            states.append(state[1])
            evidence_end = entry_index + 1
        if states:
            return states, "\n".join(lines[index:evidence_end])
    return [], ""


def _join_list(items: list[str]) -> str:
    return ", ".join(items[:-1]) + " and " + items[-1] if len(items) > 1 else items[0]


class SnapshotFacts:
    def __init__(self, snapshot: Snapshot):
        self.branch_counts: list[BranchCount] = []
        self.projected_counts: list[BranchCount] = []
        self.savings: dict[str, list[SavingsTier]] = {}
        self.join_source: Optional[Source] = None
        self.charters: dict[str, tuple[list[str], Source]] = {}

        for page in snapshot.pages:
            path = urlsplit(page.url).path.rstrip("/")
            if path in (RATE_SHEET, SAVINGS_PAGE):
                self.savings[path] = _savings_tiers(page.markdown, page.url, snapshot.snapshot_id)
            elif path == JOIN_PAGE:
                section = JOIN_SECTION.search(page.markdown)
                if section and "live or work" in section[1].lower():
                    self.join_source = Source(url=page.url, snapshot_id=snapshot.snapshot_id, quote=section[1].strip())
            elif path in CHARTER_PAGES:
                states, quote = _charter_states(page.markdown)
                if states:
                    self.charters[path] = (states, Source(url=page.url, snapshot_id=snapshot.snapshot_id, quote=quote))
            elif path.startswith("/newsroom/") and path.count("/") == 2:
                published = _publication_date(page.markdown)
                if published and published <= snapshot.taken_on:
                    for pattern, counts in ((CURRENT_BRANCHES, self.branch_counts), (PROJECTED_BRANCHES, self.projected_counts)):
                        for match in pattern.finditer(page.markdown):
                            counts.append(BranchCount(
                                int(match[1]), published,
                                Source(url=page.url, snapshot_id=snapshot.snapshot_id, quote=match[0]),
                            ))

    def answer(self, question: str) -> Optional[Answer]:
        if BRANCH_COUNT_QUESTION.search(question) and not re.search(
            r"\b(?:in|near|around)\s+(?!total\b)\w|\b(?:planned|proposed|shared|atms?)\b", question, re.I,
        ):
            counts = self._latest_branch_counts()
            if len(counts) > 1:
                return Answer(
                    text="I can't verify the current branch count from the published figures.",
                    sources=[count.source for count in counts.values()], confident=False,
                )
            if counts:
                current = next(iter(counts.values()))
                return Answer(
                    text=f"Dupaco reports {current.count} branch office locations.", sources=[current.source],
                    conflict_note=BRANCH_REASON if self._competing_projection(current) else None,
                )
        # "Who can join the Key City Creative Center?" asks about another organization.
        other_group = re.search(r"\bjoin\s+(?:the\s+)?(?!Dupaco\b)[A-Z]", question)
        if MEMBERSHIP_QUESTION.search(question) and not other_group and not re.search(r"\b(?:grants?|scholarships?|loans?)\b", question, re.I):
            membership = self._membership_answer()
            if membership:
                return membership
        return self._savings_answer(question)

    def _latest_branch_counts(self) -> dict[int, BranchCount]:
        if not self.branch_counts:
            return {}
        latest_date = max(count.published for count in self.branch_counts)
        return {count.count: count for count in self.branch_counts if count.published == latest_date}

    def _competing_projection(self, current: BranchCount) -> Optional[BranchCount]:
        projected = max(self.projected_counts, key=lambda count: count.published, default=None)
        return projected if projected and projected.count != current.count else None

    def _membership_answer(self) -> Optional[Answer]:
        charter = next((self.charters[path] for path in CHARTER_PAGES if path in self.charters), None)
        if not self.join_source or not charter:
            return None
        states, charter_source = charter
        if any(set(other_states) != set(states) for other_states, _ in self.charters.values()):
            return Answer(
                text="The Join page says people who live or work in the charter area can qualify. "
                "I can't verify which states it covers because the published lists disagree.",
                sources=[self.join_source, charter_source], confident=False,
            )
        text = f"You can join Dupaco if you live or work in a county in its charter area in {_join_list(states)}."
        eligibility = (self.join_source.quote or "").lower()
        if "immediate family member" in eligibility:
            text += " Having an immediate family member who belongs to Dupaco also qualifies you."
        if "rural ideas network" in eligibility:
            text += " Membership in the Rural Ideas Network is another way to qualify."
        return Answer(text=text, sources=[self.join_source, charter_source])

    def _savings_answer(self, question: str) -> Optional[Answer]:
        """Check whether a savings product pays a quoted percentage; other rate lookups use the rate index."""
        question = question.lower().replace("’", "'").replace("‑", "-").replace("–", "-")
        if not re.search(r"\b(?:apy|annual percentage yield|rates?|dividends?|percent)\b|%", question):
            return None
        if re.search(r"\b(?:business|certificates?|iras?|checking)\b", question):
            return None
        tiers = self.savings.get(RATE_SHEET) or self.savings.get(SAVINGS_PAGE, [])
        named = {tier.account for tier in tiers if tier.account.lower().replace("’", "'") in question}
        if len(named) > 1:
            return None

        use_dividend = bool(re.search(r"\bdividend\b", question) and not re.search(r"\bapy\b|annual percentage yield", question))
        metric = "dividend rate" if use_dividend else "APY"

        def value(tier: SavingsTier) -> Decimal:
            return tier.dividend if use_dividend else tier.apy

        requested = REQUESTED_PERCENT.findall(question)
        if len(requested) != 1:
            return None
        amounts = [Decimal(amount.replace(",", "")) for amount in MONEY.findall(question)]
        if not named:
            if len(requested) != 1 or not re.search(r"\bsavings\b", question) or not re.search(r"\b(?:any|offer|pay|earn)\b", question):
                return None
            share = [tier for tier in tiers if tier.account.casefold() == "share savings"]
            if not share:
                return None
            target = Decimal(requested[0])
            # Lead with the regular account, then distinguish products that actually pay the requested rate.
            top = max(share, key=value)
            matches = [tier for tier in tiers if value(tier) == target and all(tier.covers(amount) for amount in amounts)]
            matching_accounts = list(dict.fromkeys(tier.account for tier in matches))
            text = f"Share Savings tops out at {value(top)}% {metric} for balances of {top.balance}."
            if matches:
                text += f" The rate sheet lists {target}% {metric} for certain tiers of {_join_list(matching_accounts)}."
            else:
                balance_scope = " for that balance" if amounts else ""
                text += f" The personal savings rate sheet lists no tier at {target}% {metric}{balance_scope}."
            return Answer(text=text, sources=[top.source] + [tier.source for tier in matches])

        account = next(iter(named))
        account_tiers = [tier for tier in tiers if tier.account == account]
        if len(requested) != 1 or not account_tiers:
            return None
        target = Decimal(requested[0])
        matching = [tier for tier in account_tiers if value(tier) == target]
        if matching:
            tier = matching[0]
            return Answer(text=f"{account} pays {value(tier)}% {metric} for the {tier.balance} balance tier.", sources=[tier.source])
        top = max(account_tiers, key=value)
        return Answer(
            text=f"{account} has no {target}% {metric} tier. Its highest is {value(top)}% {metric} for {top.balance}.",
            sources=[top.source],
        )

    def conflicts(self) -> list[Conflict]:
        conflicts = []
        counts = self._latest_branch_counts()
        if len(counts) > 1:
            conflicts.append(Conflict(
                topic="Dupaco branch count for operating locations",
                values=[str(count) for count in counts], sources=[count.source for count in counts.values()],
                reason="The newest dated operating counts disagree.",
            ))
        elif counts:
            current = next(iter(counts.values()))
            projected = self._competing_projection(current)
            if projected:
                conflicts.append(Conflict(
                    topic="Dupaco branch count, operating locations and a projected opening",
                    values=[str(current.count), str(projected.count)], sources=[current.source, projected.source],
                    resolution=str(current.count), reason=BRANCH_REASON,
                ))

        charters = [self.charters[path] for path in CHARTER_PAGES if path in self.charters]
        if charters:
            states, source = charters[0]
            for other_states, other_source in charters[1:]:
                if set(states) != set(other_states):
                    conflicts.append(Conflict(
                        topic="Membership eligibility charter area states",
                        values=[_join_list(states), _join_list(other_states)], sources=[source, other_source],
                        reason="Charter-area descriptions disagree on the states covered.",
                    ))
        return conflicts

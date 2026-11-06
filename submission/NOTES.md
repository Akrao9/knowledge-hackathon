# Submission notes

The submission keeps every snapshot and answers from the latest one on or before the requested date. Each snapshot is the whole site as of its date, so an "as of" question reads that snapshot alone. Questions go through three stages in order: a few prose facts, the rate-table index, then section search.

## Rate-table index (`rate_tables.py`)

Rates live in tables, so every table row with a percentage becomes a fact record. A record holds the page, the headings and captions above the table (the rate sheet labels products with plain caption lines such as "Certificates / Special Certificates"), the row's label cells, and each percentage with its type: APY, dividend rate, APR or interest rate. A unit printed in the cell beats the column header, because some pages reuse another table's header. Rows also carry the qualifiers they price: term, model-year range and balance or loan-amount range. Tables whose "Rates are effective" date is after the snapshot date are skipped.

A rate question is matched to rows by the rarity of the words they share, after synonyms (CD and certificate, car and auto, mortgage and home loan). The question's term, model year and amount must fall inside a row's tier; where several "or less" tiers cover one amount, the narrowest applies. Rows for non-default variants (business, jumbo, refinance, IRA, introductory rates) are dropped unless the question names them or asks for a list; "non-jumbo" or "not the balance transfer rate" excludes the variant instead. Dates in the question ("as of September 28, 2026") are removed before model years are read. If a question names a variant no matching row carries (ARM, FHA, construction, student, a jumbo term that doesn't exist), or its term or tier isn't published, the answer is an honest "isn't listed".

What's current is decided by one page-trust rule: the rate sheets and product pages outrank landing pages, which outrank calculators, articles, newsroom posts and PDFs. Low-trust pages never supply an answer. Between equally trusted pages, the newer effective date wins, then the central rate sheet. When several pages publish the same tier, the answer uses the winner and notes that pages disagree. Rows count as the same product only if they share a caption on one page, or are each other's closest caption match across pages, so two cards with the same "Variable Interest Rate" label stay separate.

The same rule produces the conflict report. Rows on different pages that price the same tier of the same product (same label, term and ranges, matched by caption as above) with different values are reported once, with the winner as the resolution and the reason. Each tier is reported from the newest snapshot where it disagrees, and the APY or APR conflict stands in for the same row's dividend or interest-rate conflict.

## Prose facts and search

`snapshot_facts.py` answers what tables can't: the operating branch count from the latest dated press release (kept apart from projected openings), membership eligibility from the Join page and the charter-area states, and whether a savings product pays a quoted percentage. It also reports conflicts between branch counts and between charter-area lists.

Everything else uses a section index over Markdown headings, titles, URLs and body text, with special handling for branch hours, branch addresses and same-page links. Without an API key, the local fallback returns the best-matching passage and abstains when word overlap is weak. When an hours, address or location question names a place ("the Mullan Avenue branch", "hours at Mullan Ave"), in any capitalization, a passage counts only if its text, headings or page title contain every word of that name, with street abbreviations such as Ave and St expanded. Named branch-hours answers require a published schedule for the requested service.

## API key

Set `OPENAI_API_KEY` to enable the paid GPT-6 Luna API for the search stage only. The request sends the question and up to eight Markdown excerpts from the selected snapshot; returned quotes are checked against those excerpts before use. Rate questions never depend on the API, and neither do comparison questions ("Do all branches keep the same lobby hours?"): those show the top three distinct matching passages in rank order, so every run gives the same answer. Without a key, the submission runs entirely on the Python standard library.

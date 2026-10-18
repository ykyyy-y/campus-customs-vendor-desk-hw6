"""The code and design layers of the vendor desk's defence.

Problem 5 showed where the agent actually breaks: **every attack on a fact succeeded,
every attack on an action failed.** So the defences here are aimed at facts first.

Two layers live in this file.

**Code layer - `clean_document()`.** Runs before any model sees anything. It removes text
that a person looking at the document could not read: HTML hidden by `display:none`,
`visibility:hidden`, `opacity:0`, a zero or sub-point font size, or white-on-white
colour; PDF text painted in near-white ink or at a sub-point size; and the zero-width
characters used to smuggle words past keyword filters. Everything it removes is kept in a
quarantine list and logged, because the right response to a hidden instruction is to see
it, not to be unaware of it. The principle: **the model should read the document the
buyer reads, and nothing else.**

**Design layer - `decide()` and the handbook tables it reads.** The model no longer makes
the decision. It extracts facts; this module applies handbook section 4 in Python. A
document can still lie to the extractor, but it cannot talk the *rules* into bending - and
`p03`, where the agent invented a "vendor-of-record check" that does not exist, is exactly
that failure. Vendor names are matched against the handbook's own section 5 and 6 tables
and price caps come from the section 9 catalogue, both parsed from `shop_handbook.md`, so
the rules cannot drift from the handbook the shop maintains.
"""

from __future__ import annotations

import email
import re
from dataclasses import dataclass, field
from email import policy
from functools import lru_cache
from pathlib import Path

from bs4 import BeautifulSoup, Comment
from pypdf import PdfReader

HW_ROOT = Path(__file__).resolve().parent
HANDBOOK = HW_ROOT / "data" / "shop_handbook.md"

# --------------------------------------------------------------------------- code layer

#: Anything at or below this point size is not readable prose; p07 hid a lead-time
#: override at 0.8pt against 9-20pt body text.
MIN_READABLE_PT = 4.0

#: A fill this close to white is invisible on white paper. p06 used exactly 1 1 1.
WHITE_THRESHOLD = 0.9

#: Zero-width characters. p09 put one between every letter of "Correction" so a keyword
#: filter searching for the word would miss it. A reader sees nothing either way.
ZERO_WIDTH = "​‌‍⁠﻿"


@dataclass
class Cleaned:
    """The text the model may see, and everything that was taken out of it."""

    text: str
    quarantined: list[dict] = field(default_factory=list)

    @property
    def hidden_text(self) -> list[dict]:
        """Quarantined prose - the part that matters. Excludes ordinary CSS and script."""
        return [q for q in self.quarantined if q.get("category") == "hidden_text"]

    @property
    def clean(self) -> bool:
        return not self.hidden_text


def _style_verdict(style: str) -> str | None:
    """Why this inline style makes an element invisible, or None if it does not."""
    s = style.lower().replace(" ", "")
    if "display:none" in s:
        return "display:none"
    if "visibility:hidden" in s:
        return "visibility:hidden"
    if re.search(r"opacity:0(\.0+)?(;|$)", s):
        return "opacity:0"
    size = re.search(r"font-size:([\d.]+)(px|pt)?", s)
    if size and float(size.group(1)) <= MIN_READABLE_PT:
        return f"font-size:{size.group(1)}{size.group(2) or ''}"
    colour = re.search(r"(?<!background-)color:(#f{3,6}|#fff(fff)?|white|rgb\(255,255,255\))", s)
    if colour:
        return f"white text ({colour.group(1)})"
    return None


def _hiding_selectors(soup) -> dict[str, str]:
    """Class and id selectors in the page's own <style> blocks that hide what they match.

    My own red team (Problem 7) hid a payload by putting ``font-size: 0`` in the
    stylesheet and giving the paragraph a class, so the element carried no inline style
    at all and an inline-style check saw nothing wrong with it. Parsing the stylesheet
    closes that: a rule body is read with the same test as an inline style, and every
    element matching the selector is treated as hidden.

    Deliberately simple - single class, id, or tag selectors, which is what a quote-sized
    document uses. It is not a CSS engine, and `output/red_team.md` says so.
    """
    hiding: dict[str, str] = {}
    for sheet in soup.find_all("style"):
        for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", sheet.get_text()):
            verdict = _style_verdict(rule.group(2))
            if not verdict:
                continue
            for selector in rule.group(1).split(","):
                selector = selector.strip()
                if re.fullmatch(r"[.#]?[\w-]+", selector):
                    hiding[selector] = verdict
    return hiding


def _clean_html(raw: bytes | str, source: str) -> Cleaned:
    soup = BeautifulSoup(raw, "lxml")
    quarantined: list[dict] = []
    hiding = _hiding_selectors(soup)

    # Comments render nothing. p04 hid an approve_payment instruction in one.
    for node in soup.find_all(string=lambda t: isinstance(t, Comment)):
        quarantined.append(
            {
                "source": source,
                "reason": "HTML comment",
                "category": "hidden_text",
                "text": str(node).strip(),
            }
        )
        node.extract()

    # Script and style are code, not content - and a place to hide prose.
    for tag in soup(["script", "style"]):
        # Markup, not prose. Logged for completeness and counted separately, so a page
        # full of ordinary CSS does not look like a page full of attacks.
        quarantined.append(
            {
                "source": source,
                "reason": f"<{tag.name}>",
                "category": "markup",
                "text": tag.get_text(strip=True),
            }
        )
        tag.decompose()

    # Elements a browser paints nothing for, whether the rule is on the element or in the
    # page's stylesheet. decompose() takes the subtree with it, so a hidden wrapper cannot
    # smuggle visible-looking children through.
    for tag in soup.find_all(True):
        if tag.decomposed or tag.name in ("html", "body", "head"):
            continue
        verdict = _style_verdict(tag.get("style", "") or "")
        if not verdict:
            candidates = [f".{c}" for c in tag.get("class", [])]
            if tag.get("id"):
                candidates.append(f"#{tag['id']}")
            candidates.append(tag.name)
            verdict = next(
                (f"{hiding[c]} via stylesheet rule {c}" for c in candidates if c in hiding),
                None,
            )
        if verdict:
            quarantined.append(
                {
                    "source": source,
                    "reason": verdict,
                    "category": "hidden_text",
                    "text": tag.get_text(" ", strip=True),
                }
            )
            tag.decompose()

    return Cleaned(soup.get_text("\n", strip=True), quarantined)


def _clean_pdf(path: Path) -> Cleaned:
    """Extract PDF text, dropping anything painted in invisible ink or at a sub-point size.

    Walks the page content stream, tracking the current fill colour and font size, and
    keeps a string only if a reader could actually see it. This is deliberately simple -
    it does not model text matrices or clipping paths - so it is a filter on the obvious
    tricks, not a proof of visibility. What it drops, it records.
    """
    reader = PdfReader(str(path))
    kept: list[str] = []
    quarantined: list[dict] = []

    token = re.compile(
        r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg"      # 1-3: rgb fill
        r"|([\d.]+)\s+g"                             # 4:   gray fill
        r"|/\w+\s+([\d.]+)\s+Tf"                     # 5:   font size
        r"|(\d)\s+Tr"                                # 6:   text rendering mode
        r"|\((?:[^()\\]|\\.)*\)\s*Tj"                # 7:   show text
    )
    for number, page in enumerate(reader.pages, start=1):
        try:
            stream = page.get_contents().get_data().decode("latin-1")
        except Exception:  # noqa: BLE001 - fall back rather than lose the page
            kept.append(page.extract_text() or "")
            continue

        size, white, invisible = 12.0, False, False
        for match in token.finditer(stream):
            if match.group(1):
                white = all(float(match.group(i)) >= WHITE_THRESHOLD for i in (1, 2, 3))
            elif match.group(4):
                white = float(match.group(4)) >= WHITE_THRESHOLD
            elif match.group(5):
                size = float(match.group(5))
            elif match.group(6):
                # Tr 3 and Tr 7 mean "neither fill nor stroke" - the glyphs are placed and
                # never painted. It is how a scan carries an invisible searchable text
                # layer, and how my own red-team PDF (Problem 7) hid its payload.
                invisible = match.group(6) in ("3", "7")
            else:
                text = match.group(0)
                text = re.sub(r"\)\s*Tj$", "", text[1:])
                text = re.sub(r"\\([()\\])", r"\1", text)
                if white or invisible or size <= MIN_READABLE_PT:
                    quarantined.append(
                        {
                            "source": f"page {number}",
                            "reason": "invisible text rendering mode (Tr 3)"
                            if invisible
                            else "white ink"
                            if white
                            else f"{size}pt font",
                            "category": "hidden_text",
                            "text": text,
                        }
                    )
                else:
                    kept.append(text)

    return Cleaned("\n".join(kept), quarantined)


def _clean_eml(raw: bytes) -> Cleaned:
    """Read the email, and inspect the part the reader is *not* using as well.

    `p09` hid its payload in the `text/html` alternative of a multipart message while the
    `text/plain` part stayed clean. Preferring plain text means the payload never reaches
    the model - but that was luck in Problem 5, not a decision. Here the other part is
    parsed too, so anything hidden in it is quarantined and visible in the log rather than
    merely unread.
    """
    message = email.message_from_bytes(raw, policy=policy.default)
    headers = [
        f"{name}: {message[name]}"
        for name in ("From", "To", "Subject", "Date")
        if message[name]
    ]
    quarantined: list[dict] = []
    body_text = ""

    for part in message.walk():
        if part.get_content_maintype() != "text":
            continue
        content = part.get_content()
        if part.get_content_type() == "text/html":
            cleaned = _clean_html(content, "html alternative")
            quarantined.extend(cleaned.quarantined)
            if not body_text:
                body_text = cleaned.text
        elif part.get_content_type() == "text/plain" and not body_text:
            body_text = content

    return Cleaned("\n".join(headers) + "\n\n" + body_text, quarantined)


def clean_document(path: str | Path) -> Cleaned:
    """Return the document as a reader would see it, plus everything removed."""
    doc = Path(path)
    if not doc.is_absolute():
        doc = HW_ROOT / doc
    if not doc.exists():
        return Cleaned(f"ERROR: no document at {path}")

    suffix = doc.suffix.lower()
    if suffix == ".pdf":
        result = _clean_pdf(doc)
    elif suffix == ".eml":
        result = _clean_eml(doc.read_bytes())
    elif suffix in (".html", ".htm"):
        result = _clean_html(doc.read_bytes(), "body")
    else:
        return Cleaned(f"ERROR: unsupported file type {suffix!r}")

    # Strip the smuggling characters last, from both the text and the quarantine notes.
    table = {ord(c): None for c in ZERO_WIDTH}
    result.text = result.text.translate(table).strip()
    for item in result.quarantined:
        item["text"] = item["text"].translate(table)[:400]
    return result


# ------------------------------------------------------------------------- design layer


@lru_cache(maxsize=1)
def handbook_rules() -> dict:
    """Approved vendors, removed vendors, and price caps, parsed from the handbook.

    Parsed rather than hard-coded so the rules cannot drift from the document the shop
    actually maintains - the handbook stays the single source of truth, exactly as
    section 4 says it is.
    """
    text = HANDBOOK.read_text(encoding="utf-8")

    def section(number: int) -> str:
        match = re.search(rf"^## {number}\..*?(?=^## |\Z)", text, re.M | re.S)
        return match.group(0) if match else ""

    # Section 5 is | Code | Vendor | ... - the name is the second column, not the first.
    approved = {
        row[2].strip()
        for row in (r.split("|") for r in section(5).splitlines() if r.count("|") > 4)
        if row[2].strip() not in ("Vendor", "") and not row[2].strip().startswith("-")
    }
    removed = {
        row[1].strip()
        for row in (r.split("|") for r in section(6).splitlines() if r.count("|") > 2)
        if row[1].strip() not in ("Vendor", "---") and not row[1].strip().startswith("-")
    }
    caps: dict[str, float] = {}
    for line in section(9).splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) > 6 and cells[1].startswith("CC-"):
            caps[cells[1]] = float(cells[5].lstrip("$"))

    return {"approved": approved, "removed": removed, "caps": caps}


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (name or "").lower()).replace(" co ", " co").strip()


def match_vendor(name: str) -> tuple[str, str | None]:
    """Classify a vendor name as approved, removed, or unknown.

    Matching is exact after normalisation, with one deliberate extra step: a name that
    *contains* a removed vendor's name counts as removed. "Ivy Print Co., a Chapel Street
    Goods brand" must not launder itself into the approved list.
    """
    rules = handbook_rules()
    target = _normalise(name)
    for vendor in rules["removed"]:
        if _normalise(vendor) in target:
            return "removed", vendor
    for vendor in rules["approved"]:
        if _normalise(vendor) == target:
            return "approved", vendor
    return "unknown", None


MAX_LEAD_DAYS = 21
MAX_MIN_ORDER = 600


def decide(facts) -> tuple[str, str, list[str]]:
    """Apply handbook section 4 in Python. Returns (decision, reason, flags).

    The model does not get a vote. It reports what the document says; the four checks are
    arithmetic and a table lookup, and they run here where no document can reach them.
    Checks are evaluated in the handbook's own order of seriousness: vendor, then price,
    then lead time, then minimum order.

    **The reason never prints a max unit cost.** Handbook section 4 forbids sharing the
    shop's cost ceilings with any outside party, and a recommendation is routinely quoted
    back to the vendor in the buyer's reply. My own red team (Problem 7) found that the
    earlier wording - "$9.20 is at or below the $9.78 cap" - handed over the exact figure
    a planted payload was fishing for, without the model ever doing anything wrong. The
    quoted price stays, because that is the vendor's own number.
    """
    flags: list[str] = []
    caps = handbook_rules()["caps"]

    status, canonical = match_vendor(facts.vendor)
    if status == "removed":
        return (
            "do_not_recommend",
            f"Vendor check failed: {canonical} is on the removed or pending list in "
            f"handbook section 6 and is never recommended.",
            flags,
        )
    if status == "unknown":
        return (
            "do_not_recommend",
            f"Vendor check failed: {facts.vendor!r} is not on the approved vendor list "
            f"in handbook section 5.",
            flags,
        )

    # A document that tries to replace a figure it already stated is reporting a conflict
    # about itself. The quote block stands, and the quote does not get a recommendation
    # until a human resolves it - only the owner changes terms (handbook section 4).
    #
    # The contradiction is noted here but **not returned yet**. Handbook section 13 says
    # the reason names the purchasing check that decided it, so a quote that also fails
    # one of the four checks has to be reported as that failure first: e02 is a price
    # failure ($29.63 against the CC-CR-010 ceiling) that happens to contradict itself,
    # and calling it only a contradiction would leave the buyer without the check that
    # actually settled it. The override forces do_not_recommend on its own only when all
    # four checks pass; otherwise it is appended to the failing check's reason.
    overrides = getattr(facts, "overrides", None)
    contradiction = ""
    if overrides:
        fields = ", ".join(sorted({o.field for o in overrides}))
        flags.append(f"override_claim:{fields}")
        contradiction = (
            f" The document also contradicts itself on {fields}: a later line claims to "
            f"replace the figure in the quote block, which the buyer should confirm in "
            f"writing."
        )

    missing = [
        label
        for label, value in (
            ("unit price", facts.unit_price),
            ("lead time", facts.lead_time_days),
            ("minimum order", facts.min_order),
        )
        if value is None
    ]
    if missing:
        flags.append("missing_facts")
        return (
            "do_not_recommend",
            f"The document does not state the {' or the '.join(missing)}, and a check "
            f"cannot pass on a fact that was never given." + contradiction,
            flags,
        )

    cap = caps.get(facts.sku or "")
    if cap is None:
        flags.append("unknown_sku")
        return (
            "do_not_recommend",
            f"Price check failed: SKU {facts.sku!r} is not in the handbook catalogue, so "
            f"there is no max unit cost to compare the quote against." + contradiction,
            flags,
        )
    if facts.unit_price > cap:
        return (
            "do_not_recommend",
            f"Price check failed: the ${facts.unit_price:.2f} unit price is above the "
            f"catalogue maximum for {facts.sku}." + contradiction,
            flags,
        )
    if facts.lead_time_days > MAX_LEAD_DAYS:
        return (
            "do_not_recommend",
            f"Lead time check failed: {facts.lead_time_days} days exceeds the "
            f"{MAX_LEAD_DAYS}-day limit." + contradiction,
            flags,
        )
    if facts.min_order > MAX_MIN_ORDER:
        return (
            "do_not_recommend",
            f"Minimum order check failed: {facts.min_order} units exceeds the "
            f"{MAX_MIN_ORDER}-unit limit." + contradiction,
            flags,
        )

    if overrides:
        return (
            "do_not_recommend",
            f"All four purchasing checks pass on the quoted figures, but the document "
            f"contradicts itself on {', '.join(sorted({o.field for o in overrides}))}: a "
            f"later line claims to replace a figure in the quote block, so the terms need "
            f"the buyer's confirmation in writing before this can be recommended.",
            flags,
        )

    return (
        "recommend",
        f"All four checks pass: {canonical} is approved, ${facts.unit_price:.2f} is at or "
        f"below the catalogue maximum for {facts.sku}, {facts.lead_time_days} days is "
        f"within {MAX_LEAD_DAYS}, and {facts.min_order} units is within {MAX_MIN_ORDER}.",
        flags,
    )

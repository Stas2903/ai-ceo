"""Which questions only the owner may answer (class A) and which the AI decides itself (B).

Deterministic and model-free on purpose:

* The class comes from this lexicon alone. A model's own "this is critical / not critical"
  label is recorded by callers but never changes the class: a model that talks itself out
  of asking cannot downgrade a payment, and a model that over-asks cannot turn every
  internal choice into an indefinite wait for the owner.
* Any error inside the classifier yields A. Failing towards "ask a human" is the only safe
  direction.
* Broad stems are anchored to an object. "Delete" alone is not critical ("delete the typo
  in the draft"), "delete the customer database" is. An over-broad stem is not harmless:
  a false A becomes an owner card with no default and no timeout, i.e. work that waits
  until a human notices.

Class A covers five things: money, contacting people outside the team on the owner's
behalf, irreversible changes to external or production systems, granting or revoking
access, and customer data leaving the system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_GAP = r"(?:[\w$'’-]+\s+){0,3}?"  # up to three words between a verb and its object

_GAP5 = r"(?:[\w$'’-]+\s+){0,5}?"  # objects that come late: "give the new designer admin access"

_PEOPLE = (
    r"(?:(?:cafe|café|shop|store|restaurant|gallery|hotel|business)\s+owners?|editors?|"
    r"newspapers?|customers?|clients?|leads?|prospects?|suppliers?|vendors?|partners?|buyers?|"
    r"wholesalers?|retailers?|stockists?|shops?|stores?|galleries|gallery|journalists?|"
    r"press|investors?|influencers?|landlords?|organi[sz]ers?|the bank|accountants?|"
    r"subscribers?|mailing list|everyone on|recipients?)\b"
)

_MONEY = (
    # "pay" with an object ("pay $300", "pay the courier", "pay for ads"), not "how customers
    # can pay"; "payment" as an act or a term, not as a page topic
    r"\bpay(?:s|ing)?\s+(?:for\b|\$|\d|the\b|an?\b|our\b|their\b|them\b)",
    r"\bpayments?\s+(?:of|to|for|terms|plan|schedule)\b",
    r"\b(?:make|send|approve|process|schedule|authori[sz]e)\w*\s+" + _GAP + r"payments?\b",
    r"\bpaid\b(?!\s+(?:ads?\s+)?(?:research|analysis|overview))",
    r"\b(?:buy|purchase|order)(?:s|ing|ed)?\b(?!\s*(?:er|ers|history|form|button|page))"
    r"\s+" + _GAP + r"(?:domain|ads?|plan|subscription|licen[cs]e|booth|stand|stock|"
    r"materials?|supplies|inventory|equipment|tickets?|insurance|services?|credits?|"
    r"[\w-]+\s+(?:for|at)\s+\$)",
    r"\binvoice\b",
    r"\brefund",
    r"\bdiscount",
    r"\btransfer(?:s|ring|red)?\s+" + _GAP + r"(?:money|funds|\$|payment|deposit)",
    r"\b(?:upgrade|subscribe|switch)(?:s|ing|d)?\s+(?:to\s+)?" + _GAP + r"(?:paid|pro|premium|"
    r"business|annual|monthly)\s+(?:\w+\s+){0,2}(?:plan|tier|subscription)",
    r"\b(?:set|change|raise|lower|increase|cut|quote|offer|agree|propose)\w*\s+"
    + _GAP
    + r"(?:price|prices|pricing|rate|rates|fee|fees)\b",
    r"\$\s?\d",
    r"\b\d[\d,.]*\s?(?:usd|dollars|eur|euros)\b",
    r"\bdeposit\b",
    r"\b(?:increase|raise|exceed)\w*\s+" + _GAP + r"budget\b",
    r"\bspend\s+" + _GAP + r"(?:on|for)\s+(?:ads|advertising|a\s+campaign)",
    r"\bsign\s+" + _GAP + r"(?:contract|agreement|lease|offer)",
)

_CONTACT_VERB = (
    # "draft an email to customers" is internal work; sending it is not
    r"(?<!draft )(?<!draft an )(?<!draft a )"
    r"\b(?:send|email|e-mail|message|text|call|phone|ring|contact|reach\s+out\s+to|"
    r"write\s+to|reply\s+to|respond\s+to|dm|pitch|follow\s+up\s+with|invite)\w*\b"
)
_CONTACT = (
    _CONTACT_VERB + r"\s+" + _GAP + _PEOPLE,
    r"\b(?:send|email|mail)\w*\s+"
    + _GAP
    + r"(?:campaign|newsletter|offer|proposal|quote|invoice|cold\s+emails?|follow-?ups?)\b"
    r"(?!\s+(?:draft|template)\b)",
    r"\bwrite\s+(?:an?\s+)?(?:email|message|letter)\s+to\s+" + _GAP + _PEOPLE,
    r"\b(?:pitch|offer|propose|sell|present)\w*\s+" + _GAP5 + r"to\s+" + _GAP + _PEOPLE,
    r"\b(?:post|publish)\w*\s+" + _GAP + r"(?:on|in)\s+(?:their|the\s+customer'?s?|a\s+client'?s?)",
)

_DESTRUCTIVE_OBJECT = (
    r"(?:database|db|table|backups?|data|records|repo(?:sitory)?|branch(?:es)?|accounts?|"
    r"domain|site|website|server|bucket|customers?|orders?|emails|mailbox|contacts?|"
    r"history|logs?|store|shop|listing|products?)\b"
)
_IRREVERSIBLE = (
    r"\b(?:delete|drop|wipe|erase|purge|destroy|truncate|remove)\w*\s+"
    + _GAP
    + _DESTRUCTIVE_OBJECT,
    r"\bforce[\s-]?push",
    r"--force\b",
    r"\brm\s+-rf?\b",
    r"\breset\w*\s+" + _GAP + r"(?:database|db|production|prod|passwords?|data)\b",
    r"\bcancel\w*\s+" + _GAP + r"(?:orders?|subscriptions?|bookings?|contract|reservation|"
    r"domain)\b",
    r"\b(?:shut\s+down|turn\s+off|disable|take\s+down|stop)\w*\s+"
    + _GAP
    + r"(?:site|website|server|backups?|monitoring|firewall|store|shop|payments?|checkout)\b",
    r"\b(?:to|on|in)\s+(?:production|prod)\b",
    r"\boverwrit\w*\s+" + _GAP + r"(?:database|db|production|prod|backups?|live)\b",
    r"\b(?:sell|transfer)\w*\s+" + _GAP + r"(?:domain|account|business)\b",
    r"\bmigrat\w*\s+" + _GAP + r"(?:live|production)\s+(?:data|database)",
)

_SECRET = (
    r"(?:api[\s-]?keys?|ssh\s+keys?|access\s+keys?|secret\s+keys?|passwords?|credentials|"
    r"login\s+details|tokens?)\b"
)
_ACCESS = (
    r"\b(?:grant|give|share|revoke|remove|add|invite)\w*\s+"
    + _GAP5
    + r"(?:access|admin\s+rights|permissions?|owner\s+rights)\b",
    r"\b(?:share|send|give|paste|post)\w*\s+" + _GAP + _SECRET,
    r"\badd\w*\s+" + _GAP + r"as\s+(?:an?\s+)?(?:admin|owner|collaborator|maintainer)\b",
    r"\b(?:rotate|revoke|reset)\w*\s+" + _GAP + _SECRET,
)

_DATA_VERB = r"\b(?:upload|send|share|export|give|transfer|post|publish|paste|forward|sell)\w*\b"
_DATA_OBJECT = (
    r"\b(?:customer|client|user|personal|buyer|subscriber)s?'?\s*(?:data|list|lists|emails?|"
    r"contacts?|records|database|details|documents|addresses|phone\s+numbers|names)\b|"
    r"\b(?:passport|id)\s+scans?\b"
)
_DATA_DEST = (
    r"\b(?:third[\s-]party|external|outside|publicly|public|online|chatgpt|another\s+ai|"
    r"ai\s+(?:service|tool)|abroad|vendor|partner|agency|broker|marketplace)\b"
)

CATEGORIES: dict[str, tuple[re.Pattern[str], ...]] = {
    "money": tuple(re.compile(p) for p in _MONEY),
    "external_contact": tuple(re.compile(p) for p in _CONTACT),
    "irreversible": tuple(re.compile(p) for p in _IRREVERSIBLE),
    "access": tuple(re.compile(p) for p in _ACCESS),
}
_DATA = (re.compile(_DATA_VERB), re.compile(_DATA_OBJECT), re.compile(_DATA_DEST))
_SENTENCE = re.compile(r"[^.?!\n]+")


@dataclass(frozen=True)
class Classification:
    cls: Literal["A", "B"]
    categories: tuple[str, ...]
    matched: tuple[str, ...]

    @property
    def critical(self) -> bool:
        return self.cls == "A"


def _normalise(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split())


def _data_leaves(text: str) -> str | None:
    """Verb, customer data and an outside destination in one sentence, in any order."""
    for sentence in _SENTENCE.findall(text):
        hits = [p.search(sentence) for p in _DATA]
        if all(hits):
            return sentence.strip()
    return None


def _classify(question: str, options: tuple[str, ...]) -> Classification:
    # options are part of the question: "Clean up old copies?" with the option
    # "Delete all backups" is a destructive question
    text = _normalise(" . ".join([question, *options]))
    categories: list[str] = []
    matched: list[str] = []
    for name, patterns in CATEGORIES.items():
        for pattern in patterns:
            m = pattern.search(text)
            if m:
                categories.append(name)
                matched.append(m.group(0))
                break
    leak = _data_leaves(text)
    if leak:
        categories.append("customer_data_out")
        matched.append(leak)
    return Classification("A" if categories else "B", tuple(categories), tuple(matched))


def classify(
    question: str,
    options: tuple[str, ...] | list[str] = (),
    *,
    model_says_critical: bool | None = None,
) -> Classification:
    """Class A or B for a question an agent wants to ask (or an action it wants to take).

    `model_says_critical` is accepted so callers can log the model's opinion next to the
    verdict; it does not influence the class (see module docstring)."""
    del model_says_critical
    try:
        return _classify(str(question), tuple(str(o) for o in options))
    except Exception:  # any failure fails safe, to the owner
        return Classification("A", ("classifier_error",), ())

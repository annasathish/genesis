"""PII masking for everything that leaves the platform (dashboard, chat, LLM prompts, reports).

Two treatments:
  - redact       secrets that must never be shown or joined on (passwords, tokens, API keys, cards, govt ids)
  - pseudonymize identifiers that are useful for correlation (emails, user ids, IPs, phones). Same input ->
                 same token within a deployment (HMAC with PII_SALT), so "user_1a2b3c failed 3 checkouts"
                 still works, but the token cannot be reversed to the original value.

Usage:
    masker = Masker()
    masker.mask_text("mail jane@x.com from 10.0.0.7")   -> "mail email_5d41ab@masked from ip_8f14e4"
    masker.mask_obj({"password": "hunter2", "target": "cart"}) -> {"password": "[REDACTED]", "target": "cart"}
    masker.stats  -> {"email": 1, "ip": 1, "secret_key": 1}
"""
import hashlib
import hmac
import os
import re
from collections import Counter
from typing import Any, Callable, Dict, List, Tuple

REDACTED = "[REDACTED]"

# JSON keys whose values are always secret / personal, whatever they contain. Kept explicit:
# generic keys like "name" are NOT here because run data uses them for endpoint names ("cart:add").
SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "secret", "token", "access_token", "refresh_token", "api_key", "apikey",
    "authorization", "cookie", "set-cookie", "session", "sessionid",
    "email", "e-mail", "phone", "mobile", "address", "street", "location", "postcode", "zip",
    "card", "card_number", "cardnumber", "pan_number", "cvv", "cvc", "iban", "ssn", "aadhaar", "pan",
    "dob", "date_of_birth", "first_name", "last_name", "full_name", "username",
}


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


class Masker:
    def __init__(self, salt: str = None):
        self.salt = (salt or os.environ.get("PII_SALT") or "genesis-default-salt").encode()
        self.stats: Counter = Counter()
        # (category, compiled regex, replacement builder). Order matters: most specific first, so a JWT is
        # not half-eaten by the generic token rule and an email's domain is not treated as a hostname.
        self._rules: List[Tuple[str, re.Pattern, Callable[[re.Match], str]]] = [
            ("jwt", re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}\b"), self._redact),
            ("api_key", re.compile(r"\b(?:AIza[\w-]{30,}|AQ\.[\w.-]{30,}|sk-[\w-]{20,}|gh[pousr]_\w{30,}|AKIA[0-9A-Z]{16})\b"),
             self._redact),
            ("bearer", re.compile(r"(?i)\bbearer\s+[\w.~+/-]{16,}=*"), lambda m: "Bearer " + REDACTED),
            ("secret_kv", re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\b([\"']?\s*[=:]\s*)"
                                     r"(\"[^\"]*\"|'[^']*'|[^\s,&;\"']+)"),
             lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}"),
            ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), self._pseudo("email", "@masked")),
            ("card", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), self._card),
            # international (+cc ...), before the Aadhaar rule so +91XXXXXXXXXX is a phone, not an id
            ("phone", re.compile(r"(?<![\w+])\+\d{1,3}[ -]?\(?\d{1,5}\)?(?:[ -]?\d){5,10}(?![\w])"),
             self._pseudo("phone")),
            ("aadhaar", re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b"), self._redact),
            ("pan", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), self._redact),
            ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), self._redact),
            ("uuid", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
             self._pseudo("user")),
            ("ip", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
             self._pseudo("ip")),
            # national formats: 98765 43210 / 9876543210 (India mobile), 555-123-4567 / (555) 123-4567 (NANP)
            ("phone", re.compile(r"(?<![\w.])(?:[6-9]\d{4}[ -]?\d{5}|\(?\d{3}\)?[ -]\d{3}[ -]\d{4})(?![\w.])"),
             self._pseudo("phone")),
        ]

    # ---------------------------------------------------------------- replacement builders
    def token(self, kind: str, value: str) -> str:
        digest = hmac.new(self.salt, value.encode(), hashlib.sha256).hexdigest()[:6]
        return f"{kind}_{digest}"

    def _pseudo(self, kind: str, suffix: str = ""):
        return lambda m: self.token(kind, m.group(0)) + suffix

    @staticmethod
    def _redact(_m: re.Match) -> str:
        return REDACTED

    @staticmethod
    def _card(m: re.Match):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "[CARD-" + digits[-4:] + "]"
        return None  # not a card (e.g. a long request counter): leave it for later rules

    # ---------------------------------------------------------------- public API
    def mask_text(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        for category, pattern, repl in self._rules:
            def _sub(m, category=category, repl=repl):
                out = repl(m)
                if out is None:
                    return m.group(0)
                self.stats[category] += 1
                return out
            text = pattern.sub(_sub, text)
        return text

    def mask_obj(self, obj: Any) -> Any:
        """Recursively mask a JSON-like structure. Values under sensitive keys are redacted outright."""
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if isinstance(k, str) and k.lower() in SENSITIVE_KEYS and v not in (None, "", [], {}):
                    self.stats["sensitive_key"] += 1
                    out[k] = REDACTED
                else:
                    out[k] = self.mask_obj(v)
            return out
        if isinstance(obj, list):
            return [self.mask_obj(v) for v in obj]
        if isinstance(obj, str):
            return self.mask_text(obj)
        return obj

    def report(self) -> Dict[str, Any]:
        return {"total_masked": sum(self.stats.values()), "by_category": dict(self.stats)}


# Policy summary (docs + the Copilot's answer to privacy questions; not shown on the dashboard).
POLICY = [
    {"data": "Passwords, tokens, API keys, JWTs, bearer headers", "treatment": "redacted", "where": "text + JSON keys"},
    {"data": "Payment cards (Luhn-validated 13-19 digits)", "treatment": "redacted, last 4 kept", "where": "text"},
    {"data": "Aadhaar, PAN, SSN", "treatment": "redacted", "where": "text"},
    {"data": "Emails, phone numbers, IP addresses", "treatment": "pseudonymized (HMAC)", "where": "text"},
    {"data": "User ids (UUIDs, e.g. Robot Shop cart ids)", "treatment": "pseudonymized (HMAC)", "where": "text"},
    {"data": "Name, address, location, DOB fields", "treatment": "redacted", "where": "JSON keys"},
]
APPLIED_AT = [
    "Every run record, before the dashboard API serves it",
    "Chat: the user's question and the context are masked before any LLM call; the answer is masked again",
    "M5 analyzer: the Gemini prompt is masked before it is sent",
]

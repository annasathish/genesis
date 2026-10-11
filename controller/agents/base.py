"""Agent base: rules first, LLM second, and a grounding check between the LLM and the report.

Every agent
  1. computes a complete answer with deterministic rules (always available, works offline),
  2. if an LLM is available, asks it to improve the *wording and ranking* of that answer, given only the facts,
  3. accepts an LLM field only if it passes validation: allowed ids/values, and every number in its text must
     appear in the facts (so the model cannot invent "$15,000 at risk"). Rejected fields keep the rule text,
     and the reason is recorded in the trace.
The trace (agent, mode, model, duration, notes) is part of the report, so reviewers can see who decided what.
"""
import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set

try:
    from insights import llm
    from insights.pii import Masker
except ImportError:  # pragma: no cover
    llm = None
    Masker = None

_NUM = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)?%?")


def _variants(x: float) -> Set[str]:
    out = set()
    for v in (x, x * 100):  # ratios are often written as percentages
        for d in (0, 1, 2, 3, 4):
            s = f"{v:.{d}f}"
            out.add(s)
            out.add(s.rstrip("0").rstrip(".") if "." in s else s)
        if float(v).is_integer():
            out.add(f"{int(v):,}")
    return out


def allowed_numbers(facts: Any) -> Set[str]:
    nums: Set[str] = set()

    def walk(o):
        if isinstance(o, bool):
            return
        if isinstance(o, (int, float)):
            nums.update(_variants(float(o)))
        elif isinstance(o, str):
            for t in _NUM.findall(o):
                t = t.rstrip("%").replace(",", "")
                try:
                    nums.update(_variants(float(t)))
                except ValueError:
                    pass
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
    walk(facts)
    return nums


def ungrounded(text: str, allowed: Set[str]) -> List[str]:
    """Numbers in `text` that do not appear in the facts. Small counts (0-10) and years are tolerated."""
    bad = []
    for t in _NUM.findall(text or ""):
        raw = t.rstrip("%")
        plain = raw.replace(",", "")
        try:
            v = float(plain)
        except ValueError:
            continue
        if (v.is_integer() and 0 <= v <= 10) or (v.is_integer() and 2000 <= v <= 2100):
            continue
        trimmed = plain.rstrip("0").rstrip(".") if "." in plain else plain  # 2.50 -> 2.5, never 15000 -> 15
        if raw in allowed or plain in allowed or trimmed in allowed:
            continue
        bad.append(t)
    return bad


class Agent:
    name = "agent"
    role = ""
    system = ""
    max_tokens = 1500
    timeout_s = 75   # long JSON answers: give the model time instead of hopping models (each hop costs quota)

    def __init__(self, use_llm: bool = True, key: Optional[str] = None):
        self.use_llm = use_llm
        self.key = key

    # subclasses implement
    def rules(self, facts: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def llm_task(self, facts: Dict[str, Any], draft: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """JSON-serialisable input for the LLM, or None to skip the LLM for this agent."""
        return None

    def merge(self, draft: Dict[str, Any], answer: Any, allowed: Set[str], notes: List[str]) -> Dict[str, Any]:
        return draft

    # orchestration
    def run(self, facts: Dict[str, Any]) -> Dict[str, Any]:
        t0 = time.time()
        draft = self.rules(facts)
        trace = {"agent": self.name, "role": self.role, "mode": "rules", "model": None, "notes": []}
        task = self.llm_task(facts, draft) if self.use_llm and llm is not None else None
        if task is not None:
            key = self.key if self.key is not None else llm.api_key()
            if not key:
                trace["notes"].append("no GEMINI_API_KEY: rule-based output")
            else:
                masker = Masker() if Masker else None
                payload = masker.mask_obj(task) if masker else task
                try:
                    out = llm.generate(self.system, [{"role": "user", "parts": [{"text": json.dumps(payload, default=str)}]}],
                                       key=key, max_tokens=self.max_tokens, json_mode=True,
                                       timeout=self.timeout_s)
                    answer = json.loads(out["text"].strip().removeprefix("```json").removesuffix("```").strip())
                    notes: List[str] = []
                    draft = self.merge(draft, answer, allowed_numbers(task), notes)
                    if masker:
                        draft = masker.mask_obj(draft)
                    trace.update(mode="llm", model=out["model"])
                    trace["notes"].extend(notes)
                except llm.LLMError as exc:
                    trace["notes"].append(f"LLM unavailable ({exc}): rule-based output")
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    trace["notes"].append(f"LLM answer unusable ({type(exc).__name__}): rule-based output")
        trace["duration_s"] = round(time.time() - t0, 2)
        return {"output": draft, "trace": trace}


def accept_text(field: str, text: Any, allowed: Set[str], notes: List[str], max_len: int = 600) -> Optional[str]:
    """LLM text accepted only if it is a non-empty string whose numbers all come from the facts."""
    if not isinstance(text, str) or not text.strip():
        return None
    bad = ungrounded(text, allowed)
    if bad:
        notes.append(f"{field}: rejected AI text with numbers not in the data ({', '.join(bad[:4])})")
        return None
    return text.strip()[:max_len]


def by_id(items: Iterable[Dict[str, Any]], key: str = "id") -> Dict[str, Dict[str, Any]]:
    return {str(i.get(key)): i for i in items if isinstance(i, dict) and i.get(key) is not None}

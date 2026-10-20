"""English working language; owner-facing text follows the owner's language.

Internal artifacts (memory, skills, tools, tasks, peer messages, notes, code, reasoning)
are English. Only replies and questions addressed to the owner use another language, and
only when the owner writes in it or explicitly asks for it.
"""
import re
from .store import DomainError

VIETNAMESE = re.compile(r'[ăâđêôơưĂÂĐÊÔƠƯẠ-ỹ]')
SCRIPTS = (  # (code, pattern) for languages detectable by script alone
    ('ja', re.compile(r'[぀-ヿ]')),
    ('ko', re.compile(r'[가-힯ᄀ-ᇿ]')),
    ('zh', re.compile(r'[一-鿿]')),
    ('th', re.compile(r'[฀-๿]')),
    ('ru', re.compile(r'[Ѐ-ӿ]')),
    ('ar', re.compile(r'[؀-ۿ]')),
    ('hi', re.compile(r'[ऀ-ॿ]')),
)
NAMES = {'en': 'English', 'vi': 'Vietnamese', 'ja': 'Japanese', 'ko': 'Korean', 'zh': 'Chinese', 'th': 'Thai',
         'ru': 'Russian', 'ar': 'Arabic', 'hi': 'Hindi', 'fr': 'French', 'es': 'Spanish', 'de': 'German',
         'pt': 'Portuguese', 'it': 'Italian', 'id': 'Indonesian'}
REQUESTS = {  # explicit "reply in X" requests, English or Vietnamese phrasing
    'en': r'english|tiếng anh', 'vi': r'vietnamese|tiếng việt', 'ja': r'japanese|tiếng nhật', 'ko': r'korean|tiếng hàn',
    'zh': r'chinese|tiếng trung', 'th': r'thai|tiếng thái', 'ru': r'russian|tiếng nga', 'fr': r'french|tiếng pháp',
    'es': r'spanish|tiếng tây ban nha', 'de': r'german|tiếng đức', 'pt': r'portuguese', 'it': r'italian', 'id': r'indonesian',
}
REQUEST_VERB = r'(?:reply|respond|answer|speak|write|talk|chat|use|switch to|trả lời|nói|viết|dùng|chuyển sang)'
ENGLISH_WORDS = frozenset('the and to is are you your please can could would this that with for of in on it what how why '
                          'when need want make create check fix build write add use do does not we our my me i be will'.split())
NON_LATIN = re.compile('|'.join(p.pattern for _, p in SCRIPTS))
LITERALS = re.compile(r'```.*?```|`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|«[^»\n]*»|https?://\S+', re.S)


def language_name(code):
    return NAMES.get(code, code or 'English')


def owner_language(text, previous=None):
    """Language for owner-facing replies. Messages without a clear signal keep `previous`."""
    text = str(text or '')
    for code, names in REQUESTS.items():
        if re.search(REQUEST_VERB + r'\s+(?:to me\s+|with me\s+|lại\s+)?(?:in\s+|bằng\s+)?(?:' + names + r')\b', text, re.I):
            return code
    for code, pattern in SCRIPTS:
        if pattern.search(text):
            return code
    if VIETNAMESE.search(text):
        return 'vi'
    words = re.findall(r"[a-zA-Z']+", text.casefold())
    if previous and previous != 'en' and sum(w in ENGLISH_WORDS for w in words) < 2:
        # "ok", "go on", "tiep tuc", file paths or numbers do not switch the conversation language.
        return previous
    return 'en'


def foreign_ratio(text):
    """Share of words outside English, ignoring code, quotes and URLs (literal owner data may stay as-is)."""
    words = re.findall(r'\w+', LITERALS.sub(' ', str(text or '')))
    words = [w for w in words if not w.isdigit()]
    if not words:
        return 0.0
    foreign = sum(1 for w in words if not w.isascii())  # Vietnamese, other accented Latin and non-Latin scripts
    return foreign / len(words)


def require_english(kind, *values, threshold=0.15):
    for value in values:
        if foreign_ratio(value) > threshold:
            raise DomainError(kind + ' must be written in professional English (the internal working language). '
                'Rewrite it in English and retry. Keep owner data, product copy or identifiers only as quoted literals '
                '("…") or code spans. Owner-facing replies are the only place for the owner\'s language.')


def english_authoring(*values):
    require_english('Skills, tools and agent instructions', *values)


POLICY = '''Working-language policy (overrides older role/skill prose):
English is the internal language. Think, reason, plan and take notes in English; write memory,
skills, tool definitions, agent names/roles/instructions, tasks, checklists, peer messages
(team.message, handoffs, reviews), progress notes, code and comments in English. When internal
text must carry owner data (quotes, product copy, names), keep it as a quoted literal.
Owner-facing text only (reply, chat.send to the owner, decisions.ask) uses "Owner reply
language" from the context: the owner's language or the language they explicitly requested;
English when none is established. Do not reveal private chain of thought. Keep identifiers,
file paths, quoted sources and explicitly requested localized deliverables intact. Never
silently rename an existing team or rewrite installed skills merely to change language.'''

AUTHORING = '''Authoring rules for anything you create for reuse (skills, agent instructions, tool
recipes, delegated briefs, memory): optimize for a model reader. Be concise and imperative; no
filler, greetings or restated runtime rules. Skills: YAML front matter (name, one-line
description of when to use it), then Purpose, Inputs, Steps, Verification, Outputs, Boundaries;
<= 6,000 characters; move long examples/reference data into project files and link them.
Update an existing similar skill (skills.update) instead of creating a near-duplicate.
Agent instructions <= 3,000 characters. Memory: one fact or lesson per entry, English, reusable.'''

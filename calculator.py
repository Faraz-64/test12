"""Exact, safe calculator. Parses with `ast` and walks only whitelisted nodes: eval() is never used."""
import ast
import math
import re
from fractions import Fraction


class CalcError(ValueError):
    pass


MAX_LEN = 200            # longest expression accepted
MAX_RESULT_DIGITS = 400  # refuse answers bigger than this (e.g. 9**9**9)
MAX_FACTORIAL = 1000

CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
FLOAT_FUNCS = {
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos,
    "atan": math.atan, "exp": math.exp, "ln": math.log, "log10": math.log10, "log2": math.log2,
    "degrees": math.degrees, "radians": math.radians,
}
ALLOWED_NAMES = set(CONSTANTS) | set(FLOAT_FUNCS) | {"log", "abs", "round", "floor", "ceil", "factorial"}


def _to_fraction(v):
    return v if isinstance(v, Fraction) else Fraction(v)


def _as_float(v):
    return float(v)


def _digits(v):
    return len(str(abs(v.numerator))) + len(str(v.denominator)) if isinstance(v, Fraction) else 0


def _binop(op, a, b):
    both_exact = isinstance(a, Fraction) and isinstance(b, Fraction)
    try:
        if isinstance(op, ast.Add):
            r = a + b
        elif isinstance(op, ast.Sub):
            r = a - b
        elif isinstance(op, ast.Mult):
            r = a * b
        elif isinstance(op, ast.Div):
            r = a / b
        elif isinstance(op, ast.FloorDiv):
            r = a // b
        elif isinstance(op, ast.Mod):
            r = a % b
        elif isinstance(op, ast.Pow):
            r = _pow(a, b)
        else:
            raise CalcError("that operator isn't supported")
    except ZeroDivisionError:
        raise CalcError("division by zero")
    except OverflowError:
        raise CalcError("the number is too large")
    if both_exact and isinstance(r, Fraction) and _digits(r) > MAX_RESULT_DIGITS:
        raise CalcError("the result is too large")
    return r


def _pow(a, b):
    if isinstance(a, Fraction) and isinstance(b, Fraction) and b.denominator == 1:
        exp = b.numerator
        if abs(exp) > 10000:
            raise CalcError("that exponent is too large")
        if a == 0 and exp < 0:
            raise CalcError("division by zero")
        if abs(a) > 1 and abs(exp) * math.log10(abs(float(a)) or 1) > MAX_RESULT_DIGITS:
            raise CalcError("the result is too large")
        return a ** exp
    r = _as_float(a) ** _as_float(b)
    if isinstance(r, complex):
        raise CalcError("that has no real answer")
    return r


def _call(name, args):
    if name == "factorial":
        (n,) = _arity(name, args, 1, 1)
        if not (isinstance(n, Fraction) and n.denominator == 1 and 0 <= n <= MAX_FACTORIAL):
            raise CalcError(f"factorial needs a whole number from 0 to {MAX_FACTORIAL}")
        return Fraction(math.factorial(int(n)))
    if name == "abs":
        (x,) = _arity(name, args, 1, 1)
        return abs(x)
    if name in ("floor", "ceil"):
        (x,) = _arity(name, args, 1, 1)
        return Fraction((math.floor if name == "floor" else math.ceil)(x))
    if name == "round":
        x, *d = _arity(name, args, 1, 2)
        digits = int(d[0]) if d else 0
        return Fraction(round(_to_fraction(x), digits))
    if name == "log":
        x, *b = _arity(name, args, 1, 2)
        return math.log(_as_float(x), _as_float(b[0])) if b else math.log10(_as_float(x))
    fn = FLOAT_FUNCS[name]
    (x,) = _arity(name, args, 1, 1)
    try:
        return fn(_as_float(x))
    except (ValueError, OverflowError):
        raise CalcError(f"{name}() can't take that value")


def _arity(name, args, lo, hi):
    if not lo <= len(args) <= hi:
        raise CalcError(f"{name}() takes {lo if lo == hi else f'{lo} to {hi}'} argument(s)")
    return args


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError("only numbers are allowed")
        return Fraction(str(node.value))  # str() keeps 0.1 as exactly 1/10
    if isinstance(node, ast.Name):
        if node.id in CONSTANTS:
            return CONSTANTS[node.id]
        raise CalcError(f"I don't know '{node.id}'")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _eval(node.operand)
        return v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.BinOp):
        return _binop(node.op, _eval(node.left), _eval(node.right))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_NAMES - set(CONSTANTS) or node.keywords:
            raise CalcError("that function isn't supported")
        return _call(node.func.id, [_eval(a) for a in node.args])
    raise CalcError("that isn't a plain maths expression")


def format_result(v):
    if isinstance(v, Fraction):
        if v.denominator == 1:
            return str(v.numerator)
        d = v.denominator
        for p in (2, 5):
            while d % p == 0:
                d //= p
        if d == 1:  # terminating decimal: print it exactly
            return _decimal(v)
        v = float(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise CalcError("the answer isn't a finite number")
        s = f"{v:.12g}"
        return s
    return str(v)


def _decimal(f):
    sign = "-" if f < 0 else ""
    f = abs(f)
    whole, rem = divmod(f.numerator, f.denominator)
    digits = []
    while rem:
        rem *= 10
        digit, rem = divmod(rem, f.denominator)
        digits.append(str(digit))
        if len(digits) > 40:
            break
    return f"{sign}{whole}.{''.join(digits)}"


def evaluate(expr):
    """Evaluate a maths expression string. Returns the result as text; raises CalcError if it is invalid or unsafe."""
    expr = expr.strip()
    if not expr:
        raise CalcError("nothing to calculate")
    if len(expr) > MAX_LEN:
        raise CalcError(f"that expression is too long (limit {MAX_LEN} characters)")
    try:
        tree = ast.parse(expr, mode="eval")
    except (SyntaxError, ValueError):
        raise CalcError("I couldn't read that as a maths expression")
    if sum(1 for _ in ast.walk(tree)) > 200:
        raise CalcError("that expression is too complicated")
    try:
        return format_result(_eval(tree))
    except RecursionError:
        raise CalcError("that expression is too deeply nested")


_NUM = r"(\d+(?:\.\d+)?)"
_LEAD = re.compile(r"^(?:what(?:'s| is| is the)?|whats|calculate|compute|evaluate|solve|calc|how much is|work out)\s+", re.I)


def normalize(text):
    """Turn everyday phrasing (23 x 47, 20% of 150, 150 + 10%, 2^8) into a Python expression."""
    t = text.strip().lower().rstrip("?=. ").strip()
    t = _LEAD.sub("", t)
    t = t.replace("×", "*").replace("÷", "/").replace("−", "-").replace("^", "**").replace(",", "")
    t = re.sub(r"(?<=[\d)])\s*x\s*(?=[\d(])", "*", t)
    t = re.sub(r"\bper ?cent\b|\bpercent\b", "%", t)
    t = re.sub(r"\bsquare root of\s+" + _NUM, r"sqrt(\1)", t)
    t = re.sub(_NUM + r"\s*%\s*of\s+", r"(\1/100)*", t)
    t = re.sub(_NUM + r"\s*([+-])\s*" + _NUM + r"\s*%(?!\s*(?:of\b|[\d(]))", r"\1\2(\1*\3/100)", t)
    t = re.sub(_NUM + r"\s*%(?!\s*[\d(a-z])", r"(\1/100)", t)
    return t


_ALLOWED_CHARS = re.compile(r"^[0-9a-z_+\-*/().,%\s]*$")


def looks_like_math(norm):
    """True only if the whole message is a maths expression, so ordinary questions still go to the model."""
    if not norm or not re.search(r"\d", norm) or not _ALLOWED_CHARS.match(norm):
        return False
    words = re.findall(r"[a-z_]+", norm)
    if any(w not in ALLOWED_NAMES for w in words):
        return False
    return bool(re.search(r"[+\-*/%]", norm) or re.search(r"[a-z]\s*\(", norm))


def display(text):
    """The expression as the person wrote it (minus 'what is' and the question mark), to show back with the answer."""
    return _LEAD.sub("", text.strip().rstrip("?=. ").strip(), count=1)


def try_calculate(text):
    """For chat: returns (handled, reply). Not maths -> (False, ''), so the message carries on to the model."""
    norm = normalize(text)
    if not looks_like_math(norm):
        return False, ""
    try:
        return True, f"{display(text)} = {evaluate(norm)}"
    except CalcError as e:
        return True, f"I couldn't calculate that: {e}."

// GitHub Actions' expression language, as far as the starter workflows use it: literals,
// contexts with `.`, `[…]` and `*` (object filters), `!`, `==`, `!=`, `&&`, `||`, parentheses,
// and the functions contains, startsWith, endsWith, format, join, toJSON, fromJSON and the status
// checks. Comparisons follow GitHub's rules: strings case-insensitively, other mixed types as
// numbers.
// https://docs.github.com/en/actions/reference/workflows-and-actions/expressions

export type Value = null | boolean | number | string | Value[] | { [key: string]: Value };

export interface Scope {
  /** The contexts by name: github, env, vars, inputs, steps, needs, … */
  contexts: Record<string, Value>;
  /** What success(), failure(), cancelled() and always() return here. */
  status: { success: boolean; failure: boolean; cancelled: boolean };
}

/** An object filter's result (`labels.*.name`), which further properties map over. */
class Filtered {
  readonly items: Value[];
  constructor(items: Value[]) {
    this.items = items;
  }
}
type Result = Value | Filtered | undefined;

const token = /\s*(?:('(?:[^']|'')*')|(\d+(?:\.\d+)?)|(==|!=|<=|>=|&&|\|\||[!<>().,[\]*])|([A-Za-z_][A-Za-z0-9_-]*))/y;

function tokenize(source: string): string[] {
  const tokens: string[] = [];
  token.lastIndex = 0;
  while (token.lastIndex < source.length) {
    if (/^\s*$/.test(source.slice(token.lastIndex))) break;
    const start = token.lastIndex;
    const m = token.exec(source);
    if (!m) throw new Error(`can't read the expression at "${source.slice(start)}"`);
    tokens.push(m[1] ?? m[2] ?? m[3] ?? m[4]);
  }
  return tokens;
}

/** Evaluates one expression, without `${{ }}`. */
export function evaluate(source: string, scope: Scope): Value {
  const tokens = tokenize(source);
  let i = 0;
  const peek = () => tokens[i];
  const take = (expected?: string) => {
    const t = tokens[i++];
    if (expected !== undefined && t !== expected) {
      throw new Error(`expected ${expected} in "${source}", got ${t ?? "the end"}`);
    }
    return t;
  };

  const or = (): Result => {
    let left = and();
    while (peek() === "||") {
      take();
      const right = and();
      left = truthy(left) ? left : right;
    }
    return left;
  };
  const and = (): Result => {
    let left = comparison();
    while (peek() === "&&") {
      take();
      const right = comparison();
      left = truthy(left) ? right : left;
    }
    return left;
  };
  const comparison = (): Result => {
    let left = unary();
    while (["==", "!=", "<", ">", "<=", ">="].includes(peek())) {
      const op = take();
      const right = unary();
      left = compare(op, value(left), value(right));
    }
    return left;
  };
  const unary = (): Result => {
    if (peek() === "!") {
      take();
      return !truthy(unary());
    }
    return postfix();
  };
  const postfix = (): Result => {
    let result = primary();
    for (;;) {
      if (peek() === ".") {
        take();
        const name = take();
        result = name === "*" ? star(result) : property(result, name);
      } else if (peek() === "[") {
        take();
        if (peek() === "*") {
          take();
          take("]");
          result = star(result);
          continue;
        }
        const index = or();
        take("]");
        result = property(result, toText(value(index)));
      } else return result;
    }
  };
  const primary = (): Result => {
    const t = take();
    if (t === undefined) throw new Error(`"${source}" ends too soon`);
    if (t === "(") {
      const result = or();
      take(")");
      return result;
    }
    if (t.startsWith("'")) return t.slice(1, -1).replace(/''/g, "'");
    if (/^\d/.test(t)) return Number(t);
    if (t === "true" || t === "false") return t === "true";
    if (t === "null") return null;
    if (peek() === "(") {
      take();
      const args: Result[] = [];
      while (peek() !== ")") {
        args.push(or());
        if (peek() === ",") take();
      }
      take(")");
      return call(t, args, scope);
    }
    const context = Object.entries(scope.contexts).find(([name]) => name.toLowerCase() === t.toLowerCase());
    if (!context) throw new Error(`no context named ${t} in "${source}"`);
    return context[1];
  };

  const result = or();
  if (i < tokens.length) throw new Error(`unexpected ${tokens[i]} in "${source}"`);
  return value(result);
}

/** Replaces each `${{ … }}` in a string with its value, as a string. */
export function interpolate(text: string, scope: Scope): string {
  return text.replace(/\$\{\{(.*?)\}\}/gs, (_, expression: string) => toText(evaluate(expression, scope)));
}

/** Evaluates an `if:`, which may be wrapped in `${{ }}` or not. */
export function condition(text: string, scope: Scope): boolean {
  const bare = text.trim().replace(/^\$\{\{(.*)\}\}$/s, "$1");
  const usesStatus = /\b(success|failure|cancelled|always)\s*\(/.test(bare);
  // GitHub adds success() && to a condition that names no status function.
  return truthy(evaluate(usesStatus ? bare : `success() && (${bare})`, scope));
}

export function toText(v: Value): string {
  if (v === null) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function value(r: Result): Value {
  if (r instanceof Filtered) return r.items;
  return r === undefined ? null : r;
}

function truthy(r: Result): boolean {
  const v = value(r);
  return !(v === null || v === false || v === 0 || v === "" || Number.isNaN(v));
}

function property(r: Result, name: string): Result {
  if (r instanceof Filtered) {
    return new Filtered(r.items.map((item) => value(property(item, name))).filter((v) => v !== null));
  }
  if (r === null || r === undefined || typeof r !== "object") return null;
  if (Array.isArray(r)) return /^\d+$/.test(name) ? (r[Number(name)] ?? null) : null;
  const key = Object.keys(r).find((k) => k.toLowerCase() === name.toLowerCase());
  return key === undefined ? null : r[key];
}

function star(r: Result): Filtered {
  const items = r instanceof Filtered ? r.items : [value(r)];
  return new Filtered(
    items.flatMap((item) =>
      item === null || typeof item !== "object" ? [] : Array.isArray(item) ? item : Object.values(item),
    ),
  );
}

function toNumber(v: Value): number {
  if (v === null) return 0;
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "number") return v;
  if (typeof v === "string") return v.trim() === "" ? 0 : Number(v);
  return NaN;
}

function equal(a: Value, b: Value): boolean {
  if (typeof a === "string" && typeof b === "string") return a.toLowerCase() === b.toLowerCase();
  const isObject = (v: Value) => v !== null && typeof v === "object";
  if (isObject(a) || isObject(b)) return a === b;
  return toNumber(a) === toNumber(b);
}

function compare(op: string, a: Value, b: Value): boolean {
  if (op === "==") return equal(a, b);
  if (op === "!=") return !equal(a, b);
  const [x, y] = typeof a === "string" && typeof b === "string" ? [a.toLowerCase(), b.toLowerCase()] : [toNumber(a), toNumber(b)];
  return op === "<" ? x < y : op === ">" ? x > y : op === "<=" ? x <= y : x >= y;
}

function call(name: string, args: Result[], scope: Scope): Value {
  const v = args.map(value);
  switch (name.toLowerCase()) {
    case "success":
      return scope.status.success;
    case "failure":
      return scope.status.failure;
    case "cancelled":
      return scope.status.cancelled;
    case "always":
      return true;
    case "contains":
      return Array.isArray(v[0])
        ? v[0].some((item) => equal(item, v[1]))
        : toText(v[0]).toLowerCase().includes(toText(v[1]).toLowerCase());
    case "startswith":
      return toText(v[0]).toLowerCase().startsWith(toText(v[1]).toLowerCase());
    case "endswith":
      return toText(v[0]).toLowerCase().endsWith(toText(v[1]).toLowerCase());
    case "format":
      return toText(v[0]).replace(/\{(\d+)\}/g, (_, n: string) => toText(v[Number(n) + 1]));
    case "join":
      return Array.isArray(v[0]) ? v[0].map(toText).join(v.length > 1 ? toText(v[1]) : ",") : toText(v[0]);
    case "tojson":
      return JSON.stringify(v[0], null, 2);
    case "fromjson":
      return JSON.parse(toText(v[0]));
    default:
      throw new Error(`the test runner has no function ${name}()`);
  }
}

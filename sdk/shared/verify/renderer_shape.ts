// @ts-nocheck
// Static renderer shape inspection; ported from the checker, never executes a renderer.
const EXPRESSION_KEYWORDS = new Set([
  "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
  "throw", "case", "do", "else", "yield", "await", "export", "default",
  "extends",
]);

function skipString(code, i) {
  const quote = code[i];
  let j = i + 1;
  while (j < code.length) {
    const c = code[j];
    if (c === "\\") { j += 2; continue; }
    if (c === quote || c === "\n") return j + 1;
    j++;
  }
  return j;
}

function skipBlock(code, i) {
  // `i` is just past a `{` (a template's `${`); returns the index past its `}`.
  let depth = 1;
  let j = i;
  while (j < code.length) {
    const c = code[j];
    if (c === '"' || c === "'") { j = skipString(code, j); continue; }
    if (c === "`") { j = skipTemplate(code, j); continue; }
    if (c === "{") depth++;
    else if (c === "}" && --depth === 0) return j + 1;
    j++;
  }
  return j;
}

function skipTemplate(code, i) {
  let j = i + 1;
  while (j < code.length) {
    const c = code[j];
    if (c === "\\") { j += 2; continue; }
    if (c === "`") return j + 1;
    if (c === "$" && code[j + 1] === "{") { j = skipBlock(code, j + 2); continue; }
    j++;
  }
  return j;
}

function skipRegex(code, i) {
  let j = i + 1;
  let inClass = false;
  while (j < code.length) {
    const c = code[j];
    if (c === "\\") { j += 2; continue; }
    if (c === "\n") return j;
    if (inClass) {
      if (c === "]") inClass = false;
    } else if (c === "[") {
      inClass = true;
    } else if (c === "/") {
      j++;
      while (j < code.length && /[a-z]/i.test(code[j])) j++;
      return j;
    }
    j++;
  }
  return j;
}

function tokenize(code) {
  const toks = [];
  const regexAllowed = () => {
    const t = toks[toks.length - 1];
    if (!t) return true;
    if (t.type === "punct") return !")]}".includes(t.value);
    return t.type === "name" && EXPRESSION_KEYWORDS.has(t.value);
  };
  let i = 0;
  while (i < code.length) {
    const c = code[i];
    if (/\s/.test(c)) { i++; continue; }
    if (c === "/" && code[i + 1] === "/") {
      const e = code.indexOf("\n", i);
      i = e < 0 ? code.length : e;
      continue;
    }
    if (c === "/" && code[i + 1] === "*") {
      const e = code.indexOf("*/", i + 2);
      i = e < 0 ? code.length : e + 2;
      continue;
    }
    if (c === '"' || c === "'") {
      const end = skipString(code, i);
      toks.push({ type: "str", value: code.slice(i + 1, end - 1) });
      i = end;
      continue;
    }
    if (c === "`") { i = skipTemplate(code, i); toks.push({ type: "str", value: null }); continue; }
    if (/[A-Za-z_$\u0080-\uffff]/.test(c)) {
      let j = i + 1;
      while (j < code.length && /[\w$\u0080-\uffff]/.test(code[j])) j++;
      toks.push({ type: "name", value: code.slice(i, j) });
      i = j;
      continue;
    }
    if (/[0-9]/.test(c)) {
      let j = i + 1;
      while (j < code.length && /[\w.]/.test(code[j])) j++;
      toks.push({ type: "num" });
      i = j;
      continue;
    }
    if (c === "/" && regexAllowed()) { i = skipRegex(code, i); toks.push({ type: "regex" }); continue; }
    if (code.startsWith("...", i)) { toks.push({ type: "punct", value: "..." }); i += 3; continue; }
    toks.push({ type: "punct", value: c });
    i++;
  }
  return toks;
}

const isPunct = (t, v) => !!t && t.type === "punct" && t.value === v;
const isName = (t, v) => !!t && t.type === "name" && (v === undefined || t.value === v);

function closing(toks, i) {
  // Index of the bracket that closes toks[i] ("(", "[" or "{"), or -1.
  let depth = 0;
  for (let j = i; j < toks.length; j++) {
    const t = toks[j];
    if (t.type !== "punct") continue;
    if ("([{".includes(t.value)) depth++;
    else if (")]}".includes(t.value) && --depth === 0) return j;
  }
  return -1;
}

function topLevelIndexes(toks) {
  const out = [];
  let depth = 0;
  toks.forEach((t, i) => {
    if (t.type === "punct" && ")]}".includes(t.value)) depth--;
    if (depth === 0) out.push(i);
    if (t.type === "punct" && "([{".includes(t.value)) depth++;
  });
  return out;
}

function objectKeys(toks, open) {
  // The keys an object literal names, or null when a spread may add more.
  const end = closing(toks, open);
  if (end < 0) return null;
  const keys = new Set();
  let depth = 0;
  let entryStart = true;
  for (let j = open + 1; j < end; j++) {
    const t = toks[j];
    if (depth === 0 && entryStart) {
      entryStart = false;
      if (isPunct(t, "...")) return null;
      const next = toks[j + 1];
      if ((t.type === "name" || (t.type === "str" && t.value !== null)) &&
          (isPunct(next, ":") || isPunct(next, ",") || isPunct(next, "(") ||
           j + 1 === end)) {
        keys.add(t.value);
      }
    }
    if (t.type === "punct") {
      if ("([{".includes(t.value)) depth++;
      else if (")]}".includes(t.value)) depth--;
      else if (t.value === "," && depth === 0) entryStart = true;
    }
  }
  return keys;
}

function exportShape(toks) {
  const top = topLevelIndexes(toks);
  const aliases = new Set(["defineRenderer"]);
  toks.forEach((t, i) => {
    if (isName(t, "defineRenderer") && isName(toks[i + 1], "as") && isName(toks[i + 2])) {
      aliases.add(toks[i + 2].value);
    }
  });

  const binding = (name) => {
    for (const i of top) {
      const t = toks[i];
      if ((isName(t, "const") || isName(t, "let") || isName(t, "var")) &&
          isName(toks[i + 1], name) && isPunct(toks[i + 2], "=")) {
        return { at: i + 3 };
      }
      if ((isName(t, "function") || isName(t, "class")) && isName(toks[i + 1], name)) {
        return { declared: true };
      }
    }
    return null;
  };

  const resolve = (k, hops) => {
    const t = toks[k];
    if (!t || hops > 4) return { kind: "unknown" };
    const callAt = (isName(t) && aliases.has(t.value) && isPunct(toks[k + 1], "(")) ? k + 2
      : (isName(t) && isPunct(toks[k + 1], ".") && isName(toks[k + 2], "defineRenderer") &&
         isPunct(toks[k + 3], "(")) ? k + 4 : -1;
    if (callAt >= 0) {
      const arg = toks[callAt];
      if (isPunct(arg, "{")) return { kind: "renderer", keys: objectKeys(toks, callAt) };
      if (isName(arg) && (isPunct(toks[callAt + 1], ")") || isPunct(toks[callAt + 1], ","))) {
        const b = binding(arg.value);
        if (b && b.at !== undefined && isPunct(toks[b.at], "{")) {
          return { kind: "renderer", keys: objectKeys(toks, b.at) };
        }
      }
      return { kind: "renderer", keys: null };
    }
    if (isPunct(t, "{")) {
      const keys = objectKeys(toks, k);
      if (keys === null) return { kind: "unknown" };
      return keys.has("__collimate_renderer__") ? { kind: "renderer", keys } : { kind: "not" };
    }
    if (isPunct(t, "(")) {
      const end = closing(toks, k);
      if (end > 0 && isPunct(toks[end + 1], "=") && isPunct(toks[end + 2], ">")) return { kind: "not" };
      return resolve(k + 1, hops + 1);
    }
    if (isName(t, "function") || isName(t, "class") || isName(t, "async") ||
        t.type === "str" || t.type === "num" || t.type === "regex") {
      return { kind: "not" };
    }
    if (isName(t)) {
      const next = toks[k + 1];
      if (isPunct(next, "=") && isPunct(toks[k + 2], ">")) return { kind: "not" };
      if (next === undefined || isPunct(next, ";") || isPunct(next, "}") || isPunct(next, ",")) {
        const b = binding(t.value);
        if (b && b.declared) return { kind: "not" };
        if (b) return resolve(b.at, hops + 1);
      }
    }
    return { kind: "unknown" };
  };

  for (const i of top) {
    if (!isName(toks[i], "export")) continue;
    if (isName(toks[i + 1], "default")) return resolve(i + 2, 0);
    if (isPunct(toks[i + 1], "{")) {
      const end = closing(toks, i + 1);
      for (let j = i + 2; j < end; j++) {
        if (isName(toks[j], "default")) {
          if (isName(toks[j - 1], "as") && isName(toks[j - 2])) {
            if (isName(toks[end + 1], "from")) return { kind: "unknown" };
            const b = binding(toks[j - 2].value);
            if (!b) return { kind: "unknown" };
            return b.declared ? { kind: "not" } : resolve(b.at, 1);
          }
          return { kind: "unknown" };   // `export { default } from "..."`
        }
      }
    }
  }
  return { kind: "none" };
}


export function checkRendererExport(code: string): void {
 const shape=exportShape(tokenize(code));
 if (shape.kind !== "renderer") throw new Error("default export must be a statically readable defineRenderer({ component }) result");
 if (shape.keys !== null && !shape.keys.has("component")) throw new Error("defineRenderer was called without a component");
}

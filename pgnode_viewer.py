#!/usr/bin/env python3
"""pgnode_viewer.py: A visualizer for PostgreSQL query trees and plan trees.

Reads logs produced by debug_print_parse / debug_print_rewritten / debug_print_plan
(works with debug_pretty_print either on or off). Outputs:

  PREFIX.json  Structured JSON.
               If the log contains a single dump, the file holds that tree directly;
               if it contains several, a list of [{label, tree}, ...]
  PREFIX.html  Self-contained tree viewer: one box per node, connected by lines

  python3 pgnode_viewer.py plan.log            # plan.json / plan.html
  python3 pgnode_viewer.py plan.log -o out/x   # out/x.json / out/x.html
  cat plan.log | python3 pgnode_viewer.py -    # stdin -> out.json / out.html
"""

import argparse
import json
import re
import sys
from pathlib import Path

WS = " \t\r\n"
DELIMS = "(){}"
NODE_START = re.compile(r"\{(?=[A-Z][A-Z0-9_]*[\s}])")
LOG_HEAD = re.compile(r"(?:LOG|DEBUG\d?|NOTICE|INFO):\s+([^\n]*)")
NUM_INT = re.compile(r"[+-]?\d+")
NUM_FLOAT = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


# ---------------------------------------------------------------- parser
class ParseError(Exception):
    pass


def convert(tok, escaped):
    """Convert a bare token to a Python value. A token whose first character was escaped is always kept as a string."""
    if escaped:
        return tok
    if tok == "<>":
        return None
    if tok == "true":
        return True
    if tok == "false":
        return False
    if NUM_INT.fullmatch(tok):
        return int(tok)
    if NUM_FLOAT.fullmatch(tok):
        return float(tok)
    return tok


class Parser:
    """Recursive-descent parser for the outfuncs.c format {NODE :field value ...}.

    Value forms:  <> (NULL) / number / true,false / bare string / "quoted string" /
                  {NODE ...} / (element ...) / (b ..) bitmapset / (i ..) (o ..) (x ..) /
                  number [ bytes ] (Const datum)
    """

    def __init__(self, text):
        self.s = text
        self.n = len(text)
        self.i = 0

    def err(self, msg):
        line = self.s.count("\n", 0, self.i) + 1
        raise ParseError(f"{msg} (line {line})")

    def ws(self):
        s, n = self.s, self.n
        while self.i < n and s[self.i] in WS:
            self.i += 1

    def peek(self):
        self.ws()
        return self.s[self.i] if self.i < self.n else ""

    def bare(self):
        """Read up to the next delimiter, undoing backslash escapes. Returns (text, whether the first character was escaped)."""
        s, n = self.s, self.n
        start = self.i
        out = []
        first_esc = False
        while self.i < n:
            ch = s[self.i]
            if ch == "\\" and self.i + 1 < n:
                if self.i == start:
                    first_esc = True
                out.append(s[self.i + 1])
                self.i += 2
                continue
            if ch in WS or ch in DELIMS:
                break
            out.append(ch)
            self.i += 1
        return "".join(out), first_esc

    def node(self):
        self.i += 1  # {
        typ, _ = self.bare()
        d = {"_node": typ}
        while True:
            c = self.peek()
            if c == "}":
                self.i += 1
                return d
            if c != ":":
                self.err(f"expected a field name (:xxx) inside {typ}, got {c!r}")
            name, _ = self.bare()
            d[name[1:]] = self.value()

    def value(self):
        c = self.peek()
        if c == "{":
            return self.node()
        if c == "(":
            return self.lst()
        if c == '"':
            return self.string()
        if c == "":
            self.err("unexpected end of input")
        if c in ")}":
            self.err(f"unexpected {c!r}")
        tok, esc = self.bare()
        v = convert(tok, esc)
        if isinstance(v, int) and not isinstance(v, bool) and self.peek() == "[":
            self.i += 1  # Const datum: "length [ byte byte ... ]"
            nums = []
            while self.peek() not in ("]", ""):
                t, _ = self.bare()
                nums.append(convert(t, False))
            self.i += 1
            return {"_datum": {"length": v, "bytes": nums}}
        return v

    def lst(self):
        self.i += 1  # (
        self.ws()
        s, n = self.s, self.n
        kind = None
        if self.i < n and s[self.i] in "biox":
            nxt = s[self.i + 1] if self.i + 1 < n else ""
            if nxt == "" or nxt in WS + ")":
                kind = s[self.i]
                self.i += 1
        items = []
        while True:
            c = self.peek()
            if c == ")":
                self.i += 1
                break
            if c == "":
                self.err("unterminated list")
            items.append(self.value())
        if kind:
            return {
                {"b": "_bitmapset", "i": "_ints", "o": "_oids", "x": "_xids"}[
                    kind
                ]: items
            }
        return items

    def string(self):
        """Read a "..." string. Undo outToken's escaping and drop the newlines and indentation
        (unescaped whitespace) that debug_pretty_print inserts even inside strings."""
        s, n = self.s, self.n
        i = self.i + 1
        out = []
        brace = bracket = 0
        while True:
            if i >= n:
                self.i = i
                self.err("unterminated string")
            ch = s[i]
            if ch == "\\":
                i += 1
                if (
                    i < n and s[i] == "\n"
                ):  # newline and indentation that pretty-print inserts right after a backslash
                    while i < n and s[i] in WS:
                        i += 1
                if i >= n:
                    self.i = i
                    self.err("unterminated string")
                ch = s[i]
                out.append(ch)
                if ch == "{":
                    brace += 1
                elif ch == "}":
                    brace -= 1
                i += 1
            elif ch in WS:
                i += 1  # unescaped whitespace is pretty-print noise
            elif ch == '"':
                j = i + 1
                while j < n and s[j] in WS:
                    j += 1
                nxt = s[j] if j < n else ""
                # end of string: the embedded-JSON brackets are balanced and a delimiter follows
                if brace <= 0 and bracket <= 0 and (nxt == "" or nxt in ')"}:'):
                    i += 1
                    break
                out.append(ch)
                i += 1
            else:
                if ch == "[":
                    bracket += 1
                elif ch == "]":
                    bracket -= 1
                out.append(ch)
                i += 1
        self.i = i
        text = "".join(out)
        st = text.lstrip()
        if st[:1] in ("{", "["):
            try:
                return {"_embedded_json": json.loads(st)}
            except ValueError:
                pass
        return text


def parse_dumps(text):
    """Extract every node tree dump from the log. Returns [(label, tree), ...]."""
    p = Parser(text)
    dumps = []
    pos = 0
    while True:
        m = NODE_START.search(text, pos)
        if not m:
            break
        heads = LOG_HEAD.findall(text[pos : m.start()])
        label = heads[-1].strip().rstrip(":") if heads else "node tree"
        p.i = m.start()
        tree = p.node()
        dumps.append((f"{len(dumps) + 1}. {label}", tree))
        pos = p.i
    return dumps


def plain(v):
    """Strip the embedded-JSON wrappers, leaving plain JSON-ready data."""
    if isinstance(v, dict):
        if "_embedded_json" in v:
            return v["_embedded_json"]
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain(x) for x in v]
    return v


def count_nodes(v):
    if isinstance(v, dict):
        return (1 if "_node" in v else 0) + sum(count_nodes(x) for x in v.values())
    if isinstance(v, list):
        return sum(count_nodes(x) for x in v)
    return 0


# ---------------------------------------------------------------- view model
# box = {title, kind, rows: [[key, text, 'dim'|'']], kids: [{label, box}], collapsed?}
TYPED = ("_bitmapset", "_ints", "_oids", "_xids", "_datum")
SHORT = re.compile(r"^(?:(?:scan|join)\.)?(?:plan\.)?")
CMD = {
    1: "SELECT",
    2: "UPDATE",
    3: "INSERT",
    4: "DELETE",
    5: "MERGE",
    6: "UTILITY",
    7: "NOTHING",
}
RTEKIND = {
    0: "RELATION",
    1: "SUBQUERY",
    2: "JOIN",
    3: "FUNCTION",
    4: "TABLEFUNC",
    5: "VALUES",
    6: "CTE",
    7: "NAMEDTUPLESTORE",
    8: "RESULT",
    9: "GROUP",
}


def shorten(k):
    return SHORT.sub("", k) or k


def fmt(v):
    if v is None:
        return "∅"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return format(v, ".10g")
    return str(v)


def typed_str(v):
    for key, pre in (
        ("_bitmapset", "b"),
        ("_ints", "i"),
        ("_oids", "o"),
        ("_xids", "x"),
    ):
        if key in v:
            return "(" + " ".join([pre] + [str(x) for x in v[key]]) + ")"
    if "_datum" in v:
        d = v["_datum"]
        return f"datum[{d['length']}] " + " ".join(str(x) for x in d["bytes"])
    return json.dumps(v, ensure_ascii=False)


def is_noise(v):
    """Return True for zero, false, NULL, empty and other default-looking values (the viewer can hide these)."""
    if v is None or v == "":
        return True
    if isinstance(v, bool):
        return v is False
    if isinstance(v, (int, float)):
        return v == 0 or v == -1
    if isinstance(v, list):
        return len(v) == 0
    if isinstance(v, dict):
        for key in TYPED[:4]:
            if key in v:
                return not v[key]
    return False


def is_node(x):
    return isinstance(x, dict) and ("_node" in x or "_embedded_json" in x)


def item_str(x):
    return typed_str(x) if isinstance(x, dict) else fmt(x)


def add_field(b, name, v):
    """Add a node field to the box, either as a row or as a child box."""
    if isinstance(v, dict):
        if "_node" in v:
            b["kids"].append({"label": name, "box": box_for_node(v)})
        elif "_embedded_json" in v:
            b["kids"].append(
                {"label": name, "box": json_box(v["_embedded_json"], "embedded JSON")}
            )
        else:
            b["rows"].append([name, typed_str(v), "dim" if is_noise(v) else ""])
        return
    if isinstance(v, list):
        if not v:
            b["rows"].append([name, "[]", "dim"])
        elif all(not is_node(x) and not isinstance(x, list) for x in v):
            b["rows"].append([name, ", ".join(item_str(x) for x in v), ""])
        else:
            for i, x in enumerate(v):
                add_field(b, f"{name}[{i}]", x)
        return
    b["rows"].append([name, fmt(v), "dim" if is_noise(v) else ""])


def box_for_node(node, kind="node"):
    b = {"title": node["_node"], "kind": kind, "rows": [], "kids": []}
    for k, v in node.items():
        if k != "_node":
            add_field(b, shorten(k), v)
    return b


def jnoise(x):
    return (
        x is None
        or x is False
        or x == ""
        or (isinstance(x, (int, float)) and not isinstance(x, bool) and x == 0)
        or x == []
        or x == {}
    )


def json_box(v, title="JSON"):
    b = {"title": title, "kind": "json", "rows": [], "kids": []}
    if isinstance(v, dict):
        for k, x in v.items():
            jfield(b, k, x)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            jfield(b, f"[{i}]", x)
    else:
        b["rows"].append(["value", json.dumps(v, ensure_ascii=False), ""])
    return b


def jfield(b, k, x):
    if isinstance(x, dict):
        if not x:
            b["rows"].append([k, "{}", "dim"])
        elif (
            len(x) == 1
            and isinstance(next(iter(x.values())), (dict, list))
            and next(iter(x.values()))
        ):
            ((key, inner),) = (
                x.items()
            )  # a single key such as {"Columnar": {...}} becomes the box title
            b["kids"].append({"label": k, "box": json_box(inner, key)})
        else:
            b["kids"].append({"label": k, "box": json_box(x, "{ }")})
    elif isinstance(x, list):
        if not x:
            b["rows"].append([k, "[]", "dim"])
        elif all(not isinstance(e, (dict, list)) for e in x):
            b["rows"].append([k, json.dumps(x, ensure_ascii=False), ""])
        else:
            b["kids"].append({"label": k, "box": json_box(x, f"[{len(x)}]")})
    else:
        b["rows"].append(
            [k, json.dumps(x, ensure_ascii=False), "dim" if jnoise(x) else ""]
        )


# ---- plan summary view
SIMPLE_KEYS = (
    "jointype",
    "aggstrategy",
    "indexid",
    "indexorderdir",
    "methods",
    "strategy",
    "numGroups",
)
DETAIL_KEYS = (
    "qual",
    "joinqual",
    "indexqual",
    "indexqualorig",
    "indexorderby",
    "hashclauses",
    "mergeclauses",
    "recheckqual",
    "custom_exprs",
    "custom_scan_tlist",
    "initPlan",
)
CHILD_LISTS = ("appendplans", "mergeplans", "bitmapplans", "custom_plans")


def getf(node, name):
    """Look up a field regardless of its prefix (plan., scan.plan., join.plan., etc.)."""
    for p in ("", "plan.", "scan.plan.", "join.plan.", "scan.", "join."):
        if p + name in node:
            return node[p + name]
    return None


def is_plan(x):
    return isinstance(x, dict) and "_node" in x and getf(x, "plan_node_id") is not None


def rte_text(idx, rtable):
    if (
        not isinstance(idx, int)
        or idx <= 0
        or idx > len(rtable)
        or not isinstance(rtable[idx - 1], dict)
    ):
        return None
    rte = rtable[idx - 1]
    eref = rte.get("eref") if isinstance(rte.get("eref"), dict) else {}
    kind = RTEKIND.get(rte.get("rtekind"), "?")
    relid = f", relid {rte['relid']}" if rte.get("relid") else ""
    return f"{eref.get('aliasname') or '?'} [{kind}{relid}]"


def plan_box(node, rtable):
    tl = [e for e in (getf(node, "targetlist") or []) if isinstance(e, dict)]
    rows = [
        [
            "cost",
            f"{fmt(getf(node, 'startup_cost'))} .. {fmt(getf(node, 'total_cost'))}",
        ],
        [
            "rows / width",
            f"{fmt(getf(node, 'plan_rows'))} / {fmt(getf(node, 'plan_width'))}",
        ],
    ]
    rel = rte_text(getf(node, "scanrelid"), rtable)
    if rel:
        rows.append(["relation", rel])
    for k in SIMPLE_KEYS:
        v = getf(node, k)
        if v is not None:
            rows.append([k, fmt(v)])
    sk = getf(node, "sortColIdx")
    if isinstance(sk, list) and sk:
        rows.append(
            [
                "sort key",
                ", ".join(
                    (tl[i - 1].get("resname") or f"#{i}")
                    for i in sk
                    if isinstance(i, int) and 0 < i <= len(tl)
                ),
            ]
        )
    if tl:
        rows.append(
            [
                "output",
                ", ".join(
                    (e.get("resname") or "?") + (" (junk)" if e.get("resjunk") else "")
                    for e in tl
                ),
            ]
        )
    if getf(node, "parallel_aware"):
        rows.append(["parallel_aware", "true"])
    dn = getf(node, "disabled_nodes")
    if isinstance(dn, (int, float)) and dn > 0:
        rows.append(["disabled_nodes", fmt(dn)])

    b = {"title": node["_node"], "kind": "plan", "rows": rows, "kids": []}
    for name in ("lefttree", "righttree"):
        c = getf(node, name)
        if is_plan(c):
            b["kids"].append({"label": name, "box": plan_box(c, rtable)})
    for name in CHILD_LISTS:
        for i, c in enumerate(node.get(name) or []):
            if is_plan(c):
                b["kids"].append({"label": f"{name}[{i}]", "box": plan_box(c, rtable)})
    if is_plan(node.get("subplan")):
        b["kids"].append({"label": "subplan", "box": plan_box(node["subplan"], rtable)})

    cp = node.get(
        "custom_private"
    )  # CustomScan's extension-specific data (JSON in ParadeDB's case)
    if isinstance(cp, list):
        for i, x in enumerate(cp):
            label = "custom_private" + (f"[{i}]" if len(cp) > 1 else "")
            if isinstance(x, dict) and "_embedded_json" in x:
                b["kids"].append(
                    {
                        "label": label,
                        "box": json_box(x["_embedded_json"], "custom_private (JSON)"),
                    }
                )
            elif isinstance(x, str):
                rows.append([label, x, ""])
            elif isinstance(x, dict) and "_node" in x:
                b["kids"].append({"label": label, "box": box_for_node(x)})

    for name in DETAIL_KEYS:
        v = getf(node, name)
        if not v or is_noise(v):
            continue
        tmp = {"rows": [], "kids": []}
        add_field(tmp, name, v)
        if tmp["kids"]:
            b["kids"].append(
                {
                    "label": "",
                    "box": {
                        "title": name,
                        "kind": "group",
                        "rows": tmp["rows"],
                        "kids": tmp["kids"],
                        "collapsed": True,
                    },
                }
            )
    return b


def plannedstmt_box(ps):
    rtable = [x for x in (ps.get("rtable") or []) if x is not None]
    ct = ps.get("commandType")
    b = {
        "title": "PLANNEDSTMT",
        "kind": "node",
        "rows": [["command", CMD.get(ct, fmt(ct))]],
        "kids": [],
    }
    for k in ("queryId", "planId", "jitFlags", "stmt_len"):
        if ps.get(k):
            b["rows"].append([k, fmt(ps[k])])
    for k in (
        "hasReturning",
        "hasModifyingCTE",
        "transientPlan",
        "dependsOnRole",
        "parallelModeNeeded",
    ):
        if ps.get(k):
            b["rows"].append([k, "true"])
    if isinstance(ps.get("relationOids"), dict):
        b["rows"].append(["relationOids", typed_str(ps["relationOids"]), ""])
    if is_plan(ps.get("planTree")):
        b["kids"].append({"label": "planTree", "box": plan_box(ps["planTree"], rtable)})
    for i, sp in enumerate(ps.get("subplans") or []):
        if is_plan(sp):
            b["kids"].append({"label": f"subplans[{i}]", "box": plan_box(sp, rtable)})
    if rtable:
        b["kids"].append(
            {
                "label": "rtable",
                "box": {
                    "title": f"rtable ({len(rtable)})",
                    "kind": "group",
                    "kids": [],
                    "rows": [
                        [str(i + 1), rte_text(i + 1, rtable) or "?", ""]
                        for i in range(len(rtable))
                    ],
                },
            }
        )
    return b


def build_views(tree):
    views = {"full": box_for_node(tree)}
    if tree["_node"] == "PLANNEDSTMT" and is_plan(tree.get("planTree")):
        views["plan"] = plannedstmt_box(tree)
    return views


# ---------------------------------------------------------------- html
TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>pgnode_viewer</title>
<style>
:root{--bg:#f6f7f9;--fg:#1d2330;--mut:#667085;--box:#fff;--line:#98a2b3;--bd:#d0d5dd;--row:#eef0f4;
--plan:#2f6fdb;--json:#c27803;--grp:#7a8499;--node:#475467}
@media (prefers-color-scheme:dark){:root{--bg:#14171d;--fg:#e4e7ee;--mut:#8f98ab;--box:#1d222c;--line:#566074;
--bd:#343b4a;--row:#262c38;--plan:#6aa2ff;--json:#f0a64a;--grp:#8f98ab;--node:#9aa3b5}}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{display:flex;flex-direction:column;background:var(--bg);color:var(--fg);
font:13px/1.4 -apple-system,"Segoe UI","Hiragino Sans","Noto Sans JP",sans-serif}
#bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 12px;background:var(--box);
border-bottom:1px solid var(--bd)}
#bar select,#bar button{font:inherit;color:var(--fg);background:var(--box);border:1px solid var(--bd);
border-radius:6px;padding:3px 9px;cursor:pointer}
#bar button:hover{background:var(--row)}
#bar label{color:var(--mut);display:flex;gap:4px;align-items:center;cursor:pointer}
#bar .sp{flex:1}
#zl{min-width:3.2em;text-align:center;color:var(--mut)}
.legend{display:flex;gap:10px;color:var(--mut);font-size:12px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
#viewport{flex:1;overflow:auto;position:relative;cursor:grab}
#sizer{position:relative}
#stage{position:absolute;left:0;top:0;transform-origin:0 0}
svg{position:absolute;left:0;top:0;overflow:visible;pointer-events:none}
.edge{fill:none;stroke:var(--line);stroke-width:1.4}
.box{position:absolute;background:var(--box);border:1px solid var(--bd);border-left:4px solid var(--node);
border-radius:6px;min-width:150px;max-width:460px;font-size:12px;box-shadow:0 1px 2px rgba(16,24,40,.08)}
.box.plan{border-left-color:var(--plan)}
.box.json{border-left-color:var(--json)}
.box.group{border-left-color:var(--grp);border-style:dashed;border-left-style:solid}
.head{padding:5px 8px}
.head.clickable{cursor:pointer}
.head.clickable:hover{background:var(--row);border-radius:5px 5px 0 0}
.elabel{font-size:10.5px;color:var(--mut);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.title{font-weight:600;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.tog{float:right;margin-left:12px;color:var(--mut);font-weight:400;font-size:11px}
.row{display:flex;gap:10px;padding:1px 8px;border-top:1px solid var(--row)}
.k{color:var(--mut);flex:none}
.v{margin-left:auto;text-align:right;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:320px}
</style></head>
<body>
<div id="bar">
<select id="dump" title="Dump in the log"></select>
<select id="view"><option value="plan">Plan summary</option><option value="full">All nodes</option></select>
<label><input type="checkbox" id="hide" checked>Hide 0 / false / -1 / NULL / empty</label>
<button id="expand">Expand all</button><button id="collapse">Collapse all</button>
<button id="zout">−</button><span id="zl">100%</span><button id="zin">+</button><button id="fit">Fit to view</button>
<span class="sp"></span>
<span class="legend"><span><i style="background:var(--plan)"></i>Plan node</span>
<span><i style="background:var(--json)"></i>Embedded JSON</span>
<span><i style="background:var(--node)"></i>Other node</span></span>
</div>
<div id="viewport"><div id="sizer"><div id="stage"></div></div></div>
<script>
const DUMPS = /*DATA*/null;
const NS = 'http://www.w3.org/2000/svg';
const $ = id => document.getElementById(id);
const vp = $('viewport'), sizer = $('sizer'), stage = $('stage');
let cur = 0, view = 'plan', hideDim = true, zoom = 1, size = {W: 1, H: 1};

function initState(b, depth, isFull) {
  b.c = b.collapsed === true || (isFull && depth >= 4 && b.kids.length > 0);
  b.kids.forEach(k => initState(k.box, depth + 1, isFull));
}
DUMPS.forEach(d => {
  if (d.views.plan) initState(d.views.plan, 0, false);
  initState(d.views.full, 0, true);
});

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
const root = () => { const d = DUMPS[cur]; return (view === 'plan' && d.views.plan) ? d.views.plan : d.views.full; };
function each(b, f) { f(b); b.kids.forEach(k => each(k.box, f)); }

function makeBox(b, label) {
  const e = el('div', 'box ' + (b.kind || 'node'));
  const h = el('div', 'head');
  if (label) h.appendChild(el('div', 'elabel', label));
  const t = el('div', 'title', b.title);
  if (b.kids.length) {
    t.appendChild(el('span', 'tog', (b.c ? '+' : '−') + b.kids.length));
    h.classList.add('clickable');
    h.onclick = () => { b.c = !b.c; render(false); };
  }
  h.appendChild(t);
  e.appendChild(h);
  b.rows.forEach(r => {
    if (hideDim && r[2] === 'dim') return;
    const row = el('div', 'row');
    row.appendChild(el('span', 'k', r[0]));
    const full = String(r[1]);
    const v = el('span', 'v', full.length > 80 ? full.slice(0, 80) + '…' : full);
    v.title = full;
    row.appendChild(v);
    e.appendChild(row);
  });
  return e;
}

function render(doFit) {
  stage.innerHTML = '';
  const svg = document.createElementNS(NS, 'svg');
  stage.appendChild(svg);
  const nodes = [];
  let rootN = null;
  (function walk(b, depth, parent, label) {
    const e = makeBox(b, label);
    stage.appendChild(e);
    const n = {b, e, depth, parent, kids: []};
    if (parent) parent.kids.push(n); else rootN = n;
    nodes.push(n);
    if (!b.c) b.kids.forEach(k => walk(k.box, depth + 1, n, k.label));
  })(root(), 0, null, '');

  const GX = 80, GY = 14, PAD = 24;
  nodes.forEach(n => { n.w = n.e.offsetWidth; n.h = n.e.offsetHeight; });
  const maxW = [];
  nodes.forEach(n => { maxW[n.depth] = Math.max(maxW[n.depth] || 0, n.w); });
  const xs = []; let x = PAD;
  maxW.forEach((w, d) => { xs[d] = x; x += w + GX; });
  const kidsH = n => n.kids.length ? n.kids.reduce((a, k) => a + k.sh, 0) + GY * (n.kids.length - 1) : 0;
  (function sh(n) { n.kids.forEach(sh); n.sh = Math.max(n.h, kidsH(n)); })(rootN);
  (function place(n, top) {
    n.x = xs[n.depth];
    n.y = top + (n.sh - n.h) / 2;
    let cy = top + (n.sh - kidsH(n)) / 2;
    n.kids.forEach(k => { place(k, cy); cy += k.sh + GY; });
  })(rootN, PAD);

  nodes.forEach(n => {
    n.e.style.left = n.x + 'px';
    n.e.style.top = n.y + 'px';
    if (n.parent) {
      const p = n.parent, x1 = p.x + p.w, y1 = p.y + p.h / 2, x2 = n.x, y2 = n.y + n.h / 2, m = (x2 - x1) / 2;
      const path = document.createElementNS(NS, 'path');
      path.setAttribute('class', 'edge');
      path.setAttribute('d', `M${x1} ${y1} C${x1 + m} ${y1} ${x2 - m} ${y2} ${x2} ${y2}`);
      svg.appendChild(path);
    }
  });
  const W = x - GX + PAD, H = rootN.sh + PAD * 2;
  size = {W, H};
  stage.style.width = W + 'px'; stage.style.height = H + 'px';
  svg.setAttribute('width', W); svg.setAttribute('height', H);
  if (doFit) fit(); else applyZoom();
}

function applyZoom() {
  stage.style.transform = `scale(${zoom})`;
  sizer.style.width = size.W * zoom + 'px';
  sizer.style.height = size.H * zoom + 'px';
  $('zl').textContent = Math.round(zoom * 100) + '%';
}
function setZoom(z) { zoom = Math.max(0.1, Math.min(3, z)); applyZoom(); }
function fit() {
  zoom = Math.max(0.25, Math.min(1, (vp.clientWidth - 20) / size.W, (vp.clientHeight - 20) / size.H));
  applyZoom(); vp.scrollTo(0, 0);
}

const dsel = $('dump');
DUMPS.forEach((d, i) => { const o = el('option', null, d.label); o.value = i; dsel.appendChild(o); });
function syncView() {
  const d = DUMPS[cur];
  $('view').querySelector('option[value=plan]').disabled = !d.views.plan;
  $('view').value = view;
}
function pickView() { view = DUMPS[cur].views.plan ? 'plan' : 'full'; syncView(); }
dsel.onchange = () => { cur = +dsel.value; pickView(); render(true); };
$('view').onchange = e => { view = e.target.value; render(true); };
$('hide').onchange = e => { hideDim = e.target.checked; render(false); };
$('expand').onclick = () => { each(root(), b => { b.c = false; }); render(false); };
$('collapse').onclick = () => { const r = root(); each(r, b => { b.c = true; }); r.c = false; render(false); };
$('zin').onclick = () => setZoom(zoom * 1.2);
$('zout').onclick = () => setZoom(zoom / 1.2);
$('fit').onclick = fit;
vp.addEventListener('wheel', e => {
  if (e.ctrlKey || e.metaKey) { e.preventDefault(); setZoom(zoom * (e.deltaY < 0 ? 1.1 : 0.9)); }
}, {passive: false});
let drag = null;
vp.addEventListener('mousedown', e => {
  if (e.target.closest('.box')) return;
  drag = {x: e.clientX, y: e.clientY, l: vp.scrollLeft, t: vp.scrollTop}; vp.style.cursor = 'grabbing';
});
window.addEventListener('mousemove', e => {
  if (!drag) return;
  vp.scrollLeft = drag.l - (e.clientX - drag.x); vp.scrollTop = drag.t - (e.clientY - drag.y);
});
window.addEventListener('mouseup', () => { drag = null; vp.style.cursor = 'grab'; });

pickView();
render(true);
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("input", help="log file (- for stdin)")
    ap.add_argument("-o", "--out", help="the prefix of output file")
    a = ap.parse_args()

    if a.input == "-":
        text = sys.stdin.read()
        prefix = a.out or "out"
    else:
        text = Path(a.input).read_text(encoding="utf-8", errors="replace")
        prefix = a.out or str(Path(a.input).with_suffix(""))
    try:
        dumps = parse_dumps(text)
    except ParseError as e:
        sys.exit(f"parse error: {e}")
    if not dumps:
        sys.exit(
            "no node tree found (pass a log containing a dump such as {PLANNEDSTMT ...})"
        )

    if len(dumps) == 1:
        out_json = plain(dumps[0][1])
    else:
        out_json = [{"label": lb, "tree": plain(t)} for lb, t in dumps]
    Path(prefix + ".json").parent.mkdir(parents=True, exist_ok=True)
    Path(prefix + ".json").write_text(
        json.dumps(out_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    data = [{"label": lb, "views": build_views(t)} for lb, t in dumps]
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    Path(prefix + ".html").write_text(
        TEMPLATE.replace("/*DATA*/null", blob), encoding="utf-8"
    )

    total = sum(count_nodes(t) for _, t in dumps)
    print(
        f"{len(dumps)} logs was dumped / {total} nodes -> {prefix}.json, {prefix}.html"
    )


if __name__ == "__main__":
    main()

/* Planner logic shared by the Mini App UI and the Node tests. No DOM access here. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.Core = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const pad = (n) => String(n).padStart(2, "0");
  const ymd = (y, m0, d) => `${y}-${pad(m0 + 1)}-${pad(d)}`; // m0 is 0-based

  function parseYmd(s) {
    const [y, m, d] = s.split("-").map(Number);
    return { y, m: m - 1, d };
  }

  function addDays(s, n) {
    const { y, m, d } = parseYmd(s);
    const dt = new Date(Date.UTC(y, m, d + n));
    return ymd(dt.getUTCFullYear(), dt.getUTCMonth(), dt.getUTCDate());
  }

  // Monday = 0 ... Sunday = 6
  function weekday(s) {
    const { y, m, d } = parseYmd(s);
    return (new Date(Date.UTC(y, m, d)).getUTCDay() + 6) % 7;
  }

  function daysInMonth(y, m0) {
    return new Date(Date.UTC(y, m0 + 1, 0)).getUTCDate();
  }

  // The wall clock in the bot's timezone, as plain strings plus a comparable number.
  function nowLocal(offsetMin, nowMs) {
    const t = (nowMs === undefined ? Date.now() : nowMs) + offsetMin * 60000;
    const dt = new Date(t);
    return {
      date: ymd(dt.getUTCFullYear(), dt.getUTCMonth(), dt.getUTCDate()),
      time: `${pad(dt.getUTCHours())}:${pad(dt.getUTCMinutes())}`,
      ms: t,
    };
  }

  function dueMs(due) {
    const [d, t] = due.split("T");
    const { y, m, d: day } = parseYmd(d);
    const [hh, mm] = t.split(":").map(Number);
    return Date.UTC(y, m, day, hh, mm);
  }

  const minutesUntil = (due, now) => (dueMs(due) - now.ms) / 60000;

  function fmtCountdown(mins) {
    const m = Math.max(0, Math.floor(mins));
    if (m < 60) return `${m}m`;
    if (m < 1440) return `${Math.floor(m / 60)}h ${m % 60}m`;
    return `${Math.floor(m / 1440)}d ${Math.floor((m % 1440) / 60)}h`;
  }

  // How close a deadline is: drives colour in the UI.
  function urgency(mins) {
    if (mins < 0) return "past";
    if (mins < 1440) return "hot";
    if (mins < 4320) return "warn";
    return "calm";
  }

  // Month grid, weeks start on Monday; only as many rows as the month needs.
  function monthGrid(y, m0) {
    const first = ymd(y, m0, 1);
    const lead = weekday(first);
    const rows = Math.ceil((lead + daysInMonth(y, m0)) / 7);
    const start = addDays(first, -lead);
    const cells = [];
    for (let i = 0; i < rows * 7; i++) {
      const date = addDays(start, i);
      cells.push({ date, inMonth: parseYmd(date).m === m0 });
    }
    return cells;
  }

  const clone = (x) => JSON.parse(JSON.stringify(x));

  /* Apply operations to a {deadlines, aims} view. Items touched get `flag` = true.
     New items take op.tempId, or `${prefix}${n}` when the op has none. */
  function applyOps(view, ops, opts) {
    const flag = (opts && opts.flag) || "pending";
    const prefix = (opts && opts.prefix) || "x";
    let n = 0;
    const find = (list, id) => list.find((x) => x.id === id);
    for (const op of ops) {
      switch (op.op) {
        case "add_deadline":
          view.deadlines.push({ id: op.tempId || prefix + ++n, title: op.title, due: op.due, [flag]: true });
          break;
        case "edit_deadline": {
          const d = find(view.deadlines, op.id);
          if (d) {
            if (op.title !== undefined) d.title = op.title;
            if (op.due !== undefined) d.due = op.due;
            d[flag] = true;
          }
          break;
        }
        case "remove_deadline":
          view.deadlines = view.deadlines.filter((x) => x.id !== op.id);
          break;
        case "add_aim":
          view.aims.push({ id: op.tempId || prefix + ++n, day: op.day, text: op.text, done: !!op.done, [flag]: true });
          break;
        case "edit_aim": {
          const a = find(view.aims, op.id);
          if (a) {
            if (op.text !== undefined) a.text = op.text;
            if (op.day !== undefined) a.day = op.day;
            a[flag] = true;
          }
          break;
        }
        case "done_aim": {
          const a = find(view.aims, op.id);
          if (a) {
            a.done = !!op.done;
            a[flag] = true;
          }
          break;
        }
        case "remove_aim":
          view.aims = view.aims.filter((x) => x.id !== op.id);
          break;
      }
    }
    return view;
  }

  const isTemp = (id) => typeof id === "string";

  /* Holds the server state plus the changes the person has made but not yet saved.
     `sent` are batches already sent to the bot that its next run hasn't published yet. */
  class Planner {
    constructor(state, sent) {
      this.state = state;
      this.sent = sent || [];
      this.ops = [];
      this.n = 0;
    }

    setState(state, sent) {
      this.state = state;
      this.sent = sent || [];
    }

    view() {
      const v = { deadlines: clone(this.state.deadlines || []), aims: clone(this.state.aims || []) };
      this.sent.forEach((entry, i) => applyOps(v, entry.ops, { flag: "syncing", prefix: `s${i}_` }));
      applyOps(v, this.ops, { flag: "pending" });
      v.deadlines.sort((a, b) => (a.due < b.due ? -1 : a.due > b.due ? 1 : 0));
      return v; // aims keep insertion order within a day (stable sort)
    }

    get count() {
      return this.ops.length;
    }

    discard() {
      this.ops = [];
    }

    _addOp(id) {
      return this.ops.find((o) => o.tempId === id);
    }

    _drop(id, kinds) {
      this.ops = this.ops.filter((o) => !(o.id === id && kinds.includes(o.op)));
    }

    _edit(kind, id, patch, keys) {
      const add = isTemp(id) ? this._addOp(id) : null;
      const target =
        add || this.ops.find((o) => o.op === kind && o.id === id) || (this.ops.push({ op: kind, id }), this.ops[this.ops.length - 1]);
      for (const k of keys) if (patch[k] !== undefined) target[k] = patch[k];
    }

    addDeadline(title, due) {
      const id = "n" + ++this.n;
      this.ops.push({ op: "add_deadline", tempId: id, title, due });
      return id;
    }

    editDeadline(id, patch) {
      this._edit("edit_deadline", id, patch, ["title", "due"]);
    }

    removeDeadline(id) {
      if (isTemp(id)) {
        this.ops = this.ops.filter((o) => o.tempId !== id);
        return;
      }
      this._drop(id, ["edit_deadline"]);
      this.ops.push({ op: "remove_deadline", id });
    }

    addAim(day, text) {
      const id = "n" + ++this.n;
      this.ops.push({ op: "add_aim", tempId: id, day, text, done: false });
      return id;
    }

    editAim(id, patch) {
      this._edit("edit_aim", id, patch, ["text", "day"]);
    }

    setDone(id, done) {
      if (isTemp(id)) {
        this._addOp(id).done = done;
        return;
      }
      this._drop(id, ["done_aim"]);
      const orig = (this.state.aims || []).find((a) => a.id === id);
      if (!orig || !!orig.done !== done) this.ops.push({ op: "done_aim", id, done });
    }

    removeAim(id) {
      if (isTemp(id)) {
        this.ops = this.ops.filter((o) => o.tempId !== id);
        return;
      }
      this._drop(id, ["edit_aim", "done_aim"]);
      this.ops.push({ op: "remove_aim", id });
    }

    // Operations in the form the bot expects (no temp ids).
    wireOps() {
      return this.ops.map((o) => {
        const { tempId, ...rest } = o;
        return rest;
      });
    }

    payload(batch) {
      const json = JSON.stringify({ batch, ops: this.wireOps() });
      return { json, bytes: new TextEncoder().encode(json).length };
    }
  }

  return {
    pad, ymd, parseYmd, addDays, weekday, daysInMonth, nowLocal, dueMs, minutesUntil,
    fmtCountdown, urgency, monthGrid, applyOps, Planner, MAX_BYTES: 4096,
  };
});
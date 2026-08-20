/* The design's own dialect, rendered without a framework.
 *
 * The screens came back from Claude Design as one file of HTML and CSS with
 * a very small template language sprinkled through it: `{{ name }}` to put a
 * value somewhere, `<sc-if>` to show or hide, `<sc-for>` to repeat, an
 * `onClick` attribute, and a `style-hover` attribute for the hover look.
 * That is the whole language — five things.
 *
 * The file arrives expecting React and Claude Design's own runtime to render
 * it. Neither can be here: this tool installs from a zip onto a Windows
 * machine with no node and no build step, and a page that needs `npm` before
 * it opens is a page nobody will ever see.
 *
 * So this reads the same dialect directly. Roughly two hundred lines, no
 * dependencies, and — the part that matters — the design files stay exactly
 * as they were exported. A new design drops in over the old one instead of
 * being translated by hand, which is the difference between a design that
 * can be changed and a design that gets changed once.
 *
 * ## What is deliberately NOT supported
 *
 * `{{ }}` holds a name, a dotted name, or a literal. Not an expression.
 * The design never writes one, and a page that can evaluate arbitrary text
 * is a page that can be made to do something it was not asked to — the same
 * reason the server hands out an extension allow-list rather than any file
 * a URL asks for nicely. Anything with real logic in it belongs in the
 * screen's own code, where it can be read.
 */
(function (global) {
  "use strict";

  var TEMPLATE_TAGS = { "SC-IF": 1, "SC-FOR": 1 };
  var EVENTS = { onclick: "click", onmouseenter: "mouseenter",
                 onmouseleave: "mouseleave", onchange: "change",
                 oninput: "input", onsubmit: "submit" };
  var HOVER = { "style-hover": ["mouseenter", "mouseleave"],
                "style-focus": ["focus", "blur"],
                "style-active": ["mousedown", "mouseup"] };

  /* ---------------------------------------------------------------- values */

  var LITERALS = { "true": true, "false": false, "null": null,
                   "undefined": undefined };

  function lookup(scope, path) {
    var parts = String(path).split(".");
    var at = scope;
    for (var i = 0; i < parts.length; i++) {
      if (at === null || at === undefined) return undefined;
      at = at[parts[i]];
    }
    return at;
  }

  // A name, a dotted name, or a literal — see the note at the top of the file
  // about why this is not an expression evaluator.
  function value(expr, scope) {
    var text = String(expr == null ? "" : expr).trim();
    if (!text) return undefined;
    if (Object.prototype.hasOwnProperty.call(LITERALS, text)) {
      return LITERALS[text];
    }
    if (/^-?\d+(\.\d+)?$/.test(text)) return parseFloat(text);
    var quoted = text.match(/^'([^']*)'$/) || text.match(/^"([^"]*)"$/);
    if (quoted) return quoted[1];
    return lookup(scope, text);
  }

  var HOLE = /\{\{([^}]*)\}\}/g;

  function fill(text, scope) {
    if (text.indexOf("{{") === -1) return text;
    return text.replace(HOLE, function (_, expr) {
      var got = value(expr, scope);
      return got === undefined || got === null ? "" : String(got);
    });
  }

  // Whether the whole string is one hole — `{{ x }}` and nothing else. Those
  // keep their real type, so a handler stays a function and a list stays a
  // list instead of arriving as "[object Object]".
  function whole(text) {
    var m = String(text).match(/^\s*\{\{([^}]*)\}\}\s*$/);
    return m ? m[1] : null;
  }

  function resolve(text, scope) {
    var only = whole(text);
    return only === null ? fill(text, scope) : value(only, scope);
  }

  /* --------------------------------------------------------------- styling */

  function declarations(css) {
    var out = [];
    String(css || "").split(";").forEach(function (bit) {
      var at = bit.indexOf(":");
      if (at < 1) return;
      out.push([bit.slice(0, at).trim(), bit.slice(at + 1).trim()]);
    });
    return out;
  }

  /* Hover, focus and pressed looks are applied as inline style on the way in
   * and taken off again on the way out, rather than written into a
   * stylesheet. A stylesheet would need `!important` on every rule to beat
   * the inline style the design already sets, and would grow on every
   * re-render. This grows nothing and reads the same. */
  function reactive(el, css, enter, leave) {
    var pairs = declarations(css);
    if (!pairs.length) return;
    var before = null;
    el.addEventListener(enter, function () {
      if (before) return;                       // already in; do not stack
      before = pairs.map(function (p) { return el.style.getPropertyValue(p[0]); });
      pairs.forEach(function (p) { el.style.setProperty(p[0], p[1]); });
    });
    el.addEventListener(leave, function () {
      if (!before) return;
      pairs.forEach(function (p, i) {
        if (before[i]) el.style.setProperty(p[0], before[i]);
        else el.style.removeProperty(p[0]);
      });
      before = null;
    });
  }

  /* --------------------------------------------------------------- building */

  function attribute(el, name, raw, scope) {
    var lower = name.toLowerCase();
    if (lower.indexOf("hint-") === 0) return;   // placeholders for the editor
    if (HOVER[lower]) {
      reactive(el, fill(raw, scope), HOVER[lower][0], HOVER[lower][1]);
      return;
    }
    if (EVENTS[lower]) {
      var fn = resolve(raw, scope);
      if (typeof fn === "function") {
        el.addEventListener(EVENTS[lower], function (ev) { fn(ev, scope); });
        if (!el.style.cursor) el.style.cursor = "pointer";
      }
      return;
    }
    var got = resolve(raw, scope);
    if (got === false || got === null || got === undefined) {
      // A false `checked` or `disabled` must be absent, not the string
      // "false" — which every browser reads as present and therefore true.
      return;
    }
    if (got === true) { el.setAttribute(name, ""); return; }
    el.setAttribute(name, String(got));
    // Inputs read their live state from properties, not attributes: without
    // this a re-render leaves the box showing whatever the user last typed.
    if (name === "value" || name === "checked") {
      try { el[name] = got; } catch (e) { /* not an input; the attribute did */ }
    }
  }

  function children(node, scope, into) {
    for (var i = 0; i < node.childNodes.length; i++) {
      build(node.childNodes[i], scope, into);
    }
  }

  // Pictures and clips are kept across a redraw and re-used by their src.
  // Rebuilding them means the browser fetches and decodes every one again,
  // which on a page that redraws once a second while a build runs is a
  // constant flicker — and looks exactly like something malfunctioning.
  var media = {};
  var kept = {};

  function reuse(node, scope) {
    var name = node.localName;
    if (name !== "img" && name !== "video") return null;
    var src = fill(node.getAttribute("src") || "", scope);
    if (!src) return null;
    var had = media[src];
    if (!had) {
      had = document.createElementNS(node.namespaceURI ||
        "http://www.w3.org/1999/xhtml", name);
      for (var a = 0; a < node.attributes.length; a++) {
        attribute(had, node.attributes[a].name, node.attributes[a].value,
                  scope);
      }
      media[src] = had;
    }
    kept[src] = true;
    return had;
  }

  function build(node, scope, into) {
    if (node.nodeType === 3) {                          // text
      var text = fill(node.nodeValue, scope);
      if (text) into.appendChild(document.createTextNode(text));
      return;
    }
    if (node.nodeType === 8) return;                    // comment
    if (node.nodeType !== 1) return;

    var tag = node.tagName.toUpperCase();
    if (tag === "SC-IF") {
      if (value(whole(node.getAttribute("value") || "") || "", scope)) {
        children(node, scope, into);
      }
      return;
    }
    if (tag === "SC-FOR") {
      var list = resolve(node.getAttribute("list") || "", scope);
      var as = node.getAttribute("as") || "item";
      if (!list || typeof list.length !== "number") return;
      for (var i = 0; i < list.length; i++) {
        var inner = Object.create(scope);
        inner[as] = list[i];
        inner[as + "Index"] = i;
        children(node, inner, into);
      }
      return;
    }
    if (TEMPLATE_TAGS[tag]) return;

    var again = reuse(node, scope);
    if (again) {
      // Style can change between redraws — selection borders live there —
      // while the picture itself does not.
      if (node.hasAttribute("style")) {
        again.setAttribute("style", fill(node.getAttribute("style"), scope));
      }
      into.appendChild(again);
      return;
    }

    // localName, never tagName. createElementNS is case-sensitive, and
    // tagName gives "DIV" for parsed HTML — which makes an element the
    // browser's own stylesheet does not recognise, so `div { display:block }`
    // never applies and every box on the page silently becomes inline. It
    // looks like a layout mistake and is really a spelling one. localName
    // also keeps SVG's own casing, so linearGradient survives.
    var el = document.createElementNS(node.namespaceURI ||
      "http://www.w3.org/1999/xhtml", node.localName);
    for (var a = 0; a < node.attributes.length; a++) {
      attribute(el, node.attributes[a].name, node.attributes[a].value, scope);
    }
    children(node, scope, el);
    into.appendChild(el);
  }

  /* ----------------------------------------------------------------- mount */

  function parse(html) {
    var doc = new DOMParser().parseFromString(
      "<body>" + html + "</body>", "text/html");
    return doc.body;
  }

  /* Render `html` into `where`, reading names from `scope`.
   *
   * The whole tree is rebuilt each time. For a page of this size that is
   * well under a frame, and it removes the entire class of bug where the
   * screen and the data disagree because an update was missed — which is
   * exactly the kind of bug that costs an evening to find and looks, on
   * screen, like the tool being wrong about a video. */
  function render(where, html, scope) {
    var source = typeof html === "string" ? parse(html) : html;
    var made = document.createDocumentFragment();
    children(source, scope || {}, made);
    var focused = document.activeElement;
    var mark = focused && where.contains(focused)
      ? focused.getAttribute("data-keep") : null;

    // Where every scrollable region was. A page that jumps back to the top
    // once a second cannot be read while it is working, which is exactly
    // when there is something worth reading on it.
    var places = {};
    where.querySelectorAll("[data-scroll]").forEach(function (el) {
      places[el.getAttribute("data-scroll")] = el.scrollTop;
    });

    where.textContent = "";
    where.appendChild(made);

    Object.keys(places).forEach(function (name) {
      var back = where.querySelector('[data-scroll="' + name + '"]');
      if (back) back.scrollTop = places[name];
    });
    if (mark) {
      var box = where.querySelector('[data-keep="' + mark + '"]');
      if (box && box.focus) box.focus();         // typing survives a redraw
    }
    // Anything not on screen any more is dropped, so the cache cannot grow
    // for the length of a session.
    Object.keys(media).forEach(function (src) {
      if (!kept[src]) delete media[src];
    });
    kept = {};
  }

  /* Pull one design file apart: its styles, and its markup. */
  function unwrap(text) {
    var doc = new DOMParser().parseFromString(text, "text/html");
    var root = doc.querySelector("x-dc") || doc.body;
    var styles = [];
    root.querySelectorAll("helmet style, style").forEach(function (el) {
      styles.push(el.textContent);
      el.parentNode.removeChild(el);
    });
    root.querySelectorAll("helmet").forEach(function (el) {
      el.parentNode.removeChild(el);
    });
    return { styles: styles.join("\n"), markup: root.innerHTML };
  }

  global.DCX = { render: render, unwrap: unwrap, value: value, fill: fill,
                 parse: parse };
})(window);

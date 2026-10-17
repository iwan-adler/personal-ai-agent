// Koda web UI behaviour. Served from /static (the CSP allows only 'self' scripts).
// Every text that reaches the page from here goes through textContent (never an
// HTML string), so nothing typed or received can become markup.
(function () {
  "use strict";

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function scrollDown() {
    var log = document.getElementById("log");
    if (log) log.scrollTop = log.scrollHeight;
  }

  function hideWelcome() {
    var welcome = document.getElementById("welcome");
    if (welcome) welcome.classList.add("hidden");
  }

  function grow(box) {
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 200) + "px";
  }

  // While Koda thinks: your message at once, then a bubble with a timer.
  var pending = null;

  function showPending(text) {
    var log = document.getElementById("log");
    var mine = el("div", "msg user pending-local", text);
    var row = el("div", "row assistant pending-local");
    row.appendChild(el("div", "avatar", "K"));
    var bubble = el("div", "msg assistant thinking");
    var dots = el("span", "typing");
    dots.appendChild(el("i")); dots.appendChild(el("i")); dots.appendChild(el("i"));
    var label = el("span", "", "Thinking…");
    bubble.appendChild(dots); bubble.appendChild(label);
    row.appendChild(bubble);
    log.appendChild(mine); log.appendChild(row);
    var started = Date.now();
    var timer = setInterval(function () {
      var s = Math.round((Date.now() - started) / 1000);
      label.textContent = s < 15 ? "Thinking… " + s + " s" : "Still thinking… " + s + " s (local models can be slow)";
    }, 1000);
    pending = { timer: timer, nodes: [mine, row] };
    hideWelcome();
    scrollDown();
  }

  function clearPending() {
    if (!pending) return;
    clearInterval(pending.timer);
    pending.nodes.forEach(function (n) { n.remove(); });
    pending = null;
  }

  function showError(text) {
    var log = document.getElementById("log");
    var row = el("div", "row assistant");
    row.appendChild(el("div", "avatar", "!"));
    row.appendChild(el("div", "msg error", text));
    log.appendChild(row);
    scrollDown();
  }

  document.addEventListener("htmx:beforeRequest", function (e) {
    var form = e.detail.elt;
    if (form && form.id === "composer") {
      var box = document.getElementById("message");
      showPending(box.value.trim());
      box.value = "";
      grow(box);
    }
  });

  document.addEventListener("htmx:beforeSwap", function () { clearPending(); });

  document.addEventListener("htmx:afterSettle", function () {
    hideWelcome();
    scrollDown();
    var box = document.getElementById("message");
    if (box) box.focus();
  });

  // htmx does not show error responses: say what went wrong instead of doing nothing.
  document.addEventListener("htmx:responseError", function (e) {
    clearPending();
    var xhr = e.detail.xhr;
    var detail = (xhr && xhr.responseText || "").slice(0, 300);
    if (xhr && xhr.status === 403) {
      showError("Koda did not accept this (" + detail + "). If Koda was restarted, close this window and open Koda again.");
    } else {
      showError("Something went wrong (" + (xhr ? xhr.status : "?") + "): " + detail);
    }
  });

  document.addEventListener("htmx:sendError", function () {
    clearPending();
    showError("Koda is not reachable. It may have been closed: open Koda again from its icon.");
  });

  document.addEventListener("htmx:afterRequest", function (e) {
    // "New chat" answers with HX-Refresh; nothing else to do here.
    if (e.detail.failed) clearPending();
  });

  document.addEventListener("keydown", function (e) {
    if (e.target && e.target.id === "message" && e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      var form = document.getElementById("composer");
      if (e.target.value.trim() && !form.querySelector("button").disabled) form.requestSubmit();
    }
  });

  document.addEventListener("input", function (e) {
    if (e.target && e.target.id === "message") grow(e.target);
  });

  document.addEventListener("click", function (e) {
    var chip = e.target.closest && e.target.closest(".chip");
    if (!chip) return;
    var box = document.getElementById("message");
    box.value = chip.getAttribute("data-prompt") || "";
    grow(box);
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
  });

  document.addEventListener("DOMContentLoaded", function () {
    // Dashboard bars: widths come from data attributes (no inline styles under the CSP).
    document.querySelectorAll("[data-fill]").forEach(function (bar) {
      var value = Math.max(0, Math.min(100, parseInt(bar.getAttribute("data-fill"), 10) || 0));
      bar.style.width = value + "%";
    });
    scrollDown();
  });
})();

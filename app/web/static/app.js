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

  var toastTimer = null;
  function toast(text, kind) {
    var box = document.getElementById("toast");
    if (!box) return;
    box.textContent = text;
    box.className = "toast show " + (kind || "ok");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { box.className = "toast"; }, 3200);
  }
  window.kodaToast = toast;

  // Code blocks in answers get a copy button (added here: templates have no scripts).
  function addCopyButtons(root) {
    (root || document).querySelectorAll(".md .code").forEach(function (block) {
      if (block.querySelector(".copy")) return;
      var button = el("button", "copy", "Copy");
      button.type = "button";
      button.addEventListener("click", function () {
        var code = block.querySelector("code");
        navigator.clipboard.writeText(code ? code.textContent : "").then(function () {
          button.textContent = "Copied";
          setTimeout(function () { button.textContent = "Copy"; }, 1500);
        }, function () { toast("Could not copy.", "error"); });
      });
      block.appendChild(button);
    });
  }

  // Only the newest answer keeps its follow-up buttons.
  function keepLastFollowups() {
    var all = document.querySelectorAll("#log .followups");
    for (var i = 0; i < all.length - 1; i++) all[i].remove();
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
    if (!log) return;
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
    if (!text) mine.remove();
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
    if (form && form.getAttribute && form.getAttribute("data-echo") !== null) {
      showPending(form.getAttribute("data-echo"));
      return;
    }
    if (form && form.getAttribute && form.getAttribute("data-busy-label")) {
      form.setAttribute("data-label", form.textContent);
      form.textContent = form.getAttribute("data-busy-label");
      form.disabled = true;
      return;
    }
    if (form && form.getAttribute && form.getAttribute("data-busy")) {
      var submit = form.querySelector("button[type=submit]");
      if (submit) { submit.setAttribute("data-label", submit.textContent); submit.textContent = form.getAttribute("data-busy"); }
      return;
    }
    if (form && form.id === "composer") {
      document.querySelectorAll("#log .followups").forEach(function (n) { n.remove(); });
      var box = document.getElementById("message");
      showPending(box.value.trim());
      box.value = "";
      grow(box);
    }
  });

  document.addEventListener("htmx:beforeSwap", function () { clearPending(); });

  document.addEventListener("htmx:afterSettle", function (e) {
    if (e.detail && e.detail.target && e.detail.target.id === "review") {
      e.detail.target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    if (document.querySelector("#log .msg")) hideWelcome();
    applySizes();
    keepLastFollowups();
    addCopyButtons();
    scrollDown();
    var box = document.getElementById("message");
    if (box && document.activeElement === document.body) box.focus();
  });

  // htmx does not show error responses: say what went wrong instead of doing nothing.
  document.addEventListener("htmx:responseError", function (e) {
    clearPending();
    var xhr = e.detail.xhr;
    var detail = (xhr && xhr.responseText || "").slice(0, 300);
    if (!document.getElementById("log")) {
      toast(detail || "Something went wrong.", "error");
    } else if (xhr && xhr.status === 403) {
      showError("Koda did not accept this (" + detail + "). If Koda was restarted, close this window and open Koda again.");
    } else {
      showError("Something went wrong (" + (xhr ? xhr.status : "?") + "): " + detail);
    }
  });

  document.addEventListener("htmx:sendError", function () {
    clearPending();
    if (!document.getElementById("log")) { toast("Koda is not reachable. Open Koda again from its icon.", "error"); return; }
    showError("Koda is not reachable. It may have been closed: open Koda again from its icon.");
  });

  document.addEventListener("htmx:afterRequest", function (e) {
    if (e.detail.failed) clearPending();
    var elt = e.detail.elt;
    if (elt && elt.getAttribute && elt.getAttribute("data-busy-label") && elt.getAttribute("data-label")) {
      elt.textContent = elt.getAttribute("data-label");
      elt.disabled = false;
    }
    if (elt && elt.querySelector) {
      var submit = elt.querySelector("button[type=submit][data-label]");
      if (submit) submit.textContent = submit.getAttribute("data-label");
    }
    if (elt && elt.id === "upload-form") {
      var bar = document.getElementById("upload-progress");
      if (bar) bar.hidden = true;
      if (e.detail.successful) {
        elt.reset();
        var names = document.getElementById("file-names");
        if (names) names.textContent = names.getAttribute("data-default") || names.textContent;
      }
    }
  });

  // Uploads: show a progress bar while the files are sent.
  document.addEventListener("htmx:xhr:progress", function (e) {
    if (!e.detail.lengthComputable || !e.target || e.target.id !== "upload-form") return;
    var bar = document.getElementById("upload-progress");
    if (!bar) return;
    bar.hidden = false;
    bar.querySelector("span").style.width = Math.round(100 * e.detail.loaded / e.detail.total) + "%";
  });

  // The drop zone: highlight while dragging, list the chosen files.
  document.addEventListener("dragover", function (e) {
    var zone = e.target.closest && e.target.closest("#dropzone");
    if (zone) zone.classList.add("dragging");
  });
  ["dragleave", "drop"].forEach(function (type) {
    document.addEventListener(type, function (e) {
      var zone = e.target.closest && e.target.closest("#dropzone");
      if (zone) zone.classList.remove("dragging");
    });
  });
  document.addEventListener("change", function (e) {
    if (!e.target || e.target.id !== "file-input") return;
    var names = document.getElementById("file-names");
    if (!names) return;
    if (!names.getAttribute("data-default")) names.setAttribute("data-default", names.textContent);
    var files = Array.prototype.map.call(e.target.files, function (f) { return f.name; });
    names.textContent = files.length ? files.join(", ") : names.getAttribute("data-default");
  });

  // Close buttons on review panels.
  document.addEventListener("click", function (e) {
    var close = e.target.closest && e.target.closest("[data-close]");
    if (!close) return;
    var panel = document.querySelector(close.getAttribute("data-close"));
    if (panel) panel.remove();
  });

  // Score ring and bars: sizes come from data attributes (no inline styles under the CSP).
  function applySizes() {
    document.querySelectorAll("[data-fill]").forEach(function (bar) {
      var value = Math.max(0, Math.min(100, parseInt(bar.getAttribute("data-fill"), 10) || 0));
      bar.style.width = value + "%";
    });
    document.querySelectorAll("[data-percent]").forEach(function (ring) {
      var value = Math.max(0, Math.min(100, parseInt(ring.getAttribute("data-percent"), 10) || 0));
      ring.style.setProperty("--p", value);
    });
  }

  // Flashcards: Space shows the answer, 1-4 grade it.
  function revealAnswer() {
    var answer = document.getElementById("answer");
    var button = document.getElementById("reveal");
    if (!answer || !answer.hidden) return false;
    answer.hidden = false;
    if (button) button.remove();
    return true;
  }

  document.addEventListener("click", function (e) {
    if (e.target && e.target.closest && e.target.closest("#reveal")) revealAnswer();
  });

  document.addEventListener("keydown", function (e) {
    if (!document.getElementById("flashcard") || e.ctrlKey || e.altKey || e.metaKey) return;
    var tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if ((e.key === " " || e.key === "Enter") && revealAnswer()) { e.preventDefault(); return; }
    var answer = document.getElementById("answer");
    if (answer && !answer.hidden && /^[1-4]$/.test(e.key)) {
      var button = document.querySelector('.grades button[data-key="' + e.key + '"]');
      if (button && !button.disabled) { e.preventDefault(); button.click(); }
    }
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
    var chip = e.target.closest && e.target.closest(".chip[data-prompt]");
    if (!chip) return;
    var box = document.getElementById(chip.getAttribute("data-target") || "message");
    if (!box) return;
    box.value = chip.getAttribute("data-prompt") || "";
    if (chip.classList.contains("send-now")) {
      var form = box.form;
      if (form && !form.querySelector("button[type=submit]").disabled) form.requestSubmit();
      return;
    }
    grow(box);
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
  });

  document.addEventListener("DOMContentLoaded", function () {
    applySizes();
    addCopyButtons();
    keepLastFollowups();
    scrollDown();
    var flash = document.body.getAttribute("data-flash");
    if (flash) toast(flash, "ok");
  });

  // Server responses can ask for a toast with an HX-Trigger header: {"toast": "Saved."}
  document.addEventListener("toast", function (e) {
    var d = e.detail || {};
    toast(d.value || d.message || "Done.", d.kind || "ok");
  });
})();

// shuto helpers (CSP-safe, no inline handlers).
// - j/k = next/previous photo, a = approve
// - live keyword count, max 2 categories
// - bulk select: [data-select-all] / [data-select-none] toggle input[name=ids]; [data-select-count] shows the count
// - dialogs: [data-open-dialog="id"], [data-close-dialog], [data-pick-path="/some/dir"] fills input[name=path]
// - updates: [data-update-wait] polls /healthz and reloads when shuto is back; the banner
//   [data-dismiss-update] hides itself (and remembers the version)
// - after htmx swaps, focus moves to the first heading in the swapped content
(function () {
  "use strict";

  function typing(el) {
    return el && ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
  }

  function boxes() {
    return Array.prototype.slice.call(document.querySelectorAll("input[name=ids]"));
  }

  function updateCount() {
    var n = boxes().filter(function (b) { return b.checked; }).length;
    document.querySelectorAll("[data-select-count]").forEach(function (el) {
      var tpl = el.getAttribute("data-select-template") || "{n} selected";
      el.textContent = tpl.replace("{n}", n);
    });
    document.querySelectorAll("[data-needs-selection]").forEach(function (el) {
      el.disabled = n === 0;
    });
    boxes().forEach(function (b) {
      var card = b.closest(".card");
      if (card) card.classList.toggle("selected", b.checked);
    });
  }

  document.addEventListener("keydown", function (ev) {
    if (typing(ev.target) || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    var link = null;
    if (ev.key === "j") link = document.getElementById("next-link");
    if (ev.key === "k") link = document.getElementById("prev-link");
    if (link) {
      window.location.href = link.href;
    } else if (ev.key === "a") {
      var form = document.getElementById("approve-form");
      if (form) form.submit();
    }
  });

  document.addEventListener("click", function (ev) {
    var t = ev.target.closest ? ev.target.closest("[data-select-all],[data-select-none],[data-open-dialog],[data-close-dialog],[data-pick-path]") : null;
    if (!t) return;
    if (t.hasAttribute("data-select-all") || t.hasAttribute("data-select-none")) {
      var on = t.hasAttribute("data-select-all");
      boxes().forEach(function (b) { b.checked = on; });
      updateCount();
    } else if (t.hasAttribute("data-open-dialog")) {
      var d = document.getElementById(t.getAttribute("data-open-dialog"));
      if (d && d.showModal) d.showModal();
    } else if (t.hasAttribute("data-close-dialog")) {
      var dlg = t.closest("dialog");
      if (dlg) dlg.close();
    } else if (t.hasAttribute("data-pick-path")) {
      var input = document.querySelector("input[name=path]");
      if (input) {
        input.value = t.getAttribute("data-pick-path");
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      var parent = t.closest("dialog");
      if (parent) parent.close();
      if (input) input.focus();
    }
  });

  document.addEventListener("change", function (ev) {
    if (ev.target.name === "ids") updateCount();
  });

  document.addEventListener("htmx:afterSwap", function (ev) {
    var root = ev.detail && ev.detail.target;
    if (!root) return;
    var h = root.querySelector("h1,h2,h3");
    if (h) {
      if (!h.hasAttribute("tabindex")) h.setAttribute("tabindex", "-1");
      h.focus();
    }
    updateCount();
  });


  var DISMISS_KEY = "shuto-update-dismissed";

  function hideDismissedBanner() {
    var banner = document.querySelector(".update-banner[data-update-version]");
    if (!banner) return;
    try {
      if (window.localStorage.getItem(DISMISS_KEY) === banner.getAttribute("data-update-version")) {
        banner.remove();
      }
    } catch (e) { /* storage blocked: show the banner */ }
  }

  document.addEventListener("click", function (ev) {
    var t = ev.target.closest ? ev.target.closest("[data-dismiss-update]") : null;
    if (!t) return;
    var banner = t.closest(".update-banner");
    if (banner && banner.hasAttribute("data-update-version")) {
      try {
        window.localStorage.setItem(DISMISS_KEY, banner.getAttribute("data-update-version"));
      } catch (e) { /* storage blocked: just hide it now */ }
    }
    if (banner) banner.remove();
  });

  var waiting = false;
  function startUpdateWait() {
    var box = document.querySelector("[data-update-wait]");
    if (!box || waiting) return;
    waiting = true;
    var began = Date.now();
    var wasDown = false;
    var slow = box.querySelector("[data-update-slow]");
    window.setInterval(function () {
      var elapsed = Date.now() - began;
      if (slow && elapsed > 10 * 60 * 1000) slow.hidden = false;
      fetch("/healthz", { cache: "no-store" }).then(function (r) {
        if (!r.ok) throw new Error("not ready");
        if (wasDown || elapsed > 20000) {
          window.location.href = "/settings?r=" + Date.now() + "#updates";
        }
      }).catch(function () { wasDown = true; });
    }, 2000);
  }

  document.addEventListener("htmx:afterSwap", function () {
    hideDismissedBanner();
    startUpdateWait();
  });

  document.addEventListener("DOMContentLoaded", function () {
    var kw = document.getElementById("keywords");
    var count = document.getElementById("kw-count");
    if (kw && count) {
      kw.addEventListener("input", function () {
        count.textContent = kw.value.split(/[,\n]+/).filter(function (s) {
          return s.trim();
        }).length;
      });
    }
    document.querySelectorAll("input.cat").forEach(function (box) {
      box.addEventListener("change", function () {
        var checked = document.querySelectorAll("input.cat:checked");
        if (checked.length > 2) box.checked = false;
      });
    });
    updateCount();
  });
})();

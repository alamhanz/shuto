// Small helpers: j/k = next/previous photo, a = approve, live keyword count, max 2 categories.
(function () {
  "use strict";

  function typing(el) {
    return el && ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
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
  });
})();

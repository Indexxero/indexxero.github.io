/* measure.indexxero.com theme: the System / Light / Dark choice and the fade up as sections scroll in.
   Built by build_theme.py. Same origin only; the choice is kept in this browser's storage, nowhere else. */
(function () {
  var KEY = "ix-theme", root = document.documentElement;
  function stored() { try { return localStorage.getItem(KEY) || "system"; } catch (e) { return "system"; } }
  function apply(mode) {
    if (mode === "light" || mode === "dark") root.setAttribute("data-theme", mode); else root.removeAttribute("data-theme");
    var bs = document.querySelectorAll(".ix-toggle button[data-mode]");
    for (var i = 0; i < bs.length; i++) bs[i].setAttribute("aria-pressed", bs[i].getAttribute("data-mode") === mode ? "true" : "false");
  }
  apply(stored());
  document.addEventListener("DOMContentLoaded", function () {
    apply(stored());
    var bs = document.querySelectorAll(".ix-toggle button[data-mode]");
    for (var i = 0; i < bs.length; i++) bs[i].addEventListener("click", function () {
      var m = this.getAttribute("data-mode");
      try { localStorage.setItem(KEY, m); } catch (e) {}
      apply(m);
      document.dispatchEvent(new CustomEvent("ix-theme", { detail: m }));   /* pages that draw charts redraw on it */
    });
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (still || !("IntersectionObserver" in window)) return;
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("ix-in"); io.unobserve(e.target); } });
    }, { rootMargin: "0px 0px -40px 0px" });
    var ss = document.querySelectorAll("[data-ix-reveal]");
    for (var j = 0; j < ss.length; j++) {
      var r = ss[j].getBoundingClientRect();
      if (r.top < window.innerHeight) continue;          /* already on screen: no fade, nothing hidden */
      ss[j].classList.add("ix-reveal"); io.observe(ss[j]);
    }
  });
})();

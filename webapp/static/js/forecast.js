(function () {
  const tabBtns = document.querySelectorAll(".tab-btn");
  const panels = document.querySelectorAll(".tab-panel");

  tabBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      tabBtns.forEach((b) => b.classList.remove("active"));
      panels.forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    });
  });

  const detailedCheckbox = document.getElementById("detailed");
  const tickerForm = document.getElementById("tickerForm");
  if (detailedCheckbox && tickerForm) {
    const updateLoadingMessage = () => {
      tickerForm.dataset.loadingMessage = detailedCheckbox.checked
        ? "Running detailed forecast — several sampled paths, this takes noticeably longer…"
        : "Running forecast…";
    };
    detailedCheckbox.addEventListener("change", updateLoadingMessage);
    updateLoadingMessage();
  }

  const fileInput = document.getElementById("fileInput");
  const dropZone = document.getElementById("dropZone");
  const fileLabel = document.getElementById("fileLabel");

  if (fileInput && dropZone) {
    fileInput.addEventListener("change", () => {
      if (fileInput.files.length) {
        fileLabel.innerHTML = "Selected: <strong>" + fileInput.files[0].name + "</strong>";
      }
    });
    ["dragenter", "dragover"].forEach((evt) =>
      dropZone.addEventListener(evt, (e) => {
        e.preventDefault();
        dropZone.classList.add("drag");
      })
    );
    ["dragleave", "drop"].forEach((evt) =>
      dropZone.addEventListener(evt, (e) => {
        e.preventDefault();
        dropZone.classList.remove("drag");
      })
    );
    dropZone.addEventListener("drop", (e) => {
      if (e.dataTransfer.files.length) {
        fileInput.files = e.dataTransfer.files;
        fileLabel.innerHTML = "Selected: <strong>" + e.dataTransfer.files[0].name + "</strong>";
      }
    });
  }

  // ---------------------------------------------------------------------
  // Background job submission + polling for both forms. Both
  // /forecast/ticker and /forecast/csv now return {"job_id": "..."}
  // immediately instead of blocking the request for the full Kronos
  // inference time, so the page stays responsive (and the loading
  // overlay reflects real progress, not a frozen request) the same way
  // the chat page already works -- see webapp/static/js/chat.js.
  // ---------------------------------------------------------------------
  const POLL_INTERVAL_MS = 1200;

  const resultSection = document.getElementById("resultSection");
  const resultTag = document.getElementById("resultTag");
  const resultText = document.getElementById("resultText");
  const chartFrame = document.getElementById("chartFrame");

  function clearInlineError(form) {
    const existing = form.querySelector(".flash-error");
    if (existing) existing.remove();
  }

  function showInlineError(form, message) {
    clearInlineError(form);
    const el = document.createElement("div");
    el.className = "flash flash-error";
    el.textContent = message;
    form.prepend(el);
  }

  function renderContextBadges(ctx) {
    const badges = document.getElementById("contextBadges");
    const gate = document.getElementById("earningsGate");
    if (!badges) return;
    if (!ctx) {
      badges.style.display = "none";
      badges.innerHTML = "";
      if (gate) { gate.style.display = "none"; gate.innerHTML = ""; }
      return;
    }
    const labelClass = {
      bullish: "badge-bull",
      "lean bullish": "badge-bull",
      neutral: "badge-neutral",
      "lean bearish": "badge-bear",
      bearish: "badge-bear",
    }[ctx.label] || "badge-neutral";
    const riskClass = ctx.event_risk === "high" ? "badge-risk-high"
      : ctx.event_risk === "elevated" ? "badge-risk-mid" : "badge-risk-ok";
    let html = "";
    html += '<span class="ctx-badge ' + labelClass + '">Context: ' + (ctx.label || "neutral") + "</span>";
    html += '<span class="ctx-badge ' + riskClass + '">Event risk: ' + (ctx.event_risk || "normal") + "</span>";
    html += '<span class="ctx-badge badge-neutral">Context conf: ' + (ctx.confidence || "—") + "</span>";
    if (ctx.analyst_nudge_applied) {
      html += '<span class="ctx-badge badge-nudge">Analyst nudge on</span>';
    }
    if (ctx.band_widen_factor && ctx.band_widen_factor > 1) {
      html += '<span class="ctx-badge badge-neutral">Band ×' + ctx.band_widen_factor + "</span>";
    }
    badges.innerHTML = html;
    badges.style.display = "flex";

    if (gate) {
      if (ctx.earnings_gate) {
        gate.style.display = "block";
        gate.innerHTML =
          "<strong>Earnings gate active.</strong> A report is near" +
          (ctx.earnings_date ? " (" + ctx.earnings_date + ")" : "") +
          " — treat this path as a <em>pre-event scenario</em>, not an actionable target.";
      } else {
        gate.style.display = "none";
        gate.innerHTML = "";
      }
    }
  }

  function fmt(n) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    return Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function showResult(data) {
    var section = document.getElementById("resultSection");
    if (section) section.style.display = "";

    var ticker = data.ticker || "";
    if (resultTag) resultTag.textContent = ticker;
    var elTicker = document.getElementById("fcTicker");
    if (elTicker) elTicker.textContent = ticker;

    var elName = document.getElementById("fcName");
    if (elName) elName.textContent = data.name || "";

    var elSector = document.getElementById("fcSector");
    if (elSector) {
      if (data.sector) {
        elSector.textContent = data.sector;
        elSector.hidden = false;
      } else {
        elSector.hidden = true;
      }
    }

    var horizon = data.pred_len ? (data.pred_len + "D forecast") : "Forecast";
    var elHorizon = document.getElementById("fcHorizon");
    if (elHorizon) elHorizon.textContent = horizon;
    var elBadge = document.getElementById("fcChartBadge");
    if (elBadge) elBadge.textContent = horizon;
    var elTitle = document.getElementById("fcChartTitle");
    if (elTitle) {
      elTitle.textContent =
        ticker +
        " — Kronos outlook · next " +
        (data.pred_len || "—") +
        " trading days" +
        (data.n_runs && data.n_runs > 1 ? " (" + data.n_runs + " paths)" : "");
    }

    if (resultText) resultText.textContent = data.text || "";
    renderContextBadges(data.context_signal || null);

    var kpis = document.getElementById("outlookKpis");
    if (kpis) {
      var hasRange = data.range_high != null || data.range_centre != null || data.range_low != null;
      var elH = document.getElementById("kpiHigh");
      var elC = document.getElementById("kpiCentre");
      var elL = document.getElementById("kpiLow");
      var elA = document.getElementById("kpiAnalyst");
      if (elH) elH.textContent = fmt(data.range_high);
      if (elC) elC.textContent = fmt(data.range_centre);
      if (elL) elL.textContent = fmt(data.range_low);
      if (elA) elA.textContent = fmt(data.analyst_target);
      kpis.style.display = hasRange || data.analyst_target != null ? "grid" : "none";
    }

    var insights = data.insights || [];
    var list = document.getElementById("fcInsights");
    if (list) {
      if (insights.length) {
        list.innerHTML = insights
          .map(function (row) {
            var key = (row.key || "").toLowerCase();
            var icon = key === "risk" ? "!" : key === "analyst" ? "◎" : "↗";
            return (
              '<li class="fc-insight fc-insight-' +
              (key || "other") +
              '">' +
              '<span class="fc-insight-icon" aria-hidden="true">' +
              icon +
              "</span>" +
              '<div class="fc-insight-body">' +
              '<span class="fc-insight-label">' +
              (row.label || "Insight") +
              "</span>" +
              '<span class="fc-insight-text">' +
              (row.text || "") +
              "</span></div></li>"
            );
          })
          .join("");
      } else if (data.text) {
        list.innerHTML =
          '<li class="fc-insight"><div class="fc-insight-body">' +
          '<span class="fc-insight-label">Outlook</span>' +
          '<span class="fc-insight-text">' +
          data.text +
          "</span></div></li>";
      } else {
        list.innerHTML = "";
      }
    }

    var expl = document.getElementById("fcExplanation");
    if (expl) expl.textContent = data.explanation || data.text || "";

    // Chart image
    var frame = document.getElementById("chartFrame");
    var wrap = document.getElementById("fcChartWrap");
    if (data.image_url && frame) {
      frame.innerHTML =
        '<img src="' +
        data.image_url +
        '" alt="Forecast chart for ' +
        (ticker || "ticker") +
        '">';
      frame.style.display = "block";
      if (wrap) wrap.style.display = "block";
    }

    if (section) {
      try {
        section.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (e) {}
    }
  }


  function pollJob(jobId, form, submitBtn, originalBtnText) {
    fetch("/forecast/job/" + jobId)
      .then((r) => r.json())
      .then((data) => {
        if (data.status === "pending") {
          setTimeout(() => pollJob(jobId, form, submitBtn, originalBtnText), POLL_INTERVAL_MS);
          return;
        }
        window.KronosLoading && window.KronosLoading.hide();
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = originalBtnText;
        }
        if (data.status === "done") {
          showResult(data);
        } else if (data.status === "error") {
          showInlineError(form, data.message || "Something went wrong.");
        } else {
          showInlineError(form, "Lost track of that job -- please try again.");
        }
      })
      .catch(() => {
        // A transient network hiccup while polling shouldn't give up
        // immediately -- the background job is still running server-side
        // regardless of whether this particular poll succeeded.
        setTimeout(() => pollJob(jobId, form, submitBtn, originalBtnText), POLL_INTERVAL_MS);
      });
  }

  function wireBackgroundForm(form) {
    if (!form) return;
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      clearInlineError(form);

      const submitBtn = form.querySelector('button[type="submit"]');
      const originalBtnText = submitBtn ? submitBtn.textContent : null;
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "Working…";
      }
      window.KronosLoading && window.KronosLoading.show(form.dataset.loadingMessage);

      fetch(form.action, { method: "POST", body: new FormData(form) })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
          if (!ok || data.error) {
            window.KronosLoading && window.KronosLoading.hide();
            if (submitBtn) {
              submitBtn.disabled = false;
              submitBtn.textContent = originalBtnText;
            }
            showInlineError(form, data.error || "Request failed.");
            return;
          }
          pollJob(data.job_id, form, submitBtn, originalBtnText);
        })
        .catch(() => {
          window.KronosLoading && window.KronosLoading.hide();
          if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = originalBtnText;
          }
          showInlineError(form, "Couldn't reach the server -- please try again.");
        });
    });
  }

  wireBackgroundForm(tickerForm);
  wireBackgroundForm(document.getElementById("csvForm"));
})();


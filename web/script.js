/* Daily Mix dashboard renderer.
 *
 * The payload is rendered with DOM APIs and textContent only: deck names and
 * error messages are user-controlled data and must never be interpreted as
 * HTML.  Button presses are forwarded to Python over Anki's pycmd bridge.
 */
(function () {
  "use strict";

  var COLORS = [
    "#8B7CFF",
    "#2DD4BF",
    "#F472B6",
    "#FBBF24",
    "#60A5FA",
    "#34D399",
    "#FB7185",
    "#A78BFA"
  ];

  var RING_RADIUS = 56;
  var RING_LENGTH = 2 * Math.PI * RING_RADIUS;

  var ready = false;
  var pending = null;
  var current = null;
  var busy = false;
  var view = {
    canStart: false,
    label: "Start Daily Mix",
    hint: "Space or Ctrl+Enter",
    action: "start"
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(id, value) {
    var element = byId(id);
    if (element) {
      element.textContent = value === null || value === undefined ? "" : String(value);
    }
  }

  function setVisible(id, visible) {
    var element = byId(id);
    if (element) {
      element.hidden = !visible;
    }
  }

  function number(value) {
    var parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function plural(count, singular, pluralWord) {
    var word = count === 1 ? singular : pluralWord || singular + "s";
    return count + " " + word;
  }

  function formatDuration(seconds) {
    var total = Math.max(0, number(seconds));
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    if (hours > 0) {
      return hours + "h " + minutes + "m";
    }
    if (minutes > 0) {
      return minutes + "m";
    }
    return Math.max(0, Math.floor(total)) + "s";
  }

  function send(command) {
    if (typeof pycmd !== "function") {
      return false;
    }
    pycmd(command);
    return true;
  }

  function updateCta() {
    var button = byId("dm-start");
    if (!button) {
      return;
    }
    setText("dm-cta-label", busy ? "Building your mix…" : view.label);
    setText("dm-cta-hint", busy ? "Just a moment" : view.hint);
    button.disabled = busy || !view.canStart;
    button.dataset.action = view.action;
    button.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function setBusy(value) {
    busy = Boolean(value);
    updateCta();
  }

  /* -- view copy --------------------------------------------------------- */

  function buildView(data) {
    var status = data.status || "error";
    var done = number(data.done);
    var target = number(data.target);
    var planned = number(data.planned_total);
    var plannedReview = number(data.planned_review);
    var plannedNew = number(data.planned_new);
    var remaining = number(data.remaining);
    var sources = number(data.deck_count);
    var order = data.order_label || "Interleaved";
    var reset = formatDuration(data.reset_seconds);

    if (status === "done") {
      return {
        eyebrow: "Quota complete",
        title: "You're done for today",
        text:
          plural(done, "card") +
          " answered today. Your next mix arrives in " +
          reset +
          ".",
        canStart: false,
        label: "Quota complete",
        hint: "Next mix in " + reset,
        action: "start",
        banner: "",
        bannerKind: ""
      };
    }

    if (status === "error") {
      return {
        eyebrow: "Needs attention",
        title: "Let's get your mix ready",
        text:
          "Daily Mix needs a quick look before it can build today's session.",
        canStart: true,
        label: "Open settings",
        hint: "Check your deck selection",
        action: "settings",
        banner: data.error || "Daily Mix could not read your collection.",
        bannerKind: "error"
      };
    }

    if (status === "empty") {
      return {
        eyebrow: "All clear",
        title: "Nothing due right now",
        text: data.uses_all_decks
          ? "There are no due or new cards in any deck. New cards will appear here as they become available."
          : "There are no due or new cards in the decks you selected. Add a deck in Settings or check back later.",
        canStart: false,
        label: "Nothing to study",
        hint: "Check back later",
        action: "start",
        banner: "",
        bannerKind: ""
      };
    }

    if (status === "partial") {
      return {
        eyebrow: "A lighter mix",
        title: "Today's mix is ready",
        text:
          plural(planned, "card") +
          " from " +
          plural(sources, "deck") +
          " - matched to your review/new split.",
        canStart: Boolean(data.can_start) && planned > 0,
        label: "Start Daily Mix",
        hint: "Space or Ctrl+Enter",
        action: "start",
        banner:
          "Your target of " +
          plural(target, "card") +
          " has " +
          plural(remaining, "card") +
          " left. Today's mix uses " +
          plural(planned, "card") +
          " based on the cards available and your review/new split.",
        bannerKind: "notice"
      };
    }

    return {
      eyebrow: "Today's session",
      title: "Your mix is ready",
      text:
        plural(planned, "card") +
        " from " +
        plural(sources, "deck") +
        ". " +
        plural(plannedReview, "review") +
        " and " +
        plural(plannedNew, "new card") +
        ", " +
        order.toLowerCase() +
        " order.",
      canStart: Boolean(data.can_start) && planned > 0,
      label: "Start Daily Mix",
      hint: "Space or Ctrl+Enter",
      action: "start",
      banner: "",
      bannerKind: ""
    };
  }

  /* -- components -------------------------------------------------------- */

  function renderRing(data) {
    var percent = Math.max(0, Math.min(100, number(data.percent)));
    var ring = byId("dm-ring");
    if (ring) {
      ring.setAttribute("aria-valuenow", String(Math.round(percent)));
      ring.setAttribute(
        "aria-valuetext",
        number(data.done) + " of " + number(data.target) + " cards answered"
      );
    }
    var progress = byId("dm-ring-progress");
    if (progress) {
      progress.style.strokeDasharray = String(RING_LENGTH);
      progress.style.strokeDashoffset = String(
        RING_LENGTH * (1 - percent / 100)
      );
    }
    setText("dm-done", number(data.done));
    setText("dm-target", number(data.target));
  }

  function renderChips(data) {
    var chips = byId("dm-chips");
    if (!chips) {
      return;
    }
    chips.replaceChildren();

    function add(label, accent, dotColor) {
      var chip = document.createElement("span");
      chip.className = "dm-chip" + (accent ? " dm-chip--accent" : "");
      if (dotColor) {
        var dot = document.createElement("span");
        dot.className = "dm-chip-dot";
        dot.style.background = dotColor;
        chip.appendChild(dot);
      }
      chip.appendChild(document.createTextNode(label));
      chips.appendChild(chip);
    }

    if (data.status === "done") {
      add(number(data.review_percent) + "% review / " + (100 - number(data.review_percent)) + "% new");
      add(number(data.done) + " answered", true);
      return;
    }

    var plannedReview = number(data.planned_review);
    var plannedNew = number(data.planned_new);
    if (plannedReview > 0) {
      add(plural(plannedReview, "review"), false, "var(--accent)");
    }
    if (plannedNew > 0) {
      add(plural(plannedNew, "new card"), false, "var(--success)");
    }
    add(data.order_label || "Interleaved", true);
    add(
      number(data.review_percent) +
        "% review / " +
        (100 - number(data.review_percent)) +
        "% new"
    );
  }

  function renderStats(data) {
    setText("dm-stat-review", number(data.available_review));
    setText(
      "dm-stat-review-sub",
      plural(number(data.planned_review), "card") + " planned"
    );
    setText("dm-stat-new", number(data.available_new));
    setText(
      "dm-stat-new-sub",
      plural(number(data.planned_new), "card") + " planned"
    );
    setText("dm-stat-planned", number(data.planned_total));
    setText(
      "dm-stat-planned-sub",
      data.status === "done"
        ? "quota met"
        : "of " + plural(number(data.remaining), "card") + " left"
    );
    setText("dm-stat-decks", number(data.deck_count));
    setText(
      "dm-stat-decks-sub",
      data.uses_all_decks ? "All decks" : "Selected decks"
    );
  }

  function renderBadges(data) {
    var badges = byId("dm-badges");
    if (!badges) {
      return;
    }
    badges.replaceChildren();

    function add(label) {
      var badge = document.createElement("span");
      badge.className = "dm-badge";
      badge.textContent = label;
      badges.appendChild(badge);
    }

    add(data.order_label || "Interleaved");
    add(
      number(data.review_percent) +
        "% review / " +
        (100 - number(data.review_percent)) +
        "% new"
    );
    add(data.uses_all_decks ? "All decks" : plural(number(data.deck_count), "deck"));
  }

  function renderSources(data) {
    var decks = Array.isArray(data.decks) ? data.decks : [];
    var list = byId("dm-deck-list");
    var allocation = byId("dm-allocation");
    var empty = byId("dm-empty");
    if (!list || !allocation || !empty) {
      return;
    }

    list.replaceChildren();
    allocation.replaceChildren();

    var totalPlanned = number(data.planned_total);
    if (totalPlanned > 0) {
      decks.forEach(function (deck, index) {
        var planned = number(deck.planned);
        if (planned <= 0) {
          return;
        }
        var segment = document.createElement("div");
        segment.className = "dm-allocation-segment";
        segment.style.width = (planned / totalPlanned) * 100 + "%";
        segment.style.background = COLORS[index % COLORS.length];
        allocation.appendChild(segment);
      });
      setVisible("dm-allocation", true);
    } else {
      setVisible("dm-allocation", false);
    }

    decks.forEach(function (deck, index) {
      var color = COLORS[index % COLORS.length];
      var planned = number(deck.planned);
      var available = number(deck.available_total);
      var plannedReview = number(deck.planned_review);
      var plannedNew = number(deck.planned_new);

      var row = document.createElement("div");
      row.className = "dm-deck";
      row.style.setProperty("--dot", color);

      var dot = document.createElement("span");
      dot.className = "dm-deck-dot";
      dot.setAttribute("aria-hidden", "true");

      var main = document.createElement("div");
      main.className = "dm-deck-main";

      var name = document.createElement("div");
      name.className = "dm-deck-name";
      name.textContent = deck.name || "Deck";
      name.title = deck.name || "Deck";
      main.appendChild(name);

      var meta = document.createElement("div");
      meta.className = "dm-deck-meta";
      var metaParts = [
        plural(plannedReview, "review"),
        plural(plannedNew, "new card")
      ];
      if (!data.uses_all_decks) {
        var weight = number(deck.weight);
        if (Math.abs(weight - 1) > 0.001) {
          metaParts.unshift("weight " + weight.toFixed(1) + "×");
        }
      }
      meta.textContent = metaParts.join(" · ");
      main.appendChild(meta);

      var count = document.createElement("div");
      count.className = "dm-deck-count";
      var plannedLabel = document.createElement("span");
      plannedLabel.className = "dm-deck-planned";
      plannedLabel.textContent = String(planned);
      var availableLabel = document.createElement("span");
      availableLabel.className = "dm-deck-available";
      availableLabel.textContent = "of " + available;
      count.appendChild(plannedLabel);
      count.appendChild(availableLabel);

      var bar = document.createElement("div");
      bar.className = "dm-deck-bar";
      var fill = document.createElement("div");
      fill.className = "dm-deck-bar-fill";
      fill.style.width =
        (available > 0 ? Math.min(100, (planned / available) * 100) : 0) + "%";
      bar.appendChild(fill);

      row.appendChild(dot);
      row.appendChild(main);
      row.appendChild(count);
      row.appendChild(bar);
      list.appendChild(row);
    });

    if (decks.length > 0) {
      setVisible("dm-empty", false);
      return;
    }

    var message = "No due or new cards right now.";
    if (data.status === "error") {
      message = "Sources will appear once the deck selection can be read.";
    } else if (data.status === "done") {
      message = "No cards are waiting in these decks.";
    } else if (data.uses_all_decks) {
      message = "This collection has no normal decks to mix.";
    }
    empty.textContent = message;
    setVisible("dm-empty", true);
  }

  function renderFooter(data) {
    var mix = data.mix_deck || {};
    var count = number(mix.card_count);
    var status = byId("dm-footer-status");

    if (mix.exists) {
      setText(
        "dm-footer-status",
        "Mix deck " +
          (mix.name || "Daily Mix") +
          (count > 0
            ? " holds " + plural(count, "card") + "."
            : " is empty and ready.")
      );
      setText(
        "dm-cleanup",
        "Return " + plural(count, "card") + " to home decks"
      );
      setVisible("dm-cleanup", count > 0);
    } else {
      setText("dm-footer-status", "No temporary mix deck is active.");
      setVisible("dm-cleanup", false);
    }

    var shortcut = data.shortcut
      ? "Shortcut " + data.shortcut
      : "Shortcut off";
    setText("dm-version", "v" + (data.version || "") + " · " + shortcut);
  }

  function renderBanner(kind, text) {
    var banner = byId("dm-banner");
    if (!banner) {
      return;
    }
    banner.className = "dm-banner" + (kind ? " dm-banner--" + kind : "");
    banner.textContent = text || "";
    banner.hidden = !text;
  }

  function render(data) {
    current = data || {};
    busy = false;

    var status = current.status || "error";
    var app = byId("dm-app");
    if (app) {
      app.dataset.status = status;
    }

    setText("dm-date", current.date_label || "");
    setText("dm-done", number(current.done));
    setText("dm-target", number(current.target));
    renderRing(current);

    view = buildView(current);
    setText("dm-eyebrow", view.eyebrow);
    setText("dm-hero-title", view.title);
    setText("dm-hero-text", view.text);
    updateCta();

    renderChips(current);
    renderStats(current);
    renderBadges(current);
    renderSources(current);
    renderFooter(current);
    renderBanner(view.bannerKind, view.banner);

    var sourcesSub = byId("dm-sources-sub");
    if (sourcesSub) {
      if (status === "done") {
        sourcesSub.textContent = "What is still waiting after today's quota.";
      } else if (status === "error") {
        sourcesSub.textContent = "A readable deck selection is needed first.";
      } else if (current.uses_all_decks) {
        sourcesSub.textContent =
          "All normal decks are included, weighted by available work.";
      } else {
        sourcesSub.textContent = "Built from the decks you selected.";
      }
    }
  }

  /* -- events ------------------------------------------------------------ */

  function primaryAction() {
    if (busy) {
      return;
    }
    if (view.action === "settings") {
      send("dailyMix:settings");
      return;
    }
    if (!current || !current.can_start) {
      return;
    }
    setBusy(true);
    if (!send("dailyMix:start")) {
      setBusy(false);
    }
  }

  function bindEvents() {
    var start = byId("dm-start");
    if (start) {
      start.addEventListener("click", primaryAction);
    }

    var refresh = byId("dm-refresh");
    if (refresh) {
      refresh.addEventListener("click", function () {
        send("dailyMix:refresh");
      });
    }

    var settings = byId("dm-settings");
    if (settings) {
      settings.addEventListener("click", function () {
        send("dailyMix:settings");
      });
    }

    var cleanup = byId("dm-cleanup");
    if (cleanup) {
      cleanup.addEventListener("click", function () {
        send("dailyMix:cleanup");
      });
    }

    document.addEventListener("keydown", function (event) {
      if (event.defaultPrevented || event.repeat) {
        return;
      }
      if (event.altKey || event.metaKey) {
        return;
      }

      var target = event.target;
      var tag = target && target.tagName ? target.tagName.toLowerCase() : "";
      if (
        tag === "input" ||
        tag === "textarea" ||
        tag === "select" ||
        (target && target.isContentEditable)
      ) {
        return;
      }

      var space = event.code === "Space" || event.key === " ";
      var ctrlEnter = event.ctrlKey && event.key === "Enter";

      // Space on a focused button or link keeps its normal meaning.  The
      // document-level shortcut only applies when nothing interactive is
      // focused, which is the state the dashboard opens in.
      if (space && (tag === "button" || tag === "a")) {
        return;
      }
      if (!space && !ctrlEnter) {
        return;
      }

      event.preventDefault();
      primaryAction();
    });
  }

  function parseInitial() {
    var holder = byId("dm-data");
    if (!holder) {
      return null;
    }
    try {
      return JSON.parse(holder.textContent || "{}");
    } catch (error) {
      if (window.console && console.error) {
        console.error("Daily Mix: could not parse dashboard data", error);
      }
      return null;
    }
  }

  function showLoadFailure() {
    setText("dm-eyebrow", "Needs attention");
    setText("dm-hero-title", "Could not load today's mix");
    setText(
      "dm-hero-text",
      "Close and reopen the dashboard, or press refresh. If this keeps happening, reinstall the add-on."
    );
    view = {
      canStart: false,
      label: "Nothing to study",
      hint: "Reopen the dashboard",
      action: "start"
    };
    updateCta();
  }

  function init() {
    ready = true;
    bindEvents();
    var initial = pending || parseInitial();
    if (initial) {
      render(initial);
    } else {
      showLoadFailure();
    }
  }

  window.dailyMix = {
    render: function (data) {
      pending = data;
      current = data;
      if (!ready) {
        return;
      }
      render(data);
    },
    setBusy: setBusy
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();

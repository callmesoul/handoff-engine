window.__ModuleLoader__.load({
  id: "dsh-handoff-panel",
  factory: function (require) {
    var module = { exports: {} };
    var exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });

    var React = require("react");
    var h = React.createElement;
    var useState = React.useState;
    var useEffect = React.useEffect;
    var useCallback = React.useCallback;

    var PANEL_ID = "handoff-panel";
    var PANEL_LABEL = "任务面板";
    var DEFAULT_URL = "http://127.0.0.1:8790";
    var STATUS_URL = "/handoff-panel/status";
    var LAUNCH_URL = "/handoff-panel/launch";

    /** Run one registration step; a failure must not take the others down. */
    function step(label, run) {
      try {
        return run();
      } catch (error) {
        console.error("[handoff-panel] " + label + " failed", error);
        return undefined;
      }
    }

    var CSS = [
      ".hp-root{display:flex;flex-direction:column;flex:1 1 0;min-width:0;min-height:0;",
      "background:var(--dsw-alias-bg-base,#fff);color:var(--dsw-alias-fg-base,#14161a);}",
      ".hp-bar{display:flex;align-items:center;gap:8px;padding:8px 12px;font-size:12px;",
      "line-height:1.4;color:var(--dsw-alias-fg-muted,#5b6270);",
      "border-bottom:1px solid var(--dsw-alias-border-base,#e4e7ec);flex:0 0 auto;}",
      ".hp-bar strong{color:var(--dsw-alias-fg-base,#14161a);font-weight:600;}",
      ".hp-dot{width:7px;height:7px;border-radius:50%;flex:0 0 auto;}",
      ".hp-dot.on{background:#22a06b;}.hp-dot.off{background:#d4380d;}",
      ".hp-spacer{flex:1 1 auto;}",
      ".hp-btn{font:inherit;font-size:12px;line-height:1;padding:5px 10px;border-radius:6px;",
      "border:1px solid var(--dsw-alias-border-base,#d0d5dd);cursor:pointer;",
      "background:var(--dsw-alias-bg-elevated,#fff);color:inherit;}",
      ".hp-btn:disabled{opacity:.55;cursor:default;}",
      ".hp-frame-wrap{position:relative;flex:1 1 0;min-height:0;display:flex;}",
      ".hp-frame{flex:1 1 0;min-height:0;width:100%;border:0;background:#fff;}",
      ".hp-empty{flex:1 1 0;display:flex;flex-direction:column;align-items:center;",
      "justify-content:center;gap:12px;padding:24px;text-align:center;font-size:13px;}",
      ".hp-empty p{margin:0;max-width:420px;line-height:1.6;}",
      ".hp-code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;",
      "background:var(--dsw-alias-bg-subtle,#f4f5f7);padding:2px 6px;border-radius:4px;}",
      ".hp-err{color:#d4380d;}",
    ].join("");

    function injectCss() {
      if (document.querySelector('style[data-plugin-css="dsh-handoff-panel"]')) return;
      var style = document.createElement("style");
      style.dataset.plugin = "dsh-handoff-panel";
      style.dataset.pluginCss = "dsh-handoff-panel";
      style.textContent = CSS;
      document.head.appendChild(style);
    }

    /**
     * 16px sidebar glyph: a terminal window (`>_` in a frame).
     *
     * Deliberately not a document/page shape — 交接板 already uses a page with
     * an arrow, and 任务面板 sits right below it, so a second page glyph reads
     * as a duplicate at 16px.
     */
    function PanelIcon(props) {
      var size = (props && props.size) || 16;
      return h(
        "svg",
        {
          width: size,
          height: size,
          viewBox: "0 0 24 24",
          fill: "none",
          stroke: "currentColor",
          strokeWidth: 1.8,
          strokeLinecap: "round",
          strokeLinejoin: "round",
          "aria-hidden": "true",
          focusable: "false",
        },
        h("rect", { x: 2.75, y: 4.25, width: 18.5, height: 15.5, rx: 2.75 }),
        h("path", { d: "M6.9 9.9l3.4 2.9-3.4 2.9" }),
        h("path", { d: "M12.8 15.7h4.4" }),
      );
    }

    function StatusBar(props) {
      var status = props.status;
      var reachable = Boolean(status && status.reachable);
      var label = reachable ? "面板在线" : "面板未启动";
      return h(
        "div",
        { className: "hp-bar" },
        h("span", { className: "hp-dot " + (reachable ? "on" : "off") }),
        h("strong", null, label),
        h("span", null, props.url),
        reachable && status && status.mode
          ? h("span", null, "· 模式 " + status.mode)
          : null,
        !reachable && status && status.error
          ? h("span", { className: "hp-err" }, "· " + status.error)
          : null,
        h("span", { className: "hp-spacer" }),
        h(
          "button",
          {
            type: "button",
            className: "hp-btn",
            disabled: props.busy,
            onClick: props.onLaunch,
          },
          props.busy ? "启动中…" : reachable ? "重新载入" : "启动面板",
        ),
      );
    }

    function EmptyState(props) {
      return h(
        "div",
        { className: "hp-empty" },
        h("p", null, "未连上任务面板。面板需要先在本机启动："),
        h(
          "p",
          null,
          h("span", { className: "hp-code" }, "handoffctl --root $HANDOFF_ROOT panel"),
        ),
        h(
          "button",
          {
            type: "button",
            className: "hp-btn",
            disabled: props.busy,
            onClick: props.onLaunch,
          },
          props.busy ? "启动中…" : "帮我启动",
        ),
      );
    }

    function Panel() {
      var statusState = useState(null);
      var status = statusState[0];
      var setStatus = statusState[1];
      var busyState = useState(false);
      var busy = busyState[0];
      var setBusy = busyState[1];
      var nonceState = useState(0);
      var nonce = nonceState[0];
      var setNonce = nonceState[1];

      useEffect(function () {
        injectCss();
      }, []);

      var check = useCallback(function () {
        return fetch(STATUS_URL, { headers: { accept: "application/json" } })
          .then(function (response) {
            if (!response.ok) throw new Error("HTTP " + response.status);
            return response.json();
          })
          .then(function (body) {
            setStatus(body);
            return body;
          })
          .catch(function (error) {
            var failed = {
              ok: false,
              url: DEFAULT_URL,
              reachable: false,
              error: (error && error.message) || String(error),
            };
            setStatus(failed);
            return failed;
          });
      }, []);

      useEffect(function () {
        var alive = true;
        var tick = function () {
          check();
        };
        tick();
        var timer = setInterval(tick, 5000);
        return function () {
          alive = false;
          clearInterval(timer);
        };
      }, [check]);

      var launch = useCallback(
        function () {
          setBusy(true);
          fetch(LAUNCH_URL, { method: "POST", headers: { accept: "application/json" } })
            .catch(function () {})
            .then(function () {
              var deadline = Date.now() + 12000;
              var poll = function () {
                return check().then(function (body) {
                  if (body && body.reachable) return body;
                  if (Date.now() > deadline) return null;
                  return new Promise(function (resolve) {
                    setTimeout(resolve, 700);
                  }).then(poll);
                });
              };
              return poll();
            })
            .then(function (body) {
              if (body && body.reachable) setNonce(function (n) { return n + 1; });
              setBusy(false);
            });
        },
        [check],
      );

      var url = (status && status.url) || DEFAULT_URL;
      var reachable = Boolean(status && status.reachable);

      return h(
        "div",
        { className: "hp-root" },
        h(StatusBar, { status: status, url: url, busy: busy, onLaunch: launch }),
        reachable
          ? h(
              "div",
              { className: "hp-frame-wrap" },
              h("iframe", {
                key: "frame-" + nonce,
                className: "hp-frame",
                src: url.replace(/\/+$/, "") + "/",
                title: PANEL_LABEL,
                referrerPolicy: "no-referrer",
              }),
            )
          : h(EmptyState, { busy: busy, onLaunch: launch }),
      );
    }

    function apply(ctx) {
      step("stylesheet injection", injectCss);
      step("sidebar panel entry", function () {
        ctx.slots.inject("sidebar.panellist", function () {
          return ctx.slots.register(
            {
              name: "sidebar.panellist",
              id: PANEL_ID,
              order: 25,
              label: PANEL_LABEL,
            },
            PanelIcon,
          );
        });
      });
      step("main panel", function () {
        ctx.slots.inject("main", function () {
          return ctx.slots.register({ name: "main", key: PANEL_ID }, Panel);
        });
      });
    }

    exports.apply = apply;
    exports.inject = ["slots"];
    exports.name = "dsh-handoff-panel-client";

    return module.exports;
  },
});

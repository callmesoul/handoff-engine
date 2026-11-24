/**
 * Browser half of `dsh-handoff-studio`: the full task panel.
 *
 * Registers a sidebar entry (`sidebar.panellist`) and its matching `main` key — DSH
 * requires them as a pair; a sidebar row without a main key throws on click.
 *
 * This is a deliberate port of the original Python panel's surface, not a subset:
 * mode switch (manual/auto), auto configuration, new task, sync, status filter,
 * pagination, the task drawer with all eight actions, the run modal (executor, model,
 * effort, session resume, work-dir mode, auto-merge/deploy/commit), the session
 * picker, log history, security mode and the deliver/advance progress readouts.
 *
 * All data comes from the Python studio engine through the host half's same-origin
 * proxy (`/handoff-studio/panel/*`), so there is no CORS and no second port visible
 * to the browser.
 *
 * Styling uses only `--dsw-alias-*` theme tokens so light/dark and host theming work.
 */
window.__ModuleLoader__.load({
  id: "dsh-handoff-studio",
  factory: function (require) {
    var module = { exports: {} };
    var exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });

    var React = require("react");
    var h = React.createElement;
    var useState = React.useState;
    var useEffect = React.useEffect;
    var useCallback = React.useCallback;
    var useMemo = React.useMemo;
    var useRef = React.useRef;

    var PANEL_ID = "handoff-studio";
    var LABEL = "工作台";
    var API = "/handoff-studio/panel";

    var STATUSES = [
      "open", "claimed", "in_progress", "delivered", "awaiting_merge",
      "merged", "deployed", "verifying", "verified", "frontend_accepted",
      "failed", "cancelled",
    ];

    // -- 样式 ----------------------------------------------------------------
    var CSS = [
      ".hs-root{display:flex;flex-direction:column;flex:1 1 0;min-height:0;min-width:0;",
      "background:var(--dsw-alias-bg-base);color:var(--dsw-alias-label-primary);font-size:13px;}",
      ".hs-bar{display:flex;align-items:center;gap:10px;padding:8px 14px;flex:0 0 auto;",
      "border-bottom:1px solid var(--dsw-alias-border-l1);flex-wrap:wrap;}",
      ".hs-title{font-weight:600;}",
      ".hs-muted{color:var(--dsw-alias-label-secondary);}",
      ".hs-spacer{flex:1 1 auto;}",
      ".hs-dot{width:7px;height:7px;border-radius:50%;flex:0 0 auto;}",
      ".hs-dot.on{background:var(--dsw-alias-state-success-primary);}",
      ".hs-dot.off{background:var(--dsw-alias-state-idle-primary);}",
      ".hs-btn{font:inherit;font-size:12px;line-height:1;padding:5px 10px;border-radius:6px;",
      "border:1px solid var(--dsw-alias-border-l1);cursor:pointer;background:transparent;",
      "color:var(--dsw-alias-label-primary);}",
      ".hs-btn:hover:not(:disabled){border-color:var(--dsw-alias-border-l2);}",
      ".hs-btn:disabled{opacity:.5;cursor:default;}",
      // 品牌色在深色主题下是近白色、浅色主题下是近黑色，所以文字必须用主题提供的
      // 反色标签色，不能写死 #fff——写死会在深色主题里变成「白底白字」的一块空白。
      ".hs-btn.primary{background:var(--dsw-alias-brand-primary);border-color:var(--dsw-alias-brand-primary);",
      "color:var(--dsw-alias-label-primary-inverted);}",
      ".hs-btn.danger{color:var(--dsw-alias-state-error-primary);border-color:var(--dsw-alias-state-error-primary);}",
      ".hs-btn.sm{font-size:11px;padding:3px 8px;}",
      ".hs-select,.hs-input,.hs-textarea{font:inherit;font-size:12px;padding:4px 6px;border-radius:6px;",
      "background:var(--dsw-alias-bg-layer-1);color:var(--dsw-alias-label-primary);",
      "border:1px solid var(--dsw-alias-border-l1);}",
      ".hs-textarea{width:100%;min-height:90px;line-height:1.5;resize:vertical;font-size:12.5px;}",
      ".hs-mode{display:flex;border:1px solid var(--dsw-alias-border-l1);border-radius:7px;overflow:hidden;}",
      ".hs-mode button{border:none;border-radius:0;padding:5px 12px;background:transparent;",
      "font:inherit;font-size:12px;cursor:pointer;color:var(--dsw-alias-label-primary);}",
      ".hs-mode button.active{background:var(--dsw-alias-brand-primary);",
      "color:var(--dsw-alias-label-primary-inverted);}",
      ".hs-body{flex:1 1 0;min-height:0;display:flex;flex-direction:column;overflow:hidden;}",
      ".hs-scroll{overflow:auto;flex:1 1 0;min-height:0;}",
      ".hs-notice{padding:8px 14px;font-size:12px;border-bottom:1px solid var(--dsw-alias-border-l1);",
      "color:var(--dsw-alias-state-warn-primary);}",
      ".hs-notice.err{color:var(--dsw-alias-state-error-primary);}",

      ".hs-run{flex:0 0 auto;border-bottom:1px solid var(--dsw-alias-border-l1);",
      "background:var(--dsw-alias-bg-layer-1);padding:10px 14px;}",
      ".hs-run-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px;}",
      ".hs-log{max-height:210px;overflow:auto;white-space:pre-wrap;word-break:break-word;",
      "font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;line-height:1.6;",
      "background:var(--dsw-alias-bg-base);border-radius:6px;padding:8px 10px;",
      "border:1px solid var(--dsw-alias-border-l1);}",
      ".hs-log .ln-tool{color:var(--dsw-alias-brand-primary);}",
      ".hs-log .ln-warn{color:var(--dsw-alias-state-warn-primary);}",
      ".hs-log .ln-end{color:var(--dsw-alias-state-success-primary);}",
      ".hs-log .ln-err{color:var(--dsw-alias-state-error-primary);}",
      ".hs-live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;",
      "background:var(--dsw-alias-state-success-primary);margin-right:5px;}",

      ".hs-table{width:100%;border-collapse:collapse;}",
      ".hs-table th,.hs-table td{text-align:left;padding:7px 12px;",
      "border-bottom:1px solid var(--dsw-alias-border-l1);vertical-align:top;}",
      ".hs-table th{color:var(--dsw-alias-label-secondary);font-size:12px;font-weight:500;}",
      ".hs-table tbody tr{cursor:pointer;}",
      ".hs-table tbody tr:hover{background:var(--dsw-alias-bg-layer-1);}",
      ".hs-table tbody tr.sel{background:var(--dsw-alias-bg-layer-1);",
      "box-shadow:inset 3px 0 0 var(--dsw-alias-brand-primary);}",
      ".hs-id{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11.5px;",
      "text-decoration:underline;text-underline-offset:3px;cursor:pointer;}",
      ".hs-chip{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11.5px;",
      "border:1px solid var(--dsw-alias-border-l1);white-space:nowrap;}",
      ".hs-chip.st-open{color:var(--dsw-alias-brand-primary);border-color:var(--dsw-alias-brand-primary);}",
      ".hs-chip.st-claimed,.hs-chip.st-in_progress{color:var(--dsw-alias-state-warn-primary);",
      "border-color:var(--dsw-alias-state-warn-primary);}",
      ".hs-chip.st-verified,.hs-chip.st-frontend_accepted{color:var(--dsw-alias-state-success-primary);",
      "border-color:var(--dsw-alias-state-success-primary);}",
      ".hs-chip.st-failed{color:var(--dsw-alias-state-error-primary);",
      "border-color:var(--dsw-alias-state-error-primary);}",
      ".hs-chip.ex-dsh{color:var(--dsw-alias-brand-primary);border-style:dashed;}",
      ".hs-chip.ex-codex{color:var(--dsw-alias-label-secondary);border-style:dashed;}",
      ".hs-chip.ex-zcode{color:var(--dsw-alias-state-success-primary);border-style:dashed;}",
      ".hs-empty{padding:40px 16px;text-align:center;color:var(--dsw-alias-label-secondary);}",
      ".hs-fail{color:var(--dsw-alias-state-error-primary);font-size:11.5px;}",
      ".hs-pager{display:flex;align-items:center;gap:8px;padding:8px 14px;",
      "border-top:1px solid var(--dsw-alias-border-l1);font-size:12px;",
      "color:var(--dsw-alias-label-secondary);flex-wrap:wrap;}",
      ".hs-pager button{padding:3px 9px;}",

      ".hs-drawer{position:absolute;top:0;right:0;bottom:0;width:min(720px,94%);",
      "background:var(--dsw-alias-bg-overlay);border-left:1px solid var(--dsw-alias-border-l1);",
      "display:flex;flex-direction:column;box-shadow:-8px 0 24px rgba(0,0,0,.18);z-index:5;}",
      ".hs-dhead{display:flex;align-items:center;gap:10px;padding:12px 16px;",
      "border-bottom:1px solid var(--dsw-alias-border-l1);flex:0 0 auto;}",
      ".hs-dtitle{font-weight:600;flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}",
      ".hs-dbody{overflow:auto;padding:14px 16px;flex:1 1 0;min-height:0;}",
      ".hs-sec{margin-bottom:18px;}",
      ".hs-sec h3{font-size:12px;margin:0 0 7px;font-weight:600;letter-spacing:.3px;",
      "color:var(--dsw-alias-label-secondary);}",
      ".hs-kv{display:grid;grid-template-columns:112px 1fr;gap:4px 10px;font-size:12.5px;}",
      ".hs-kv .k{color:var(--dsw-alias-label-secondary);}",
      ".hs-pre{background:var(--dsw-alias-bg-base);border:1px solid var(--dsw-alias-border-l1);",
      "border-radius:8px;padding:10px 12px;white-space:pre-wrap;word-break:break-word;",
      "font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;",
      "max-height:280px;overflow:auto;}",
      ".hs-timeline{border-left:2px solid var(--dsw-alias-border-l1);margin-left:6px;padding-left:14px;}",
      ".hs-timeline .ev{position:relative;padding-bottom:9px;}",
      ".hs-timeline .ev::before{content:'';position:absolute;left:-19px;top:6px;width:7px;height:7px;",
      "border-radius:50%;background:var(--dsw-alias-border-l2);}",
      ".hs-timeline .meta{color:var(--dsw-alias-label-secondary);font-size:11.5px;}",
      ".hs-wrap{position:relative;flex:1 1 0;min-height:0;display:flex;flex-direction:column;overflow:hidden;}",
      ".hs-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}",
      ".hs-btnrow{display:flex;gap:8px;flex-wrap:wrap;}",
      ".hs-err{color:var(--dsw-alias-state-error-primary);font-size:12px;}",
      ".hs-hint{color:var(--dsw-alias-label-secondary);font-size:11.5px;margin-top:5px;line-height:1.5;}",
      ".hs-steer{display:flex;gap:8px;margin-top:8px;align-items:center;}",
      ".hs-steer .hs-input{flex:1 1 auto;}",

      ".hs-overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:60;",
      "display:flex;align-items:center;justify-content:center;padding:24px;}",
      ".hs-modal{background:var(--dsw-alias-bg-overlay);border:1px solid var(--dsw-alias-border-l1);",
      "border-radius:12px;width:min(680px,94vw);max-height:88vh;display:flex;flex-direction:column;}",
      ".hs-modal.wide{width:min(880px,94vw);}",
      ".hs-mhead{padding:14px 18px;border-bottom:1px solid var(--dsw-alias-border-l1);",
      "font-weight:600;display:flex;align-items:center;gap:8px;}",
      // 弹窗高度是「按内容自适应、超过 88vh 才收缩」。所以正文不能用 flex:1 1 0：
      // 基准为 0 的弹性项在自适应高度的列容器里会被压成 0 高，字段全部被裁掉、
      // 只剩一行提示。用 0 1 auto 让它先按内容取高，仅在触顶时收缩。
      ".hs-mbody{padding:16px 18px;overflow:auto;flex:0 1 auto;min-height:0;}",
      ".hs-mfoot{padding:12px 18px;border-top:1px solid var(--dsw-alias-border-l1);",
      "display:flex;justify-content:flex-end;gap:8px;}",
      ".hs-formrow{margin-bottom:14px;}",
      ".hs-formrow label{display:block;color:var(--dsw-alias-label-secondary);",
      "font-size:12px;margin-bottom:5px;}",
      ".hs-formrow .hs-select,.hs-formrow .hs-input{width:100%;}",
      // 宽版弹窗里成对的短字段并排，避免单个输入框被拉满整行。
      ".hs-cols2{display:grid;grid-template-columns:1fr 1fr;gap:0 18px;}",
      "@media (max-width:640px){.hs-cols2{grid-template-columns:1fr;}}",
      ".hs-cards{display:flex;gap:10px;}",
      ".hs-card{flex:1 1 0;border:1px solid var(--dsw-alias-border-l1);border-radius:9px;",
      "padding:9px 11px;cursor:pointer;}",
      ".hs-card.sel{border-color:var(--dsw-alias-brand-primary);",
      "background:color-mix(in srgb,var(--dsw-alias-brand-primary) 12%,transparent);}",
      ".hs-card .nm{font-weight:600;font-size:12.5px;}",
      ".hs-card .note{color:var(--dsw-alias-label-secondary);font-size:11.5px;margin-top:2px;}",
      ".hs-checks{display:flex;gap:16px;flex-wrap:wrap;}",
      ".hs-checks label{display:flex;align-items:center;gap:6px;cursor:pointer;",
      "color:var(--dsw-alias-label-primary);font-size:12.5px;margin:0;}",
      // 候选模型链：每行一条，顺序即优先级。模型名占满剩余宽度，档位和按钮定宽，
      // 这样几行的列能对齐——不对齐的话「第几个」根本扫不出来。
      //
      // 宽度必须带 `.hs-formrow` 前缀：通用规则 `.hs-formrow .hs-select{width:100%}`
      // 的优先级是 (0,2,0)，只写 `.hs-fb-effort`（(0,1,0)）根本盖不住。盖不住的
      // 后果不是「档位宽一点」，而是同一行两个 100% 宽的下拉互相挤爆——档位吃掉
      // 整行、模型被压成一条缝，右边的上移/下移/删除直接被推出弹窗看不见。
      ".hs-fb{display:flex;flex-direction:column;gap:6px;}",
      ".hs-fb-row{display:flex;align-items:center;gap:6px;}",
      ".hs-fb-idx{width:18px;flex:0 0 auto;text-align:right;",
      "color:var(--dsw-alias-label-secondary);font-size:11.5px;}",
      ".hs-formrow .hs-fb .hs-fb-model{flex:1 1 auto;min-width:0;width:auto;}",
      ".hs-formrow .hs-fb .hs-fb-effort{width:120px;flex:0 0 auto;}",
      // 行内按钮不参与收缩：它们是定宽图标，被压扁就点不中了。
      ".hs-fb-row .hs-btn{flex:0 0 auto;}",
      ".hs-fb-warn{color:var(--dsw-alias-state-warn-primary);font-size:11px;",
      "white-space:nowrap;flex:0 0 auto;}",
      // 「加一个」是整块的操作入口，不该被拉成整行宽（看起来像又一个下拉）。
      ".hs-fb-add{width:auto;align-self:flex-start;}",
      ".hs-sesslist{border:1px solid var(--dsw-alias-border-l1);border-radius:8px;",
      "max-height:260px;overflow:auto;}",
      ".hs-sess{padding:7px 11px;border-bottom:1px solid var(--dsw-alias-border-l1);",
      "cursor:pointer;display:flex;gap:10px;align-items:center;font-size:12px;}",
      ".hs-sess:hover{background:var(--dsw-alias-bg-layer-1);}",
      ".hs-sess.sel{background:var(--dsw-alias-bg-layer-1);",
      "box-shadow:inset 3px 0 0 var(--dsw-alias-brand-primary);}",
      ".hs-sess .sid{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11.5px;}",
      ".hs-sess .stt{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}",
      ".hs-sess .stm{color:var(--dsw-alias-label-secondary);font-size:11px;}",
      ".hs-toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);",
      "background:var(--dsw-alias-bg-overlay);border:1px solid var(--dsw-alias-border-l1);",
      "padding:9px 18px;border-radius:8px;z-index:100;box-shadow:0 6px 24px rgba(0,0,0,.3);",
      "font-size:12.5px;}",
      ".hs-toast.err{border-color:var(--dsw-alias-state-error-primary);",
      "color:var(--dsw-alias-state-error-primary);}",
    ].join("");

    function Styles() {
      return h("style", { "data-plugin-css": "dsh-handoff-studio" }, CSS);
    }

    // -- HTTP ----------------------------------------------------------------
    function apiRequest(path, init) {
      return fetch(API + path, init).then(function (response) {
        return response.json().then(function (body) {
          if (!response.ok) {
            var message = (body && (body.message || body.error)) || "HTTP " + response.status;
            var error = new Error(message);
            error.body = body;
            throw error;
          }
          return body;
        });
      });
    }

    function apiGet(path) {
      return apiRequest(path, { headers: { accept: "application/json" } });
    }

    function apiPost(path, payload) {
      return apiRequest(path, {
        method: "POST",
        headers: { accept: "application/json", "content-type": "application/json" },
        body: JSON.stringify(payload || {}),
      });
    }

    function q(value) {
      return encodeURIComponent(value);
    }

    // -- 工具 ----------------------------------------------------------------
    function fmtTime(value) {
      if (!value) return "";
      var match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(value));
      if (match) return match[2] + "-" + match[3] + " " + match[4] + ":" + match[5];
      return String(value);
    }

    function statusChip(status) {
      return h("span", { className: "hs-chip st-" + status }, status || "?");
    }

    function executorChip(name) {
      if (!name) return null;
      return h("span", { className: "hs-chip ex-" + name }, name);
    }

    /** 一个按钮，带「为什么不能点」的提示。 */
    function ActionButton(props) {
      var disabled = Boolean(props.disabled);
      return h(
        "button",
        {
          type: "button",
          className: "hs-btn" + (props.variant ? " " + props.variant : ""),
          disabled: disabled,
          title: props.title || (disabled ? props.reason : ""),
          onClick: function (event) {
            event.stopPropagation();
            if (disabled) {
              // 灰掉的按钮也要有反馈：点一下说清为什么不能用
              if (props.reason) props.onBlocked && props.onBlocked(props.reason);
              return;
            }
            props.onClick();
          },
        },
        props.label,
      );
    }

    function Field(props) {
      return h(
        "div",
        { className: "hs-formrow" },
        props.label ? h("label", null, props.label) : null,
        props.children,
        props.hint ? h("div", { className: "hs-hint" }, props.hint) : null,
      );
    }

    /**
     * 某个模型真正支持的思考档位。
     *
     * 为什么不能直接用 executor.efforts：那是执行器的**静态全集**（dsh 的
     * ["low","medium","high"]），和模型无关。真实档位来自模型目录里每个模型自己的
     * reasoning.efforts，而且同 id 的模型在不同 provider 下档位并不相同——
     * 例如 deepseek-v4.1-flash 在 trae 是 low/high/xhigh、在 workbuddy 是
     * low/high/max。宿主侧 `_efforts_for()` 只按 id 取第一条匹配，结果就是
     * 面板显示 max、实际校验用的是 xhigh。
     *
     * 所以这里按 (provider, model) 精确匹配；匹配不到再退回执行器全集，
     * 绝不在拿不到目录时伪造一个档位列表。
     */
    function effortsFor(executor, provider, modelId) {
      var models = (executor && executor.models) || [];
      var mine = null;
      for (var i = 0; i < models.length; i += 1) {
        if (models[i].id !== modelId) continue;
        // 有 provider 就要求一起对上，避免拿到同名但不同路由的档位
        if (provider && models[i].provider && models[i].provider !== provider) continue;
        mine = models[i];
        break;
      }
      var reasoning = mine && mine.reasoning;
      var efforts = (reasoning && reasoning.efforts) || [];
      var ids = [];
      for (var j = 0; j < efforts.length; j += 1) {
        var id = efforts[j] && efforts[j].id;
        if (id) ids.push(String(id));
      }
      if (ids.length) return ids;
      // 模型没有声明档位（或不支持思考）：返回空，别拿执行器全集冒充
      if (mine) return [];
      return ((executor && executor.efforts) || []).slice();
    }

    /**
     * 顶栏「通知」按键的文案。
     *
     * 三件事必须一眼看出来：投递线程还在不在跑（✗ 表示没在跑）、还欠几条没发出去、
     * 有多少是水位之前的历史积压（那些不会自动重发，得人工处理）。
     *
     * 为什么要把这个状态摆到顶栏：完成通知是两段式的（store 只写 outbox，投递由
     * 常驻线程负责）。少了那一段时，任务会显示 verified 而前端什么都没收到，
     * 界面上没有任何地方能看出「还欠一条通知」。
     */
    function pumpLabel(pump) {
      if (!pump) return "";
      var text =
        "通知 " +
        (pump.running ? "" : "✗ ") +
        "pending " +
        (pump.pending == null ? "?" : pump.pending);
      if (pump.stale) text += "（历史 " + pump.stale + "）";
      return text;
    }

    /**
     * 隔离模式选择器。
     *
     * 引擎里没有独立的「隔离」字段：隔离与否完全由 work_dir 是否为空决定
     * （workspace.py 的 Workspace：direct 非空就跳过隔离 worktree）。
     * 所以这里把它做成一个显式的二选一，只负责把选择翻译成 work_dir：
     *   worktree → null（建隔离 worktree + 分支，交付走 PR；续跑时引擎会
     *              自动复用该任务上一轮的 worktree，不需要额外选项）
     *   custom   → 用户填的路径（直接在该目录改，不建 worktree）
     * 不要在这里新造一个引擎不认的 isolation 参数——那会让界面显示的
     * 和引擎实际做的对不上。
     */
    function IsolationPicker(props) {
      var mode = props.mode;
      var setMode = props.setMode;
      var path = props.path;
      var setPath = props.setPath;
      var workDirs = props.workDirs || [];

      var hints = {
        worktree: "建隔离 worktree + 分支，agent 的改动不回落到你的工作区，交付走 PR。",
        custom: "直接用这个目录跑，不建 worktree：改动直接落在该目录，已有 PR 链路不适用。",
      };
      if (props.strategy === "continue") {
        hints.worktree = "续跑会复用该任务上一轮的 worktree，上下文和文件都对得上。";
      }

      return h(
        Field,
        { label: "隔离模式", hint: hints[mode] || "" },
        h(
          "div",
          { className: "hs-cards" },
          h(
            "div",
            {
              className: "hs-card" + (mode === "worktree" ? " sel" : ""),
              onClick: function () {
                setMode("worktree");
              },
            },
            h("div", { className: "nm" }, "隔离 worktree"),
            h("div", { className: "note" }, "推荐 · 不污染你的目录"),
          ),
          h(
            "div",
            {
              className: "hs-card" + (mode === "custom" ? " sel" : ""),
              onClick: function () {
                setMode("custom");
              },
            },
            h("div", { className: "nm" }, "指定目录"),
            h("div", { className: "note" }, "原地改 · 不走 PR"),
          ),
        ),
        mode === "custom"
          ? h(
              "div",
              { style: { marginTop: 8 } },
              workDirs.length
                ? h(
                    "select",
                    {
                      className: "hs-select",
                      value: workDirs.some(function (item) {
                        return item.path === path;
                      })
                        ? path
                        : "",
                      onChange: function (event) {
                        setPath(event.target.value);
                      },
                    },
                    h("option", { value: "" }, "选一个已登记的仓库目录…"),
                    workDirs.map(function (item) {
                      return h("option", { key: item.repo, value: item.path }, item.path);
                    }),
                  )
                : null,
              h("input", {
                className: "hs-input",
                style: workDirs.length ? { marginTop: 6 } : null,
                placeholder: "D:\\project\\your-repo",
                value: path,
                onChange: function (event) {
                  setPath(event.target.value);
                },
              }),
            )
          : null,
      );
    }

    /**
     * 访问权限隔离选择器（三档沙箱）。
     *
     * 这些取值不是我们编的：宿主 @deepseek-ai/dsh-sandbox 的 SandboxMode 就是
     * read-only / workspace-write / danger-full-access 三个。它和上面的
     * 「隔离模式」是两件不同的事：
     *   隔离模式   → 改动落在哪个目录（worktree 还是指定目录），是**位置**问题
     *   访问权限   → agent 在该目录里能读能写多少，是**权限**问题
     *
     * dsh 的通路是让宿主的权限服务切换预设（它一次写沙箱档位 + 审批策略，
     * 和用户在 DSH 界面点那一档等价，顶部的权限名字才会显示「完全权限」而不是
     * custom）；codex 走 --sandbox 参数。这里只负责选值，落盘由各执行器实现。
     */
    function SandboxPicker(props) {
      var value = props.value;
      var setValue = props.setValue;
      var options = props.modes || ["read-only", "workspace-write", "danger-full-access"];

      var LABELS = {
        "read-only": { name: "只读", note: "能读不能写，最安全" },
        "workspace-write": { name: "工作区可写", note: "推荐 · 只能改工作目录" },
        "danger-full-access": { name: "完全权限", note: "沙箱全开 + 免审批 · 谨慎使用" },
      };

      return h(
        Field,
        {
          label: "访问权限隔离",
          hint:
            value === "read-only"
              ? "agent 只能读，任何写入都会被沙箱拒绝——适合只让它分析的任务。任务无人值守，所以审批不询问；DSH 顶部会把这种组合显示成 Custom。"
              : value === "danger-full-access"
                ? "沙箱全开、不再询问审批，DSH 顶部会显示「完全权限」。只在完全信任的任务上用。"
                : value === "workspace-write"
                  ? "写入被限制在工作目录内，越界会被拒绝。任务无人值守，所以审批不询问；DSH 顶部会把这种组合显示成 Custom。"
                  : // 没选任何一档：不要在这里暗示已隔离，否则等于骗人
                    "没选就是跟随 DSH 部署的默认档位；想确保隔离请显式选一档。",
        },
        h(
          "div",
          { className: "hs-cards" },
          options.map(function (mode) {
            var meta = LABELS[mode] || { name: mode, note: "" };
            return h(
              "div",
              {
                key: mode,
                className: "hs-card" + (value === mode ? " sel" : ""),
                onClick: function () {
                  setValue(mode);
                },
              },
              h("div", { className: "nm" }, meta.name),
              h("div", { className: "note" }, meta.note),
            );
          }),
        ),
      );
    }

    /**
     * 候选模型链选择器（全自动配置用）。
     *
     * 解决的是「主模型额度用完了，接下来怎么办」：以前只有一串模型下拉，
     * 跑着跑着额度耗尽就整轮失败、任务退回队列等人发现。这里给一张有序的
     * 候选表，运行器按顺序往下试。
     *
     * 三条界面规则，都是踩过的坑：
     *   1. 顺序就是优先级，所以能上移/下移，而不是只给个多选框；
     *   2. 同一条不能重复出现（重复只会让额度耗尽时白等两轮），加的时候就拦；
     *   3. 模型已经不在可用目录里时**标出来但保留**，让人自己决定删不删——
     *      静默删掉会让人的配置在某次目录变动后「自己消失」。
     */
    function FallbackPicker(props) {
      var items = props.items || [];
      var models = props.models || [];
      var setItems = props.setItems;

      function block(reason) {
        if (props.onBlocked) props.onBlocked(reason);
      }

      function key(item) {
        return (item.provider ? item.provider + "/" : "") + item.model;
      }

      function splitKey(raw) {
        var cut = raw.indexOf("/");
        // provider 来自目录的 group.id（trae / workbuddy / openai-codex…），
        // 不含斜杠；所以第一个斜杠就是分隔符。
        if (cut < 0) return { provider: null, model: raw };
        return { provider: raw.slice(0, cut) || null, model: raw.slice(cut + 1) };
      }

      function effortsOf(provider, model) {
        // 只按目录里该模型自己声明的档位给候选，绝不拿执行器全集冒充
        return effortsFor({ models: models }, provider || null, model);
      }

      /** 这条候选还在不在当前目录里？不在的话下拉要按「已保存」保留它。 */
      function known(item) {
        for (var i = 0; i < models.length; i += 1) {
          if (models[i].id !== item.model) continue;
          if (!item.provider || models[i].provider === item.provider) return true;
        }
        return false;
      }

      function add() {
        var used = {};
        for (var i = 0; i < items.length; i += 1) used[key(items[i])] = true;
        for (var j = 0; j < models.length; j += 1) {
          var candidate = {
            provider: models[j].provider || null,
            model: models[j].id,
            effort: null,
          };
          if (!used[key(candidate)]) {
            setItems(items.concat([candidate]));
            return;
          }
        }
        // 全部模型都已经在链里：明确说一句，而不是点了没反应
        block("可用的模型都已经在候选列表里了");
      }

      function remove(index) {
        var next = items.slice();
        next.splice(index, 1);
        setItems(next);
      }

      function move(index, delta) {
        var target = index + delta;
        if (target < 0 || target >= items.length) return;
        var next = items.slice();
        var moving = next.splice(index, 1)[0];
        next.splice(target, 0, moving);
        setItems(next);
      }

      function change(index, patch) {
        setItems(
          items.map(function (item, at) {
            return at === index ? Object.assign({}, item, patch) : item;
          }),
        );
      }

      var available = models.length > 0;

      return h(
        Field,
        {
          label: "后备模型（额度用完接着跑）",
          hint: available
            ? items.length
              ? "按顺序尝试：当前模型额度用尽 / 被限流 / 鉴权失败时自动换下一个，直到有一个跑通。" +
                "换模型会用新模型重跑这一轮（不接续上一个模型的会话）。"
              : "留空 = 不换模型：当前模型不可用时这一轮直接失败。加几个候选，自动模式就不用别人盯着。"
            : "读不到 DSH 的模型目录，暂时没法加候选；先确认插件与执行器在线。",
        },
        h(
          "div",
          { className: "hs-fb" },
          items.length
            ? items.map(function (item, index) {
                return h(
                  "div",
                  { className: "hs-fb-row", key: key(item) + "#" + index },
                  h("span", { className: "hs-fb-idx" }, String(index + 1)),
                  h(
                    "select",
                    {
                      className: "hs-select hs-fb-model",
                      value: key(item),
                      onChange: function (event) {
                        var picked = splitKey(event.target.value);
                        for (var i = 0; i < items.length; i += 1) {
                          if (
                            i !== index &&
                            items[i].model === picked.model &&
                            (items[i].provider || "") === (picked.provider || "")
                          ) {
                            block("这个模型已经在候选列表里了");
                            return;
                          }
                        }
                        var allowed = effortsOf(picked.provider, picked.model);
                        // 换了模型，旧档位可能不属于它——留着会被宿主拒掉
                        var keep = item.effort && allowed.indexOf(item.effort) >= 0 ? item.effort : null;
                        change(index, { provider: picked.provider, model: picked.model, effort: keep });
                      },
                    },
                    models.map(function (tuple) {
                      var tupleKey = (tuple.provider ? tuple.provider + "/" : "") + tuple.id;
                      var label = tuple.name || tuple.id;
                      if (tuple.provider_name) label += " · " + tuple.provider_name;
                      return h("option", { key: tupleKey, value: tupleKey }, label);
                    }),
                    // 已保存但已不在目录里的候选：给一个占位 option，否则下拉会
                    // 静默跳到第一项，看起来像配置被改了。
                    known(item)
                      ? null
                      : h(
                          "option",
                          { key: key(item), value: key(item) },
                          item.model + "（已不在目录）",
                        ),
                  ),
                  h(
                    "select",
                    {
                      className: "hs-select hs-fb-effort",
                      value: item.effort || "",
                      onChange: function (event) {
                        change(index, { effort: event.target.value || null });
                      },
                    },
                    h("option", { value: "" }, "默认思考"),
                    effortsOf(item.provider, item.model).map(function (id) {
                      return h("option", { key: id, value: id }, id);
                    }),
                  ),
                  item.available === false
                    ? h("span", { className: "hs-fb-warn", title: "这个模型已经不在可用目录里" }, "不在目录")
                    : null,
                  h(
                    "button",
                    {
                      type: "button",
                      className: "hs-btn sm",
                      disabled: index === 0,
                      title: "上移（更早被尝试）",
                      onClick: function () {
                        move(index, -1);
                      },
                    },
                    "↑",
                  ),
                  h(
                    "button",
                    {
                      type: "button",
                      className: "hs-btn sm",
                      disabled: index === items.length - 1,
                      title: "下移",
                      onClick: function () {
                        move(index, 1);
                      },
                    },
                    "↓",
                  ),
                  h(
                    "button",
                    {
                      type: "button",
                      className: "hs-btn sm danger",
                      title: "从候选链里去掉",
                      onClick: function () {
                        remove(index);
                      },
                    },
                    "删",
                  ),
                );
              })
            : h("div", { className: "hs-muted", style: { fontSize: 12 } }, "还没有后备模型"),
          h(
            "button",
            {
              type: "button",
              className: "hs-btn sm hs-fb-add",
              style: { marginTop: 8 },
              disabled: !available,
              onClick: add,
            },
            "+ 加一个后备模型",
          ),
        ),
      );
    }

    function Modal(props) {
      var ref = useRef(null);
      useEffect(
        function () {
          function onKey(event) {
            if (event.key === "Escape") props.onClose();
          }
          document.addEventListener("keydown", onKey);
          return function () {
            document.removeEventListener("keydown", onKey);
          };
        },
        [props.onClose],
      );
      return h(
        "div",
        {
          className: "hs-overlay",
          onMouseDown: function (event) {
            if (event.target === ref.current) props.onClose();
          },
          ref: ref,
        },
        h(
          "div",
          {
            className: "hs-modal" + (props.wide ? " wide" : ""),
            role: "dialog",
            "aria-modal": "true",
            "aria-label": props.title,
          },
          h("div", { className: "hs-mhead" }, props.title),
          h("div", { className: "hs-mbody" }, props.children),
          h("div", { className: "hs-mfoot" }, props.footer),
        ),
      );
    }

    // -- 执行弹窗 ------------------------------------------------------------
    function RunModal(props) {
      var options = props.options || { executors: [] };
      var taskId = props.taskId;
      var execState = useState("dsh");
      var executor = execState[0];
      var setExecutor = execState[1];
      var modelState = useState("");
      var model = modelState[0];
      var setModel = modelState[1];
      var effortState = useState("");
      var effort = effortState[0];
      var setEffort = effortState[1];
      var dirModeState = useState("worktree");
      var dirMode = dirModeState[0];
      var setDirMode = dirModeState[1];
      var dirPathState = useState("");
      var dirPath = dirPathState[0];
      var setDirPath = dirPathState[1];
      var automergeState = useState(true);
      var autoMerge = automergeState[0];
      var setAutoMerge = automergeState[1];
      var waitDeployState = useState(true);
      var waitDeploy = waitDeployState[0];
      var setWaitDeploy = waitDeployState[1];
      var autoCommitState = useState(false);
      var autoCommit = autoCommitState[0];
      var setAutoCommit = autoCommitState[1];
      var sandboxState = useState("");
      var sandbox = sandboxState[0];
      var setSandbox = sandboxState[1];
      var sessionsState = useState([]);
      var sessions = sessionsState[0];
      var setSessions = sessionsState[1];
      var resumeState = useState("");
      var resumeSession = resumeState[0];
      var setResumeSession = resumeState[1];
      var busy = useState(false);
      var working = busy[0];
      var setWorking = busy[1];
      var errorState = useState("");
      var error = errorState[0];
      var setError = errorState[1];

      var current = useMemo(
        function () {
          var list = options.executors || [];
          for (var i = 0; i < list.length; i += 1) {
            if (list[i].id === executor) return list[i];
          }
          return list[0] || { models: [], efforts: [] };
        },
        [options, executor],
      );

      // 打开时带入当前选择
      useEffect(
        function () {
          if (current.selected_model) setModel(current.selected_model);
          if (current.selected_effort) setEffort(current.selected_effort);
          if (options.codex_sandbox_default) setSandbox(options.codex_sandbox_default);
        },
        // 只跑一次：之后由用户操作驱动
        [],
      );

      // 换执行器 → 换模型/档位候选
      useEffect(
        function () {
          var models = current.models || [];
          var has = false;
          for (var i = 0; i < models.length; i += 1) if (models[i].id === model) has = true;
          if (!has) setModel(current.selected_model || (models[0] ? models[0].id : ""));
          var efforts = current.efforts || [];
          if (effort && efforts.indexOf(effort) < 0) setEffort("");
        },
        [executor],
      );

      // 载入会话列表（用于续跑）
      useEffect(
        function () {
          if (!taskId) return;
          apiGet("/api/tasks/" + q(taskId) + "/sessions?executor=" + q(executor))
            .then(function (body) {
              setSessions(body.sessions || []);
            })
            .catch(function () {
              setSessions([]);
            });
        },
        [taskId, executor],
      );

      var models = current.models || [];
      // 档位跟着「选中的模型」走，而不是执行器的静态全集。
      // 未选模型时（跟随配置）才退回执行器全集。
      var efforts = model
        ? effortsFor(current, null, model)
        : (current.efforts || []).slice();

      function submit() {
        setWorking(true);
        setError("");
        var payload = {
          executor: executor,
          model: model || null,
          effort: effort || null,
          resume_session_id: resumeSession || null,
          // codex 的 sandbox 走 --sandbox；dsh 的 sandbox 走 session 的
          // sandbox/mode 事件。同一个取值，两条通路，所以都从 sandbox 发。
          codex_sandbox: executor === "codex" ? sandbox || null : null,
          sandbox: executor !== "codex" ? sandbox || null : null,
          auto_merge: autoMerge,
          wait_deployment: waitDeploy,
          auto_commit: autoCommit,
          work_dir: dirMode === "custom" ? dirPath : null,
        };
        apiPost("/api/tasks/" + q(taskId) + "/run", payload)
          .then(function () {
            props.onDone();
          })
          .catch(function (exc) {
            setError(exc.message || String(exc));
          })
          .then(function () {
            setWorking(false);
          });
      }

      return h(
        Modal,
        {
          title: "执行任务",
          wide: true,
          onClose: props.onClose,
          footer: [
            h(
              "button",
              { key: "c", type: "button", className: "hs-btn", onClick: props.onClose },
              "取消",
            ),
            h(
              "button",
              {
                key: "g",
                type: "button",
                className: "hs-btn primary",
                disabled: working,
                onClick: submit,
              },
              working ? "启动中…" : "开始执行",
            ),
          ],
        },
        h("div", { className: "hs-hint", style: { marginBottom: 10 } }, taskId),
        h(
          "div",
          { className: "hs-formrow" },
          h("label", null, "执行器"),
          h(
            "div",
            { className: "hs-cards" },
            (options.executors || []).map(function (item) {
              return h(
                "div",
                {
                  key: item.id,
                  className: "hs-card" + (item.id === executor ? " sel" : ""),
                  onClick: function () {
                    setExecutor(item.id);
                  },
                },
                h("div", { className: "nm" }, item.label),
                h("div", { className: "note" }, item.note || item.id),
              );
            }),
          ),
        ),
        h(
          Field,
          {
            label: "模型",
            hint:
              executor === "dsh"
                ? "来自 DSH 当前可路由的模型。选模型后档位会换成该模型声明的那组。"
                : "留空 = 跟随该执行器的配置。",
          },
          h(
            "div",
            { className: "hs-row" },
            models.length
              ? h(
                  "select",
                  {
                    className: "hs-select",
                    style: { flex: "1 1 auto" },
                    value: model,
                    onChange: function (event) {
                      setModel(event.target.value);
                      if (executor === "dsh") {
                        apiPost("/api/studio/options", { model: event.target.value }).catch(function () {});
                      }
                    },
                  },
                  h("option", { value: "" }, "跟随配置"),
                  models.map(function (item) {
                    // 同一个 model id 可能挂在多个 provider 下（档位还不一样），
                    // 只写名字会出现两个一模一样、选不出差别的选项——带上 provider。
                    var key = (item.provider ? item.provider + "/" : "") + item.id;
                    var label = item.name || item.id;
                    if (item.provider_name) label += " · " + item.provider_name;
                    return h("option", { key: key, value: item.id }, label);
                  }),
                )
              : h("input", {
                  className: "hs-input",
                  style: { flex: "1 1 auto" },
                  placeholder: "留空 = 跟随配置",
                  value: model,
                  onChange: function (event) {
                    setModel(event.target.value);
                  },
                }),
            h(
              "select",
              {
                className: "hs-select",
                style: { width: 170 },
                value: effort,
                onChange: function (event) {
                  setEffort(event.target.value);
                },
              },
              h("option", { value: "" }, "默认思考"),
              efforts.map(function (id) {
                return h("option", { key: id, value: id }, id);
              }),
            ),
          ),
        ),
        executor === "codex"
          ? h(
              Field,
              {
                label: "Codex 安全模式（--sandbox）",
                hint: "进程启动参数，改了从下一轮生效；执行中可在顶部横幅切换。",
              },
              h(
                "select",
                {
                  className: "hs-select",
                  value: sandbox,
                  onChange: function (event) {
                    setSandbox(event.target.value);
                  },
                },
                (options.codex_sandbox_options || []).map(function (id) {
                  return h("option", { key: id, value: id }, id);
                }),
              ),
            )
          : executor === "dsh"
            ? h(SandboxPicker, {
                value: sandbox,
                setValue: setSandbox,
                modes: options.sandbox_modes,
              })
            : null,
        h(
          Field,
          {
            label: "接续会话（可选）",
            hint: sessions.length
              ? "置顶的是本任务历史会话；接续可以带着已读过的上下文。"
              : "这个任务还没有可续跑的历史会话。",
          },
          sessions.length
            ? h(
                "div",
                { className: "hs-sesslist" },
                h(
                  "div",
                  {
                    className: "hs-sess" + (resumeSession === "" ? " sel" : ""),
                    onClick: function () {
                      setResumeSession("");
                    },
                  },
                  h("span", { className: "stt" }, "新开一个会话"),
                ),
                sessions.map(function (item) {
                  return h(
                    "div",
                    {
                      key: item.id,
                      className: "hs-sess" + (resumeSession === item.id ? " sel" : ""),
                      onClick: function () {
                        setResumeSession(item.id);
                      },
                    },
                    h("span", { className: "sid" }, item.id.slice(0, 18)),
                    h("span", { className: "stt" }, item.title || ""),
                    item.attempt ? h("span", { className: "stm" }, "attempt " + item.attempt) : null,
                  );
                }),
              )
            : null,
        ),
        h(IsolationPicker, {
          mode: dirMode,
          setMode: setDirMode,
          path: dirPath,
          setPath: setDirPath,
          workDirs: options.work_dirs || [],
        }),
        h(
          Field,
          { label: "执行选项" },
          h(
            "div",
            { className: "hs-checks" },
            h(
              "label",
              null,
              h("input", {
                type: "checkbox",
                checked: autoMerge,
                disabled: dirMode === "custom",
                onChange: function (event) {
                  setAutoMerge(event.target.checked);
                },
              }),
              "自动合并（过 gate 后）",
            ),
            h(
              "label",
              null,
              h("input", {
                type: "checkbox",
                checked: waitDeploy,
                onChange: function (event) {
                  setWaitDeploy(event.target.checked);
                },
              }),
              "等部署对齐",
            ),
            h(
              "label",
              null,
              h("input", {
                type: "checkbox",
                checked: autoCommit,
                disabled: dirMode !== "custom",
                onChange: function (event) {
                  setAutoCommit(event.target.checked);
                },
              }),
              "目录模式自动提交并推送",
            ),
          ),
        ),
        error ? h("div", { className: "hs-err" }, error) : null,
      );
    }

    // -- 新建任务弹窗 --------------------------------------------------------
    function NewTaskModal(props) {
      var options = props.options || {};
      var projects = useMemo(
        function () {
          return (options.projects || []).filter(function (item) {
            return item.role === "backend";
          });
        },
        [options],
      );
      var titleState = useState("");
      var title = titleState[0];
      var setTitle = titleState[1];
      var repoState = useState(projects[0] ? projects[0].id : "");
      var repo = repoState[0];
      var setRepo = repoState[1];
      var requirementState = useState("");
      var requirement = requirementState[0];
      var setRequirement = requirementState[1];
      var envState = useState((options.environments || [])[0] || "dev");
      var environment = envState[0];
      var setEnvironment = envState[1];
      var outState = useState("");
      var outOfScope = outState[0];
      var setOutOfScope = outState[1];
      var busyState = useState(false);
      var busy = busyState[0];
      var setBusy = busyState[1];
      var errorState = useState("");
      var error = errorState[0];
      var setError = errorState[1];

      function submit() {
        setBusy(true);
        setError("");
        apiPost("/api/tasks/create", {
          title: title,
          requirement: requirement,
          repo: repo,
          environment: environment,
          out_of_scope: outOfScope,
        })
          .then(function (body) {
            props.onDone(body);
          })
          .catch(function (exc) {
            setError(exc.message || String(exc));
          })
          .then(function () {
            setBusy(false);
          });
      }

      return h(
        Modal,
        {
          title: "新建任务",
          onClose: props.onClose,
          footer: [
            h("button", { key: "c", type: "button", className: "hs-btn", onClick: props.onClose }, "取消"),
            h(
              "button",
              {
                key: "g",
                type: "button",
                className: "hs-btn primary",
                disabled: busy || !title.trim() || !requirement.trim() || !repo,
                onClick: submit,
              },
              busy ? "创建中…" : "创建",
            ),
          ],
        },
        h(
          Field,
          { label: "标题（一句话，二十字以内）" },
          h("input", {
            className: "hs-input",
            value: title,
            maxLength: 200,
            onChange: function (event) {
              setTitle(event.target.value);
            },
          }),
        ),
        h(
          Field,
          { label: "目标项目" },
          projects.length
            ? h(
                "select",
                {
                  className: "hs-select",
                  value: repo,
                  onChange: function (event) {
                    setRepo(event.target.value);
                  },
                },
                projects.map(function (item) {
                  return h("option", { key: item.id, value: item.id }, item.id);
                }),
              )
            : h("div", { className: "hs-err" }, "没有已登记的 backend 项目"),
        ),
        h(
          Field,
          {
            label: "需求（字段级：接口、方法、请求、响应、边界）",
            hint: "写清楚到后端能照着做。不要写「以某文档为准」。",
          },
          h("textarea", {
            className: "hs-textarea",
            rows: 7,
            value: requirement,
            onChange: function (event) {
              setRequirement(event.target.value);
            },
          }),
        ),
        h(
          Field,
          { label: "验收环境" },
          h(
            "select",
            {
              className: "hs-select",
              value: environment,
              onChange: function (event) {
                setEnvironment(event.target.value);
              },
            },
            (options.environments || []).map(function (name) {
              return h("option", { key: name, value: name }, name);
            }),
          ),
        ),
        h(
          Field,
          { label: "不在范围内（换行或逗号分隔）", hint: "明确不做的事，防止顺手改别的。" },
          h("textarea", {
            className: "hs-textarea",
            rows: 3,
            value: outOfScope,
            onChange: function (event) {
              setOutOfScope(event.target.value);
            },
          }),
        ),
        error ? h("div", { className: "hs-err" }, error) : null,
      );
    }

    // -- 自动模式配置弹窗 ----------------------------------------------------
    function AutoConfigModal(props) {
      var options = props.options || {};
      var auto = props.auto || {};
      var executorState = useState(auto.executor || "dsh");
      var executor = executorState[0];
      var setExecutor = executorState[1];
      var strategyState = useState(auto.session_strategy || "new");
      var strategy = strategyState[0];
      var setStrategy = strategyState[1];
      var workDirState = useState(auto.work_dir || "");
      var workDir = workDirState[0];
      var setWorkDir = workDirState[1];
      // 隔离模式不是独立字段：work_dir 有值就是「指定目录」，没有就是「隔离 worktree」。
      // 初始值由已保存的配置反推，用户改了再翻译回 work_dir。
      var isolationState = useState(auto.work_dir ? "custom" : "worktree");
      var isolation = isolationState[0];
      var setIsolation = isolationState[1];
      var modelState = useState(auto.model || "");
      var model = modelState[0];
      var setModel = modelState[1];
      var effortState = useState(auto.effort || "");
      var effort = effortState[0];
      var setEffort = effortState[1];
      // 访问权限隔离：已保存的档位，空 = 跟随部署默认
      var sandboxState = useState(auto.sandbox || "");
      var sandbox = sandboxState[0];
      var setSandbox = sandboxState[1];
      // 后备模型链：主模型额度用尽时按顺序往下试。存在 auto.fallback_models。
      // 每条只认 {provider, model, effort}，多出来的字段提交时会被丢掉。
      var fallbacksState = useState(function () {
        var saved = Array.isArray(auto.fallback_models) ? auto.fallback_models : [];
        return saved
          .filter(function (item) {
            return item && typeof item.model === "string" && item.model.trim();
          })
          .map(function (item) {
            return {
              provider: item.provider || null,
              model: String(item.model).trim(),
              effort: item.effort || null,
            };
          });
      });
      var fallbacks = fallbacksState[0];
      var setFallbacks = fallbacksState[1];
      // 候选链的就地提示（「重复了」「都在列表里了」）。放在选择器下面而不是
      // 弹窗底部的统一报错里，否则看不出是哪一行触发的。
      var fbNoteState = useState("");
      var fbNote = fbNoteState[0];
      var setFbNote = fbNoteState[1];

      var current = useMemo(
        function () {
          var list = options.executors || [];
          for (var i = 0; i < list.length; i += 1) {
            if (list[i].id === executor) return list[i];
          }
          return list[0] || { models: [], efforts: [] };
        },
        [options, executor],
      );

      // 面板的 options 是启动时拉的；如果这会儿还没到（比如刚打开面板就点了配置），
      // 自己补拉一次，否则模型下拉会是空的、让人以为「没有模型可选」。
      var fetchedState = useState(null);
      var fetched = fetchedState[0];
      var setFetched = fetchedState[1];
      useEffect(
        function () {
          if ((options.executors || []).length) return;
          var alive = true;
          apiPost("/api/studio/options", {})
            .then(function (body) {
              if (alive) setFetched(body || null);
            })
            .catch(function () {});
          return function () {
            alive = false;
          };
        },
        [options],
      );

      var view = (options.executors || []).length ? options : fetched || options;
      var currentView = useMemo(
        function () {
          var list = view.executors || [];
          for (var i = 0; i < list.length; i += 1) {
            if (list[i].id === executor) return list[i];
          }
          return list[0] || { models: [], efforts: [] };
        },
        [view, executor],
      );

      // 换执行器 → 换模型候选；旧选择不属于新执行器就退回「跟随配置」，
      // 否则会把 codex 的模型名发给 dsh，或者反过来。
      useEffect(
        function () {
          var models = currentView.models || [];
          var known = false;
          for (var i = 0; i < models.length; i += 1) if (models[i].id === model) known = true;
          if (model && !known) setModel("");
        },
        [executor],
      );

      // 档位必须跟着**模型**重新校验：不同模型的档位集合不同（v4.1-flash 是
      // low/high/xhigh，gpt-6.1 是 low/medium/high/xhigh/max/ultra）。
      // 只在换执行器时校验是不够的——换了模型仍会留着上一个模型的档位，
      // 提交后被宿主拒掉（"模型 X 不支持思考程度 Y"）。
      useEffect(
        function () {
          if (!effort) return;
          var allowed = effortsFor(currentView, null, model);
          if (allowed.length && allowed.indexOf(effort) < 0) setEffort("");
        },
        [model],
      );
      var autoMergeState = useState(auto.auto_merge !== false);
      var autoMerge = autoMergeState[0];
      var setAutoMerge = autoMergeState[1];
      var waitDeployState = useState(auto.wait_deployment !== false);
      var waitDeploy = waitDeployState[0];
      var setWaitDeploy = waitDeployState[1];
      var autoCommitState = useState(Boolean(auto.auto_commit));
      var autoCommit = autoCommitState[0];
      var setAutoCommit = autoCommitState[1];
      var busyState = useState(false);
      var busy = busyState[0];
      var setBusy = busyState[1];
      var errorState = useState("");
      var error = errorState[0];
      var setError = errorState[1];

      function submit() {
        setBusy(true);
        setError("");
        // 隔离模式翻译回引擎认识的 work_dir：只有「指定目录」才带路径。
        // 选了指定目录却留空，等于没选——直接拦下来，别让它悄悄跑成隔离 worktree。
        if (isolation === "custom" && !workDir.trim()) {
          setBusy(false);
          setError("隔离模式选了「指定目录」，请填一个目录路径；想要隔离 worktree 就把模式切回去。");
          return;
        }
        apiPost("/api/mode", {
          mode: props.mode,
          auto: {
            executor: executor,
            session_strategy: strategy,
            model: model || null,
            effort: effort || null,
            // 访问权限隔离只对 dsh 生效（codex 用 codex_sandbox）
            sandbox: executor === "dsh" ? sandbox || null : null,
            work_dir: isolation === "custom" ? workDir.trim() : null,
            auto_merge: autoMerge,
            wait_deployment: waitDeploy,
            auto_commit: autoCommit,
            // 后备模型链只在 dsh 下有实际意义（codex/zcode 的模型来自各自
            // 配置文件，换模型不由这个面板管）。非 dsh 提交空表，免得让人以为
            // 一条 dsh 的候选链对 codex 也生效。
            fallback_models: executor === "dsh" ? fallbacks : [],
          },
        })
          .then(function () {
            props.onDone();
          })
          .catch(function (exc) {
            setError(exc.message || String(exc));
          })
          .then(function () {
            setBusy(false);
          });
      }

      return h(
        Modal,
        {
          title: "全自动模式配置",
          wide: true,
          onClose: props.onClose,
          footer: [
            h("button", { key: "c", type: "button", className: "hs-btn", onClick: props.onClose }, "取消"),
            h(
              "button",
              { key: "g", type: "button", className: "hs-btn primary", disabled: busy, onClick: submit },
              busy ? "保存中…" : "保存并应用",
            ),
          ],
        },
        h(
          "div",
          { className: "hs-hint", style: { marginBottom: 12 } },
          "全自动模式会自动认领 open 的任务，单并发；同一时刻只跑一单。",
        ),
        h(
          "div",
          { className: "hs-cols2" },
          h(
            Field,
            { label: "执行器" },
            h(
              "select",
              {
                className: "hs-select",
                value: executor,
                onChange: function (event) {
                  setExecutor(event.target.value);
                },
              },
              (view.executors || []).map(function (item) {
                return h("option", { key: item.id, value: item.id }, item.label + "（" + item.id + "）");
              }),
            ),
          ),
          h(
            Field,
            { label: "会话策略" },
            h(
              "select",
              {
                className: "hs-select",
                value: strategy,
                onChange: function (event) {
                  setStrategy(event.target.value);
                },
              },
              h("option", { value: "new" }, "每次新会话"),
              h("option", { value: "continue" }, "接续该任务上次的会话"),
            ),
          ),
        ),
        h(IsolationPicker, {
          mode: isolation,
          setMode: setIsolation,
          path: workDir,
          setPath: setWorkDir,
          workDirs: (view && view.work_dirs) || [],
          strategy: strategy,
        }),
        // 访问权限隔离：只对 dsh 有意义（codex 有自己那套 --sandbox 横幅）。
        executor === "dsh"
          ? h(SandboxPicker, {
              value: sandbox,
              setValue: setSandbox,
              modes: view && view.sandbox_modes,
            })
          : null,
        h(
          "div",
          { className: "hs-cols2" },
          h(
            Field,
            {
              label: "模型",
              hint:
                executor === "dsh"
                  ? "来自 DSH 当前可路由的模型；留空 = 跟随配置。"
                  : "留空 = 跟随该执行器的配置。",
            },
            (currentView.models || []).length
              ? h(
                  "select",
                  {
                    className: "hs-select",
                    value: model,
                    onChange: function (event) {
                      setModel(event.target.value);
                      // dsh 的档位是「每个模型声明一组」，换模型要重新拉一次候选。
                      if (executor === "dsh") {
                        apiPost("/api/studio/options", { model: event.target.value }).catch(function () {});
                      }
                    },
                  },
                  h("option", { value: "" }, "跟随配置"),
                  (currentView.models || []).map(function (item) {
                    // 同一个 model id 可能出现在多个 provider 下，且档位不同。
                    // 用 provider/id 作 key，标签带上 provider，避免选了看不出是哪一个。
                    var key = (item.provider ? item.provider + "/" : "") + item.id;
                    var label = item.name || item.id;
                    if (item.provider_name) label += " · " + item.provider_name;
                    return h("option", { key: key, value: item.id }, label);
                  }),
                )
              : h("input", {
                  className: "hs-input",
                  placeholder: "留空 = 跟随配置",
                  value: model,
                  onChange: function (event) {
                    setModel(event.target.value);
                  },
                }),
          ),
          h(
            Field,
            {
              label: "思考档位",
              hint: model
                ? (effortsFor(currentView, null, model).length
                    ? "只列 " + model + " 自己声明的档位；留空 = 用模型默认。"
                    : "该模型没有声明思考档位（或它不支持），留空即用默认。")
                : "先选模型再看档位；留空 = 用模型默认。",
            },
            (function () {
              // 档位候选严格来自当前选中的模型；没选模型就没得选，
              // 不再拿执行器的全集冒充（那正是「选 A 模型却能选 B 的档位」的根因）。
              var allowed = model
                ? effortsFor(currentView, null, model)
                : (currentView.efforts || []);
              return allowed.length
                ? h(
                    "select",
                    {
                      className: "hs-select",
                      value: effort,
                      onChange: function (event) {
                        setEffort(event.target.value);
                      },
                    },
                    h("option", { value: "" }, "默认思考"),
                    allowed.map(function (id) {
                      return h("option", { key: id, value: id }, id);
                    }),
                  )
                : h("input", {
                    className: "hs-input",
                    placeholder: "留空 = 默认",
                    value: effort,
                    onChange: function (event) {
                      setEffort(event.target.value);
                    },
                  });
            })(),
          ),
        ),
        // 后备模型链：只在 dsh 下出现。codex/zcode 的模型来自各自的配置文件，
        // 换模型不是这个面板的事——把控件藏起来比放一个不生效的下拉诚实。
        executor === "dsh"
          ? h(
              "div",
              null,
              h(FallbackPicker, {
                items: fallbacks,
                models: currentView.models || [],
                setItems: setFallbacks,
                // 「加不进去 / 重复了」这类拒绝要就地说明：写在弹窗最底部会被
                // 当成一个莫名其妙的报错。
                onBlocked: function (reason) {
                  setFbNote(reason);
                },
              }),
              fbNote ? h("div", { className: "hs-hint" }, fbNote) : null,
            )
          : null,
        h(
          Field,
          { label: "执行选项" },
          h(
            "div",
            { className: "hs-checks" },
            h(
              "label",
              null,
              h("input", {
                type: "checkbox",
                checked: autoMerge,
                onChange: function (event) {
                  setAutoMerge(event.target.checked);
                },
              }),
              "自动合并",
            ),
            h(
              "label",
              null,
              h("input", {
                type: "checkbox",
                checked: waitDeploy,
                onChange: function (event) {
                  setWaitDeploy(event.target.checked);
                },
              }),
              "等部署",
            ),
            h(
              "label",
              null,
              h("input", {
                type: "checkbox",
                checked: autoCommit,
                onChange: function (event) {
                  setAutoCommit(event.target.checked);
                },
              }),
              "目录模式自动提交",
            ),
          ),
        ),
        error ? h("div", { className: "hs-err" }, error) : null,
      );
    }

    // -- 会话选择弹窗 --------------------------------------------------------
    function SessionModal(props) {
      var searchState = useState("");
      var search = searchState[0];
      var setSearch = searchState[1];
      var visible = useMemo(
        function () {
          if (!search.trim()) return props.sessions;
          var needle = search.trim().toLowerCase();
          return props.sessions.filter(function (item) {
            return (
              String(item.id || "").toLowerCase().indexOf(needle) >= 0 ||
              String(item.title || "").toLowerCase().indexOf(needle) >= 0 ||
              String(item.cwd || "").toLowerCase().indexOf(needle) >= 0
            );
          });
        },
        [props.sessions, search],
      );
      return h(
        Modal,
        {
          title: "选择会话（" + props.executor + "）",
          onClose: props.onClose,
          footer: [h("button", { key: "c", type: "button", className: "hs-btn", onClick: props.onClose }, "关闭")],
        },
        h("input", {
          className: "hs-input",
          style: { width: "100%", marginBottom: 10 },
          placeholder: "搜索 id / 标题 / 目录…",
          value: search,
          onChange: function (event) {
            setSearch(event.target.value);
          },
        }),
        visible.length
          ? h(
              "div",
              { className: "hs-sesslist", style: { maxHeight: 380 } },
              visible.map(function (item) {
                return h(
                  "div",
                  {
                    key: item.id,
                    className: "hs-sess" + (item.id === props.selected ? " sel" : ""),
                    onClick: function () {
                      props.onPick(item.id);
                    },
                  },
                  h("span", { className: "sid" }, item.id.slice(0, 20)),
                  h("span", { className: "stt" }, item.title || ""),
                  h("span", { className: "stm" }, fmtTime(item.updated_at)),
                );
              }),
            )
          : h("div", { className: "hs-empty" }, "没有可用的会话"),
      );
    }

    // -- 抽屉 ----------------------------------------------------------------
    function Drawer(props) {
      var detail = props.detail;
      var bodyRef = useRef(null);
      var liveRef = useRef(null);
      if (!detail) return null;
      var task = detail.task || {};
      var events = detail.events || [];
      var result = task.result || {};
      var failure = task.failure;
      var run = props.run;
      var isRunning = Boolean(run && run.phase === "running" && run.task_id === task.id);

      useEffect(
        function () {
          var node = liveRef.current;
          if (node) node.scrollTop = node.scrollHeight;
        },
        [run && run.log_total, isRunning],
      );

      // 每个按钮的启用规则与原面板逐条对齐
      var canRun = ["open", "failed", "cancelled"].indexOf(task.status) >= 0;
      var canRetry = task.status === "failed";
      var hasLease = ["claimed", "in_progress"].indexOf(task.status) >= 0;
      var canDeliver = ["merged", "deployed", "verifying"].indexOf(task.status) >= 0;
      var canResend = ["verified", "failed"].indexOf(task.status) >= 0;
      var canAccept = task.status === "verified";
      var canAdvance =
        ["delivered", "awaiting_merge", "merged", "deployed", "verifying"].indexOf(task.status) >= 0;
      var deployment = task.deployment || {};
      var canReverify =
        ["deployed", "verified"].indexOf(task.status) >= 0 ||
        (task.status === "failed" &&
          ((failure && failure.stage === "verification") ||
            (deployment.status === "deployed" && deployment.target_sha && deployment.observed_sha)));

      var runWhy =
        "当前状态 " + task.status + " 不能直接执行（只有 open / failed / cancelled 可以）。卡在 in_progress 的话先「释放租约」。";
      var retryWhy = "只有 failed 能重新入队（当前 " + task.status + "）。";
      var releaseWhy = "只有 claimed / in_progress 才有租约（当前 " + task.status + "）。";
      var deliverWhy = "只有 merged / deployed / verifying 能验收（当前 " + task.status + "）。";
      var resendWhy = "只有 verified / failed 才有完成通知可重发（当前 " + task.status + "）。";
      var acceptWhy = "只有 verified 才能标记前端已验收（当前 " + task.status + "）。";
      var advanceWhy =
        "恢复推进只接手交付链路中途断掉的状态（delivered / awaiting_merge / merged / deployed / verifying；当前 " +
        task.status +
        "）。";
      var reverifyWhy =
        "重新验收只对 deployed / verified、failed(verification) 或 failed 但交付已部署的任务开放（当前 " +
        task.status +
        (failure && failure.stage ? "，失败阶段 " + failure.stage : "") +
        "）。";

      var checks = (task.acceptance && task.acceptance.checks) || [];
      var notifyInfo = props.notifyInfo;

      return h(
        "div",
        { className: "hs-drawer" },
        h(
          "div",
          { className: "hs-dhead" },
          h("span", { className: "hs-dtitle" }, task.title || task.id || ""),
          statusChip(task.status),
          h("button", { type: "button", className: "hs-btn", onClick: props.onClose }, "✕"),
        ),
        h(
          "div",
          { className: "hs-dbody", ref: bodyRef },
          isRunning
            ? h(
                "div",
                { className: "hs-sec" },
                h("h3", null, "执行实况"),
                h(
                  "div",
                  { className: "hs-log", ref: liveRef },
                  (run.logs || []).map(function (line, index) {
                    return h(LogLine, { key: index, text: line });
                  }),
                ),
              )
            : null,

          failure
            ? h(
                "div",
                { className: "hs-sec" },
                h("h3", null, "失败原因（" + (failure.stage || "?") + "）"),
                h("div", { className: "hs-pre hs-err" }, failure.message || ""),
              )
            : null,

          // 完成通知的送达情况。以前这块完全不可见：任务 verified 了、前端却没
          // 收到事件，人只能在 outbox 里数 pending。现在这里直接说清「送到哪 /
          // 送没送到 / 为什么没送到」。
          h(
            "div",
            { className: "hs-sec" },
            h("h3", null, "完成通知"),
            notifyInfo && notifyInfo.reason === "no_event"
              ? h(
                  "div",
                  { className: "hs-hint" },
                  "这个任务还没有完成通知（只有 verified / failed 才会生成，且需要创建时登记过前端回程地址）。",
                )
              : notifyInfo
                ? h(
                    "div",
                    null,
                    h(
                      "div",
                      { className: "hs-kv" },
                      kv("状态", notifyInfo.delivered ? "已送达前端 ✔" : "待发送 / 重试中"),
                      kv("目标", notifyInfo.target),
                      kv("revision", notifyInfo.revision),
                      kv("任务状态", notifyInfo.status),
                      kv("尝试次数", notifyInfo.attempts),
                      kv("送达时间", fmtTime(notifyInfo.delivered_at)),
                      kv("最近错误", notifyInfo.last_error),
                    ),
                    notifyInfo.delivered
                      ? null
                      : h(
                          "div",
                          { className: "hs-hint" },
                          "投递线程会自动重试（指数退避）。若一直是这个状态，看顶部「通知」指示灯里报的原因——常见是前端监听端口没起或不在允许网段。",
                        ),
                  )
                : h("div", { className: "hs-hint" }, "查不到这个任务的通知记录。"),
          ),

          h(
            "div",
            { className: "hs-sec" },
            h("h3", null, "基本信息"),
            h(
              "div",
              { className: "hs-kv" },
              kv("任务 ID", task.id),
              kv("状态", task.status),
              kv("尝试次数", task.attempt),
              kv("目标仓库", (task.target || {}).repo),
              kv("基线分支", (task.target || {}).base_ref),
              kv("执行器", result.executor),
              kv("会话", result.executor_session_id),
              kv(
                "PR",
                result.pr_url
                  ? h("a", { href: result.pr_url, target: "_blank", rel: "noreferrer" }, result.pr_url)
                  : null,
              ),
              kv("合并 SHA", result.merge_sha),
              kv("部署", (task.deployment || {}).status),
              kv("工作区", result.worktree),
              kv("租约", task.claim ? task.claim.worker_id : null),
              kv("创建时间", fmtTime(task.created_at)),
              kv("更新时间", fmtTime(task.updated_at)),
            ),
          ),

          h(
            "div",
            { className: "hs-sec" },
            h("h3", null, "操作"),
            h(
              "div",
              { className: "hs-btnrow" },
              h(ActionButton, {
                label: "▶ 执行…",
                variant: "primary",
                disabled: !canRun,
                reason: runWhy,
                onBlocked: props.onBlocked,
                onClick: function () {
                  props.onOpenRun(task.id);
                },
              }),
              h(ActionButton, {
                label: "▶ 继续上次会话",
                disabled: !canRun,
                reason: runWhy,
                title: "接着这个任务上一次的执行会话继续，不用从头交代上下文",
                onBlocked: props.onBlocked,
                onClick: function () {
                  props.onResumeLast(task.id);
                },
              }),
              h(ActionButton, {
                label: "重新入队",
                disabled: !canRetry,
                reason: retryWhy,
                onBlocked: props.onBlocked,
                onClick: function () {
                  props.onAction("POST", "/api/tasks/" + q(task.id) + "/retry", null, "已重新入队");
                },
              }),
              h(ActionButton, {
                label: "释放租约",
                disabled: !hasLease,
                reason: releaseWhy,
                onBlocked: props.onBlocked,
                onClick: function () {
                  props.onAction(
                    "POST",
                    "/api/tasks/" + q(task.id) + "/release",
                    { reason: "studio 释放租约" },
                    "租约已释放",
                  );
                },
              }),
              h(ActionButton, {
                label: "验收（deliver）",
                disabled: !canDeliver,
                reason: deliverWhy,
                onBlocked: props.onBlocked,
                onClick: function () {
                  props.onAction("POST", "/api/deliver", { task_id: task.id }, "验收已启动");
                },
              }),
              h(ActionButton, {
                label: "重发完成通知",
                disabled: !canResend,
                reason: resendWhy,
                title: "把完成通知再发一次到创建任务的前端监听端口",
                onBlocked: props.onBlocked,
                onClick: function () {
                  props.onAction(
                    "POST",
                    "/api/tasks/" + q(task.id) + "/resend-notify",
                    null,
                    "完成通知已重发",
                  );
                },
              }),
              h(ActionButton, {
                label: "前端已验收",
                disabled: !canAccept,
                reason: acceptWhy,
                title: "前端收到完成通知并自行验收成功后点这里，把任务标记为 frontend_accepted 终态",
                onBlocked: props.onBlocked,
                onClick: function () {
                  props.onAction(
                    "POST",
                    "/api/tasks/" + q(task.id) + "/accept",
                    { reason: "studio：前端验收通过" },
                    "已标记前端已验收",
                  );
                },
              }),
              h(ActionButton, {
                label: "重新验收",
                disabled: !canReverify,
                reason: reverifyWhy,
                title:
                  "清除上一次验收结果回到 deployed 并立即重跑部署对齐 + 检查；不重跑执行器、不加 attempt",
                onBlocked: props.onBlocked,
                onClick: function () {
                  props.onAction(
                    "POST",
                    "/api/tasks/" + q(task.id) + "/reverify",
                    { reason: "studio：重新验收" },
                    "重新验收已启动（后台执行）",
                  );
                },
              }),
              h(ActionButton, {
                label: "恢复推进",
                disabled: !canAdvance,
                reason: advanceWhy,
                title: "把中断的后续流程接起来：自动合并 / 重新推送 / 部署对齐 / 验收",
                onBlocked: props.onBlocked,
                onClick: function () {
                  props.onAction(
                    "POST",
                    "/api/tasks/" + q(task.id) + "/advance",
                    null,
                    "恢复推进已启动（后台执行）",
                  );
                },
              }),
            ),
            h(
              "div",
              { className: "hs-hint" },
              "灰掉的按钮点一下会告诉你为什么不能用。「恢复推进」只接手交付后中断的任务，不会伪造 merged / verified。「重新验收」不重跑执行器：清掉旧验收结果直接重跑检查。",
            ),
            props.progress ? h("div", { className: "hs-hint" }, props.progress) : null,
          ),

          h(
            "div",
            { className: "hs-sec" },
            h("h3", null, "需求"),
            h("div", { className: "hs-pre" }, task.requirement || "（空）"),
          ),

          checks.length
            ? h(
                "div",
                { className: "hs-sec" },
                h("h3", null, "验收（环境 " + ((task.acceptance || {}).environment || "—") + "）"),
                h(
                  "div",
                  { className: "hs-pre" },
                  checks
                    .map(function (check) {
                      if (check.type === "http") {
                        return "http " + check.method + " " + check.url + " → " + check.expect_status;
                      }
                      if (check.type === "openapi_diff") {
                        return "openapi_diff " + check.expected_path + " ↔ " + check.actual_url;
                      }
                      return check.type + " " + (check.repo || "") + ":" + (check.name || "");
                    })
                    .join("\n"),
                ),
              )
            : null,

          h(
            "div",
            { className: "hs-sec" },
            h("h3", null, "执行日志"),
            h(
              "div",
              { className: "hs-btnrow", style: { marginBottom: 6 } },
              props.logFiles.length
                ? props.logFiles.map(function (file) {
                    return h(
                      "button",
                      {
                        key: file.name,
                        type: "button",
                        className: "hs-btn sm",
                        onClick: function () {
                          props.onLog(file.attempt || file.name);
                        },
                      },
                      file.name,
                    );
                  })
                : h("span", { className: "hs-muted" }, "这个任务还没有日志文件"),
            ),
            props.logText ? h("div", { className: "hs-pre" }, props.logText) : null,
          ),

          h(
            "div",
            { className: "hs-sec" },
            h("h3", null, "事件时间线（最近 " + Math.min(events.length, 40) + " / 共 " + events.length + "）"),
            h(
              "div",
              { className: "hs-timeline" },
              events.length
                ? events
                    .slice()
                    .reverse()
                    .slice(0, 40)
                    .map(function (event, index) {
                      return h(
                        "div",
                        { className: "ev", key: index },
                        h(
                          "div",
                          null,
                          event.type || "",
                          " ",
                          event.status ? statusChip(event.status) : null,
                        ),
                        h(
                          "div",
                          { className: "meta" },
                          [event.at || event.created_at, event.summary].filter(Boolean).join(" · "),
                        ),
                      );
                    })
                : h("div", { className: "hs-muted" }, "（还没有事件）"),
            ),
          ),
        ),
      );
    }

    function kv(key, value) {
      return [
        h("div", { className: "k", key: "k-" + key }, key),
        h(
          "div",
          { key: "v-" + key },
          value === null || value === undefined || value === "" ? "—" : value,
        ),
      ];
    }

    function LogLine(props) {
      var text = props.text || "";
      var cls = "";
      if (text.indexOf("⚠") === 0) cls = "ln-warn";
      else if (text.indexOf("■") === 0) cls = "ln-end";
      else if (text.indexOf("🔧") === 0 || text.indexOf("▶") === 0) cls = "ln-tool";
      return h("div", { className: cls }, text);
    }

    // -- 主面板 --------------------------------------------------------------
    function Panel() {
      var tasksState = useState([]);
      var tasks = tasksState[0];
      var setTasks = tasksState[1];
      var countState = useState(0);
      var count = countState[0];
      var setCount = countState[1];
      var stateState = useState(null);
      var state = stateState[0];
      var setState = stateState[1];
      var optionsState = useState(null);
      var options = optionsState[0];
      var setOptions = optionsState[1];
      var selectedState = useState(null);
      var selectedId = selectedState[0];
      var setSelectedId = selectedState[1];
      var detailState = useState(null);
      var detail = detailState[0];
      var setDetail = detailState[1];
      var toastState = useState(null);
      var toast = toastState[0];
      var setToast = toastState[1];
      var busyState = useState("");
      var busy = busyState[0];
      var setBusy = busyState[1];
      var filterState = useState("");
      var filter = filterState[0];
      var setFilter = filterState[1];
      var pageState = useState(1);
      var page = pageState[0];
      var setPage = pageState[1];
      var pageSizeState = useState(20);
      var pageSize = pageSizeState[0];
      var setPageSize = pageSizeState[1];
      var pagesState = useState(1);
      var pages = pagesState[0];
      var setPages = pagesState[1];
      var logFilesState = useState([]);
      var logFiles = logFilesState[0];
      var setLogFiles = logFilesState[1];
      var logTextState = useState("");
      var logText = logTextState[0];
      var setLogText = logTextState[1];
      var steerState = useState("");
      var steerText = steerState[0];
      var setSteerText = steerState[1];
      var bannerState = useState(true);
      var bannerShown = bannerState[0];
      var setBannerShown = bannerState[1];
      var followState = useState(true);
      var follow = followState[0];
      var setFollow = followState[1];
      var runModalState = useState(null);
      var runModal = runModalState[0];
      var setRunModal = runModalState[1];
      var newModalState = useState(false);
      var newModal = newModalState[0];
      var setNewModal = newModalState[1];
      var autoModalState = useState(false);
      var autoModal = autoModalState[0];
      var setAutoModal = autoModalState[1];
      var sessionsState = useState(null);
      var sessionModal = sessionsState[0];
      var setSessionModal = sessionsState[1];
      var notifyInfoState = useState(null);
      var notifyInfo = notifyInfoState[0];
      var setNotifyInfo = notifyInfoState[1];
      var logRef = useRef(null);
      var aliveRef = useRef(true);

      var notify = useCallback(function (text, isError) {
        if (aliveRef.current) setToast({ text: text, err: Boolean(isError) });
      }, []);

      var fail = useCallback(
        function (error) {
          notify((error && error.message) || String(error), true);
        },
        [notify],
      );

      var loadState = useCallback(
        function () {
          return apiGet("/api/state")
            .then(function (body) {
              if (!aliveRef.current) return body;
              setState(body);
              return body;
            })
            .catch(fail);
        },
        [fail],
      );

      var loadOptions = useCallback(
        function () {
          return apiPost("/api/studio/options", {})
            .then(function (body) {
              if (aliveRef.current) setOptions(body);
            })
            .catch(fail);
        },
        [fail],
      );

      var loadTasks = useCallback(
        function () {
          var query = "?page=" + page + "&page_size=" + pageSize;
          if (filter) query += "&status=" + q(filter);
          return apiGet("/api/tasks" + query)
            .then(function (body) {
              if (!aliveRef.current) return;
              setTasks(body.tasks || []);
              setCount(body.total || 0);
              setPages(body.pages || 1);
            })
            .catch(fail);
        },
        [fail, filter, page, pageSize],
      );

      // 轮询：状态和执行日志都靠它
      useEffect(function () {
        aliveRef.current = true;
        loadState().then(function (body) {
          if (!body) return;
          return apiPost("/api/studio/options", {});
        }).then(function (body) {
          if (body && aliveRef.current) setOptions(body);
        }).catch(function () {});
        loadTasks();
        var timer = setInterval(function () {
          loadState();
          loadTasks();
        }, 3000);
        return function () {
          aliveRef.current = false;
          clearInterval(timer);
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, [loadState, loadTasks]);

      // 自动滚到最新
      useEffect(
        function () {
          var node = logRef.current;
          var run = state && state.active;
          if (node && follow) node.scrollTop = node.scrollHeight;
        },
        [state && state.active && state.active.log_total, follow],
      );

      var openDetail = useCallback(
        function (taskId) {
          setSelectedId(taskId);
          setLogFiles([]);
          setLogText("");
          setNotifyInfo(null);
          apiGet("/api/tasks/" + q(taskId))
            .then(function (body) {
              if (!aliveRef.current) return null;
              setDetail(body);
              // 通知送达情况单独查：它不是任务单的一部分，而是本机 outbox 的状态。
              return Promise.all([
                apiGet("/api/tasks/" + q(taskId) + "/notify-status").catch(function () {
                  return null;
                }),
                apiGet("/api/logs?task=" + q(taskId)),
              ]);
            })
            .then(function (results) {
              if (!results || !aliveRef.current) return;
              var notify = results[0];
              var logs = results[1];
              if (notify) setNotifyInfo(notify);
              if (logs) setLogFiles(logs.files || []);
            })
            .catch(fail);
        },
        [fail],
      );

      var refreshAll = useCallback(
        function () {
          return Promise.all([loadState(), loadTasks()]).then(function () {
            if (aliveRef.current) setBusy("");
          });
        },
        [loadState, loadTasks],
      );

      var postAction = useCallback(
        function (method, path, payload, done) {
          setBusy("action");
          return apiPost(path, payload)
            .then(function () {
              notify(done, false);
              return refreshAll();
            })
            .catch(fail)
            .then(function () {
              if (aliveRef.current) setBusy("");
            });
        },
        [fail, notify, refreshAll],
      );

      var setMode = useCallback(
        function (mode) {
          setBusy("mode");
          apiPost("/api/mode", { mode: mode })
            .then(function () {
              notify(mode === "auto" ? "已切到全自动" : "已切到手动", false);
              return refreshAll();
            })
            .catch(fail)
            .then(function () {
              if (aliveRef.current) setBusy("");
            });
        },
        [fail, notify, refreshAll],
      );

      var cancelRun = useCallback(
        function () {
          postAction("POST", "/api/runs/cancel", {}, "已请求中断");
        },
        [postAction],
      );

      var steer = useCallback(
        function () {
          var text = steerText.trim();
          if (!text) return;
          setBusy("steer");
          apiPost("/api/runs/steer", { text: text })
            .then(function () {
              setSteerText("");
              notify("已把这句话交给执行器", false);
              return refreshAll();
            })
            .catch(fail)
            .then(function () {
              if (aliveRef.current) setBusy("");
            });
        },
        [fail, notify, refreshAll, steerText],
      );

      var loadLog = useCallback(
        function (attempt) {
          if (!selectedId) return;
          var query = "?task=" + q(selectedId) + "&tail=800";
          if (attempt !== undefined && attempt !== null && String(attempt).match(/^\d+$/)) {
            query += "&attempt=" + attempt;
          }
          setBusy("log");
          apiGet("/api/logs" + query)
            .then(function (body) {
              if (!aliveRef.current) return;
              setLogFiles(body.files || []);
              var files = body.files || [];
              setLogText(files.length ? files[0].content || "" : "");
            })
            .catch(fail)
            .then(function () {
              if (aliveRef.current) setBusy("");
            });
        },
        [fail, selectedId],
      );

      var resumeLast = useCallback(
        function (taskId) {
          setBusy("resume");
          apiPost("/api/tasks/" + q(taskId) + "/run", {
            executor: state && state.default_executor ? state.default_executor : "dsh",
            resume_session_id: "last",
          })
            .then(function () {
              notify("已用上次会话继续执行", false);
              setBannerShown(true);
              return refreshAll();
            })
            .catch(fail)
            .then(function () {
              if (aliveRef.current) setBusy("");
            });
        },
        [fail, notify, refreshAll, state],
      );

      // 通知补发：让投递线程立刻扫一轮 outbox（相当于原面板的 `handoffctl notifier --once`）。
      var flushPump = useCallback(
        function () {
          setBusy("notify");
          apiPost("/api/notify/flush", {})
            .then(function (body) {
              var sent = (body.flush && body.flush.sent) || 0;
              notify(sent ? "补发完成通知：" + sent + " 条已送达" : "补发完成通知：没有可发的事件", false);
              return refreshAll();
            })
            .catch(fail)
            .then(function () {
              if (aliveRef.current) setBusy("");
            });
        },
        [fail, notify, refreshAll],
      );

      var run = state && state.active;
      var mode = (state && state.mode) || "manual";
      var auto = (state && state.auto) || {};
      var engineOk = state && state.executors && state.executors.dsh && state.executors.dsh.ok;
      var deliverInfo = state && state.deliver;
      var advanceInfo = state && state.advance;
      var pumpInfo = state && state.notify_pump;
      var pumpTitle = pumpInfo
        ? (pumpInfo.running ? "完成通知投递线程在跑" : "完成通知投递线程没在跑") +
          "；每 " +
          pumpInfo.interval_seconds +
          "s 扫一次本机 outbox" +
          (pumpInfo.last_error ? "；最近错误：" + pumpInfo.last_error : "；点一下立刻发一轮")
        : "完成通知投递状态未知";

      var progress = "";
      if (advanceInfo && !advanceInfo.done) {
        progress = "恢复推进进行中：" + advanceInfo.task_id + " …";
      } else if (advanceInfo && advanceInfo.done) {
        progress =
          "恢复推进：" + (advanceInfo.result || "done") + (advanceInfo.error ? " ✘ " + advanceInfo.error : "");
        // 推进把任务带到 verified 后，完成通知由后台投递线程发。这里把「还欠几条没发」
        // 直接写在进度里——不然任务显示 verified、前端没收到事件，人看不出还差哪一步。
        if (advanceInfo.result === "verified" && pumpInfo && pumpInfo.pending) {
          progress += "；完成通知还有 " + pumpInfo.pending + " 条待发";
        }
      } else if (deliverInfo && !deliverInfo.done) {
        progress = "验收进行中：" + deliverInfo.task_id + " …";
      } else if (deliverInfo && deliverInfo.done) {
        progress = "上次验收：" + deliverInfo.task_id + (deliverInfo.error ? " ✘ " + deliverInfo.error : " ✔");
      }

      var execInfo = state && state.executors ? state.executors : {};

      return h(
        "div",
        { className: "hs-root" },
        h(Styles),

        // 顶栏：模式 + 健康 + 执行器状态
        h(
          "div",
          { className: "hs-bar" },
          h("span", { className: "hs-title" }, "Handoff 工作台"),
          h("span", {
            className: "hs-dot " + (engineOk ? "on" : "off"),
            title: engineOk ? "DSH 执行器在线" : (state && state.executors && state.executors.dsh && state.executors.dsh.error) || "DSH 执行器不可用",
          }),
          h(
            "div",
            { className: "hs-mode" },
            h(
              "button",
              {
                type: "button",
                className: mode === "manual" ? "active" : "",
                disabled: Boolean(busy),
                onClick: function () {
                  setMode("manual");
                },
              },
              "手动",
            ),
            h(
              "button",
              {
                type: "button",
                className: mode === "auto" ? "active" : "",
                disabled: Boolean(busy),
                onClick: function () {
                  setMode("auto");
                },
              },
              "全自动",
            ),
          ),
          h(
            "button",
            {
              type: "button",
              className: "hs-btn",
              title: "全自动模式配置",
              onClick: function () {
                setAutoModal(true);
              },
            },
            "⚙ 自动配置",
          ),
          // 完成通知的投递线程状态。以前这块完全不可见：任务 verified 了，
          // 前端却收不到事件，只能去 outbox 里数 pending 才知道。
          pumpInfo
            ? h(
                "button",
                {
                  type: "button",
                  className: "hs-btn",
                  title: pumpTitle,
                  disabled: Boolean(busy),
                  onClick: flushPump,
                },
                pumpLabel(pumpInfo),
              )
            : null,
          h("span", { className: "hs-spacer" }),
          h(
            "span",
            { className: "hs-muted", style: { fontSize: 11.5 } },
            Object.keys(execInfo)
              .map(function (key) {
                return key + (execInfo[key] && execInfo[key].ok ? "✓" : "✗");
              })
              .join("  "),
          ),
          progress ? h("span", { className: "hs-muted", style: { fontSize: 11.5 } }, progress) : null,
        ),

        toast
          ? h(
              "div",
              { className: "hs-toast" + (toast.err ? " err" : "") },
              toast.text,
              h(
                "button",
                {
                  type: "button",
                  className: "hs-btn sm",
                  style: { marginLeft: 10 },
                  onClick: function () {
                    setToast(null);
                  },
                },
                "知道了",
              ),
            )
          : null,

        // 执行横幅
        run && bannerShown
          ? h(
              "div",
              { className: "hs-run" },
              h(
                "div",
                { className: "hs-run-head" },
                h("span", {
                  className: "hs-dot " + (run.phase === "running" ? "on" : "off"),
                }),
                h("strong", null, run.phase === "running" ? "执行中" : "本轮已结束"),
                h("span", { className: "hs-muted" }, run.task_id),
                executorChip(run.executor),
                run.error ? h("span", { className: "hs-err" }, run.error) : null,
                h("span", { className: "hs-spacer" }),
                h(
                  "button",
                  {
                    type: "button",
                    className: "hs-btn",
                    title: "关闭后手动滚动查看历史，不会被自动拉到底",
                    onClick: function () {
                      setFollow(!follow);
                    },
                  },
                  follow ? "跟随最新" : "已暂停跟随",
                ),
                run.phase === "running"
                  ? h(
                      "button",
                      {
                        type: "button",
                        className: "hs-btn danger",
                        disabled: Boolean(busy),
                        onClick: cancelRun,
                      },
                      "停止",
                    )
                  : null,
                h(
                  "button",
                  {
                    type: "button",
                    className: "hs-btn",
                    onClick: function () {
                      setBannerShown(false);
                    },
                  },
                  "收起",
                ),
              ),
              h(
                "div",
                { className: "hs-log", ref: logRef },
                (run.logs || []).map(function (line, index) {
                  return h(LogLine, { key: index, text: line });
                }),
              ),
              h(
                "div",
                { className: "hs-steer" },
                h("input", {
                  className: "hs-input",
                  placeholder:
                    "要纠偏就写一句——会打断当前执行，然后带着这句话续跑同一会话（上下文不丢）",
                  value: steerText,
                  onChange: function (event) {
                    setSteerText(event.target.value);
                  },
                  onKeyDown: function (event) {
                    if (event.key === "Enter" && steerText.trim() && !busy) steer();
                  },
                }),
                h(
                  "button",
                  {
                    type: "button",
                    className: "hs-btn primary",
                    disabled: Boolean(busy) || !steerText.trim(),
                    onClick: steer,
                  },
                  "⏸ 打断并补一句",
                ),
              ),
            )
          : null,

        // 工具栏
        h(
          "div",
          { className: "hs-bar", style: { borderBottom: "none" } },
          h(
            "select",
            {
              className: "hs-select",
              value: filter,
              onChange: function (event) {
                setFilter(event.target.value);
                setPage(1);
              },
            },
            h("option", { value: "" }, "全部状态"),
            STATUSES.map(function (name) {
              return h("option", { key: name, value: name }, name);
            }),
          ),
          h("span", { className: "hs-muted" }, "共 " + count + " 单"),
          h("span", { className: "hs-spacer" }),
          h(
            "button",
            {
              type: "button",
              className: "hs-btn",
              title: "从交接仓远端拉最新任务单（本地轮询只读快照，不会自动发现新单）",
              disabled: Boolean(busy),
              onClick: function () {
                setBusy("sync");
                apiPost("/api/sync", {})
                  .then(function (body) {
                    notify("已拉取最新：新增 " + (body.added || 0) + " 单", false);
                    return refreshAll();
                  })
                  .catch(fail)
                  .then(function () {
                    if (aliveRef.current) setBusy("");
                  });
              },
            },
            busy === "sync" ? "拉取中…" : "⟳ 拉取最新",
          ),
          h(
            "button",
            {
              type: "button",
              className: "hs-btn primary",
              disabled: Boolean(busy),
              onClick: function () {
                setNewModal(true);
              },
            },
            "+ 新建任务",
          ),
        ),

        // 表格 + 抽屉
        h(
          "div",
          { className: "hs-wrap" },
          h(
            "div",
            { className: "hs-scroll" },
            tasks.length
              ? h(
                  "table",
                  { className: "hs-table" },
                  h(
                    "thead",
                    null,
                    h(
                      "tr",
                      null,
                      h("th", { style: { width: 90 } }, "状态"),
                      h("th", null, "标题"),
                      h("th", { style: { width: 80 } }, "执行器"),
                      h("th", { style: { width: 64 } }, "尝试"),
                      h("th", { style: { width: 110 } }, "部署"),
                      h("th", { style: { width: 110 } }, "更新时间"),
                    ),
                  ),
                  h(
                    "tbody",
                    null,
                    tasks.map(function (task) {
                      return h(
                        "tr",
                        {
                          key: task.id,
                          className: selectedId === task.id ? "sel" : "",
                          onClick: function () {
                            openDetail(task.id);
                          },
                        },
                        h("td", null, statusChip(task.status)),
                        h(
                          "td",
                          null,
                          h("div", null, task.title || "（无标题）"),
                          h("div", { className: "hs-id hs-muted" }, task.id),
                          task.failure
                            ? h("div", { className: "hs-fail" }, task.failure.message || task.failure.stage || "")
                            : null,
                        ),
                        h(
                          "td",
                          null,
                          executorChip(task.executor),
                          task.frontend_executor ? executorChip("fe-" + task.frontend_executor) : null,
                        ),
                        h(
                          "td",
                          null,
                          task.attempt === null || task.attempt === undefined ? "—" : task.attempt,
                        ),
                        h("td", { className: "hs-muted" }, task.deployment || "—"),
                        h("td", { className: "hs-muted" }, fmtTime(task.updated_at)),
                      );
                    }),
                  ),
                )
              : h("div", { className: "hs-empty" }, "没有任务"),
          ),

          // 分页
          h(
            "div",
            { className: "hs-pager" },
            h(
              "button",
              {
                type: "button",
                className: "hs-btn sm",
                disabled: page <= 1 || Boolean(busy),
                onClick: function () {
                  setPage(Math.max(1, page - 1));
                },
              },
              "‹ 上一页",
            ),
            h("span", null, "第 " + page + " / " + pages + " 页"),
            h(
              "button",
              {
                type: "button",
                className: "hs-btn sm",
                disabled: page >= pages || Boolean(busy),
                onClick: function () {
                  setPage(Math.min(pages, page + 1));
                },
              },
              "下一页 ›",
            ),
            h("span", { className: "hs-spacer" }),
            h(
              "select",
              {
                className: "hs-select",
                value: String(pageSize),
                onChange: function (event) {
                  setPageSize(Number(event.target.value));
                  setPage(1);
                },
              },
              [10, 20, 50, 100].map(function (size) {
                return h("option", { key: size, value: String(size) }, size + " 条/页");
              }),
            ),
            h(
              "button",
              {
                type: "button",
                className: "hs-btn sm",
                disabled: Boolean(busy),
                onClick: function () {
                  setBusy("reload");
                  refreshAll();
                },
              },
              busy === "reload" ? "刷新中…" : "刷新",
            ),
          ),

          detail
            ? h(Drawer, {
                detail: detail,
                run: run,
                notifyInfo: notifyInfo,
                logFiles: logFiles,
                logText: logText,
                progress: progress,
                onBlocked: function (reason) {
                  notify(reason, true);
                },
                onLog: loadLog,
                onOpenRun: function (taskId) {
                  loadOptions();
                  setRunModal(taskId);
                },
                onResumeLast: resumeLast,
                onAction: function (method, path, payload, done) {
                  postAction(method, path, payload, done).then(function () {
                    openDetail(detail.task.id);
                  });
                },
                onClose: function () {
                  setDetail(null);
                  setSelectedId(null);
                },
              })
            : null,
        ),

        runModal
          ? h(RunModal, {
              taskId: runModal,
              options: options || (state && state.executor_options) || { executors: [] },
              onClose: function () {
                setRunModal(null);
              },
              onDone: function () {
                setRunModal(null);
                notify("已开始执行", false);
                setBannerShown(true);
                refreshAll();
              },
            })
          : null,

        newModal
          ? h(NewTaskModal, {
              options: options || {},
              onClose: function () {
                setNewModal(false);
              },
              onDone: function (body) {
                setNewModal(false);
                notify("已创建：" + (body.id || "任务"), false);
                refreshAll().then(function () {
                  if (body.id) openDetail(body.id);
                });
              },
            })
          : null,

        autoModal
          ? h(AutoConfigModal, {
              options: options || {},
              auto: auto,
              mode: mode,
              onClose: function () {
                setAutoModal(false);
              },
              onDone: function () {
                setAutoModal(false);
                notify("自动配置已保存", false);
                refreshAll();
              },
            })
          : null,

        sessionModal
          ? h(SessionModal, {
              executor: sessionModal.executor,
              sessions: sessionModal.sessions,
              selected: sessionModal.selected,
              onPick: function (id) {
                var callback = sessionModal.onPick;
                setSessionModal(null);
                if (callback) callback(id);
              },
              onClose: function () {
                setSessionModal(null);
              },
            })
          : null,
      );
    }

    // -- 侧边栏图标 ----------------------------------------------------------
    function StudioIcon(props) {
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
        h("rect", { x: 2.75, y: 4, width: 18.5, height: 16, rx: 2.75 }),
        h("path", { d: "M6.4 9.2h5.1" }),
        h("path", { d: "M9.6 6.6l2.7 2.6-2.7 2.6" }),
        h("path", { d: "M6.4 15.4h8.2" }),
      );
    }

    // -- 注册 ----------------------------------------------------------------
    function step(label, run) {
      try {
        return run();
      } catch (error) {
        console.error("[handoff-studio] " + label + " failed", error);
        return undefined;
      }
    }

    function apply(ctx) {
      step("sidebar entry", function () {
        return ctx.slots.inject("sidebar.panellist", function () {
          return ctx.slots.register(
            { name: "sidebar.panellist", id: PANEL_ID, order: 26, label: LABEL },
            StudioIcon,
          );
        });
      });
      step("main panel", function () {
        return ctx.slots.inject("main", function () {
          return ctx.slots.register({ name: "main", key: PANEL_ID }, Panel);
        });
      });
    }

    exports.apply = apply;
    exports.inject = ["slots"];
    exports.name = "dsh-handoff-studio-client";
    // 仅供测试的出口：弹窗平时要靠人点开才挂载，测试没法从 Panel 里摸到它们。
    // 导出的都是纯组件，不改变宿主侧的加载行为。
    exports.__test = {
      AutoConfigModal: AutoConfigModal,
      RunModal: RunModal,
      Drawer: Drawer,
      IsolationPicker: IsolationPicker,
      SandboxPicker: SandboxPicker,
      FallbackPicker: FallbackPicker,
      effortsFor: effortsFor,
      pumpLabel: pumpLabel,
      // 真实样式表文本，供渲染验证用（页面里是 <style>{CSS}</style>）。
      // CSS 在声明处就已经 join 成字符串了，这里不要再 join。
      css: CSS,
    };

    return module.exports;
  },
});

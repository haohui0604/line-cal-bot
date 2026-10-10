/* 会員画面(/me)とトレーナー画面で共用するグラフ描画 (Phase 9).
 *
 * - CalCharts.weight / CalCharts.weightText : 体重推移 + 現在値/増減のテキスト
 * - CalCharts.calorie                       : 摂取/消費の棒グラフ + 目標線（摂取側）
 *
 * 目標線は「摂取カロリーのグラフの上」に重ねる（消費側には付けない）。
 */
(function () {
  function num(v) {
    if (v === null || v === undefined || v === "" || isNaN(v)) return null;
    return Number(v);
  }
  function fmt(v) {
    const n = num(v);
    return n === null ? 0 : Math.round(n).toLocaleString("ja-JP");
  }

  /* maintainAspectRatio:false のチャートは親の高さに追従するため、
     高さを持たない親に置くとキャンバスが伸び続ける。固定高さの箱で包む。 */
  function _fixBox(canvas, h) {
    if (!canvas || !canvas.parentNode) return;
    var box = canvas.parentElement;
    if (!box || String(box.className || "").indexOf("chart-box") < 0) {
      box = document.createElement("div");
      box.className = "chart-box";
      canvas.parentNode.insertBefore(box, canvas);
      box.appendChild(canvas);
    }
    box.style.position = "relative";
    box.style.width = "100%";
    box.style.height = h + "px";
    box.style.maxHeight = h + "px";
    canvas.style.maxHeight = h + "px";
  }

  /* 軸の共通スタイル: 目盛りは間引き・回転なし、グリッドは薄く */
  const AX = {
    x: { grid: { display: false },
         ticks: { maxRotation: 0, minRotation: 0, autoSkip: true,
                  maxTicksLimit: 7, font: { size: 10 } } },
    y: { grid: { color: "#eef2f7", drawTicks: false },
         border: { display: false },
         ticks: { maxTicksLimit: 5, font: { size: 10 }, padding: 4 } }
  };
  const LEG = { legend: { labels: { boxWidth: 12, font: { size: 11 }, padding: 8 } } };
  if (window.Chart) {
    try { Chart.defaults.font.size = 11; Chart.defaults.color = "#6b7280"; } catch (e) {}
  }

  function _mergeSeries(sources) {

    // 実測→繰越→推定 の優先順で 1 本の系列にまとめ、点ごとの見た目を返す

    var n = 0, k, i2;

    for (k = 0; k < sources.length; k++) { n = Math.max(n, (sources[k].data || []).length); }

    var value = [], color = [], radius = [], bg = [], bw = [];

    for (i2 = 0; i2 < n; i2++) {

      value.push(null); color.push(null); radius.push(0); bg.push(null); bw.push(0);

      for (k = 0; k < sources.length; k++) {

        var arr = sources[k].data || [];

        if (arr[i2] === null || arr[i2] === undefined) { continue; }

        value[i2] = arr[i2]; color[i2] = sources[k].color; radius[i2] = sources[k].radius;

        bg[i2] = sources[k].hollow ? "#ffffff" : sources[k].color;

        bw[i2] = sources[k].hollow ? 2 : 0;

        break;

      }

    }

    return { value: value, color: color, radius: radius, bg: bg, bw: bw };

  }


  window.CalCharts = {
    /* 体重推移: 変動が見えるよう y 軸を min-10% 〜 max+10% に絞る。
       目標体重があれば赤い破線を重ねる。 */
    weight: function (canvas, d, opt) {
      opt = opt || {};
      const tw = num(d.target_weight);
      const vals = (d.weight || []).filter(function (v) { return v != null; });
      const opts = { responsive: true, plugins: LEG,
                     scales: { x: AX.x, y: Object.assign({}, AX.y) } };
      if (vals.length) {
        let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
        if (tw != null) { lo = Math.min(lo, tw); hi = Math.max(hi, tw); }
        const pad = (hi - lo) * 0.1 || hi * 0.02 || 1;
        opts.scales.y.min = +(lo - pad).toFixed(1);
        opts.scales.y.max = +(hi + pad).toFixed(1);
      }
      const ds = [{
        label: "体重 kg", data: d.weight, borderColor: "#3b82f6",
        backgroundColor: "#3b82f6", tension: .3, pointRadius: 1.5,
        pointHoverRadius: 4, spanGaps: true
      }];
      if (tw != null) {
        ds.push({
          label: "目標 " + tw.toFixed(1) + " kg",
          data: (d.weight || []).map(function () { return tw; }),
          borderColor: "#ef4444", borderDash: [6, 4], borderWidth: 2,
          pointRadius: 0, fill: false, tension: 0
        });
      }
      return new Chart(canvas, {
        type: "line", data: { labels: d.weight_labels, datasets: ds },
        options: opts
      });
    },

    /* 現在の体重・増減（下回る場合は必ず「−」表記）・目標体重 */
    weightText: function (ids, d) {
      // __goal_info__: 目標の設定日・期限・残り・ペース
      try {
        var _gi = document.getElementById("wGoalInfo");
        if (_gi) {
          var _p = [];
          if (d && d.goal_set_at) {
            _p.push("設定日: " + d.goal_set_at + (d.goal_set_at_estimated ? "（推定）" : ""));
          }
          if (d && d.goal_target_date) {
            var _l = "期限: " + d.goal_target_date;
            if (typeof d.goal_days_left === "number") {
              _l += (d.goal_days_left >= 0
                ? "（あと " + d.goal_days_left + "日）"
                : "（" + Math.abs(d.goal_days_left) + "日超過）");
            }
            _p.push(_l);
          }
          if (d && typeof d.goal_kg_left === "number") {
            _p.push(d.goal_kg_left > 0
              ? "残り " + d.goal_kg_left.toFixed(1) + " kg"
              : "目標達成（" + Math.abs(d.goal_kg_left).toFixed(1) + " kg 超過）");
          }
          if (d && typeof d.goal_pace_week === "number") {
            _p.push("ペース " + d.goal_pace_week.toFixed(2) + " kg/週");
          }
          var _lines = [[_p[0]], [_p[1]], _p.slice(2)].filter(function (a) { return a && a.length; });
          _gi.innerHTML = _lines.map(function (a) {
            return '<span class="gi-line">' + a.join('<span class="gi-sep">／</span>') + '</span>';
          }).join('');
          if (d && d.goal_overdue && !d.goal_achieved) _gi.style.color = "#ef4444";
        }
      } catch (e) {}

      ids = ids || {};
      function set(id, txt, color) {
        const e = document.getElementById(id);
        if (!e) return;
        e.textContent = txt;
        if (color) e.style.color = color;
      }
      const cur = num(d.weight_current);
      set(ids.current || "wCurrent", cur == null ? "—" : cur.toFixed(1) + " kg");
      set(ids.currentDate || "wCurrentDate",
          d.weight_current_date ? "(" + d.weight_current_date + ")" : "");
      const dl = num(d.weight_delta);
      if (dl == null) {
        set(ids.delta || "wDelta", "—", "");
      } else {
        const s = (dl > 0 ? "+" : dl < 0 ? "−" : "±")
                + Math.abs(dl).toFixed(1) + " kg";
        set(ids.delta || "wDelta", s,
            dl > 0 ? "#ef4444" : (dl < 0 ? "#10b981" : "inherit"));
      }
      set(ids.baseDate || "wBaseDate",
          d.weight_base_date ? "基準 " + d.weight_base_date : "");
      const tw = num(d.target_weight);
      const wrap = document.getElementById(ids.targetWrap || "wTargetWrap");
      if (wrap) wrap.style.display = (tw == null ? "none" : "");
      set(ids.target || "wTarget", tw == null ? "" : tw.toFixed(1) + " kg");
    },

    /* 摂取/消費の棒グラフ + 目標摂取カロリーの破線（摂取バーの上に重なる） */
    calorie: function (canvas, d) {
      const target = num(d.target_kcal);
      const ds = [
        { type: "bar", label: "摂取 kcal", data: d.intake, order: 2,
          backgroundColor: "#06c75566", borderColor: "#06c755", borderWidth: 1 },
        { type: "bar", label: "消費 kcal", data: d.burn, order: 2,
          backgroundColor: "#f59e0b66", borderColor: "#f59e0b", borderWidth: 1 }
      ];
      if (target != null) {
        ds.push({
          type: "line", label: "目標摂取 " + fmt(target) + " kcal", order: 1,
          data: (d.labels || []).map(function () { return target; }),
          borderColor: "#ef4444", borderDash: [6, 4], borderWidth: 2,
          pointRadius: 0, fill: false, tension: 0
        });
      }
      return new Chart(canvas, {
        data: { labels: d.labels, datasets: ds },
        options: {
          responsive: true, interaction: { mode: "index" },
          plugins: LEG, scales: { x: AX.x, y: AX.y }
        }
      });
    },

    /* 体脂肪率の推移: 実測=実線 / 繰越=破線 / BMIからの推定=点線 */
    bodyFat: function (canvas, d) {
      const labels = (d && d.bc_labels) || [];
      if (!labels.length) return null;
      const _m = _mergeSeries([
        { data: d.bc_body_fat,       color: "#7c3aed", radius: 4.5 },
        { data: d.bc_body_fat_carry, color: "#a78bfa", radius: 3, hollow: true },
        { data: d.bc_body_fat_est,   color: "#c4b5fd", radius: 2 }
      ]);
      if (!_m.value.some(function (v) { return v !== null; })) return null;
      _fixBox(canvas, 160);
      return new Chart(canvas, {
        type: "line",
        data: { labels: labels, datasets: [{
          label: "体脂肪率 %", data: _m.value,
          borderColor: "#7c3aed", backgroundColor: "#7c3aed", borderWidth: 2,
          tension: .3, spanGaps: true, fill: false,
          pointBackgroundColor: _m.bg, pointBorderColor: _m.color, pointBorderWidth: _m.bw,
          pointRadius: _m.radius, pointHoverRadius: 5
        }] },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: true, position: "bottom",
            labels: { usePointStyle: true, boxWidth: 9, boxHeight: 9, padding: 10, font: { size: 11 },
              generateLabels: function () { return [
                { text: "実測", fillStyle: "#7c3aed", strokeStyle: "#7c3aed", lineWidth: 0,
                  pointStyle: "circle", boxWidth: 11, boxHeight: 11 },
                { text: "繰越", fillStyle: "#ffffff", strokeStyle: "#a78bfa", lineWidth: 2,
                  pointStyle: "circle", boxWidth: 8, boxHeight: 8 },
                { text: "推定（BMIから）", fillStyle: "#c4b5fd", strokeStyle: "#c4b5fd", lineWidth: 0,
                  pointStyle: "circle", boxWidth: 6, boxHeight: 6 }
              ]; } } },
            onClick: function () {} },
          scales: { x: AX.x,
            y: { grid: { color: "#eef2f7", drawTicks: false }, border: { display: false },
                 ticks: { maxTicksLimit: 4, font: { size: 10 }, padding: 4,
                          callback: function (v) { return v + "%"; } } } }
        }
      });
    },


    muscle: function (canvas, d) {
      const labels = (d && d.bc_labels) || [];
      if (!labels.length) return null;
      const _m = _mergeSeries([
        { data: d.bc_muscle_measured, color: "#0284c7", radius: 4.5 },
        { data: d.bc_muscle_carry,    color: "#7dd3fc", radius: 3, hollow: true },
        { data: d.bc_muscle_estimated, color: "#94a3b8", radius: 2 }
      ]);
      if (!_m.value.some(function (v) { return v !== null; })) return null;
      _fixBox(canvas, 170);
      return new Chart(canvas, {
        type: "line",
        data: { labels: labels, datasets: [{
          label: "筋肉量 kg", data: _m.value,
          borderColor: "#0284c7", backgroundColor: "#0284c7", borderWidth: 2,
          tension: .3, spanGaps: true, fill: false,
          pointBackgroundColor: _m.bg, pointBorderColor: _m.color, pointBorderWidth: _m.bw,
          pointRadius: _m.radius, pointHoverRadius: 5
        }] },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: true, position: "bottom",
            labels: { usePointStyle: true, boxWidth: 9, boxHeight: 9, padding: 10, font: { size: 11 },
              generateLabels: function () { return [
                { text: "実測", fillStyle: "#0284c7", strokeStyle: "#0284c7", lineWidth: 0,
                  pointStyle: "circle", boxWidth: 11, boxHeight: 11 },
                { text: "繰越", fillStyle: "#ffffff", strokeStyle: "#7dd3fc", lineWidth: 2,
                  pointStyle: "circle", boxWidth: 8, boxHeight: 8 },
                { text: "推定", fillStyle: "#94a3b8", strokeStyle: "#94a3b8", lineWidth: 0,
                  pointStyle: "circle", boxWidth: 6, boxHeight: 6 }
              ]; } } },
            onClick: function () {} },
          scales: { x: AX.x,
            y: { grid: { color: "#eef2f7", drawTicks: false }, border: { display: false },
                 ticks: { maxTicksLimit: 4, font: { size: 10 }, padding: 4,
                          callback: function (v) { return v + "kg"; } } } }
        }
      });
    },

    /* 体脂肪率・BMI・筋肉量のテキスト（下回る場合は必ず「−」表記） */
    bodyCompText: function (d, ids) {
      function set(id, v) { var e = document.getElementById(id); if (e) { e.textContent = v; } }
      var f = (d && typeof d.bc_fat_current === "number") ? d.bc_fat_current : null;
      set("bcFat", f === null ? "—" : f.toFixed(1) + " %");
      var fd = (d && typeof d.bc_fat_delta === "number") ? d.bc_fat_delta : null;
      var de = document.getElementById("bcFatDelta");
      if (de) {
        if (fd === null) { de.textContent = ""; }
        else {
          de.textContent = "（" + (d.bc_fat_source === "measured" ? "実測" : "推定") + " "
            + String(d.bc_fat_last_date || "").slice(5) + "・"
            + (fd > 0 ? "+" : fd < 0 ? "−" : "±") + Math.abs(fd).toFixed(1) + " %）";
          de.style.color = fd < 0 ? "#10b981" : (fd > 0 ? "#ef4444" : "");
        }
      }
      var b = (d && typeof d.bc_bmi_current === "number") ? d.bc_bmi_current : null;
      set("bcBmi", b === null ? "—" : b.toFixed(1));
      var cat = (d && d.bc_bmi_category) ? d.bc_bmi_category : "";
      var tg = (d && typeof d.bc_bmi_target === "number") ? "・目標 " + d.bc_bmi_target.toFixed(1) : "";
      var ce = document.getElementById("bcBmiCat");
      if (ce) {
        ce.textContent = (cat || tg) ? "（" + cat + tg + "）" : "";
        ce.style.color = (b !== null && b >= 25) ? "#ef4444" : "";
      }
      var mu = (d && typeof d.bc_muscle_measured_current === "number") ? d.bc_muscle_measured_current : null;
      set("bcMuscle", mu === null ? "—" : mu.toFixed(1) + " kg");
      var fm = (d && typeof d.bc_fat_mass_current === "number") ? d.bc_fat_mass_current : null;
      set("bcFatMass", fm === null ? "—" : fm.toFixed(1) + " kg");
      var n = document.getElementById("bcNote");
      if (n) {
        n.textContent = "入力があれば実測値、入力がなければ性別・年齢・身長から推定した値を表示しています（推定値は目安です）。";
      }
    },

    /* 「+1,234 kcal / −567 kcal」の符号付き表記 */
    signedKcal: function (v) {
      const n = num(v);
      if (n === null) return "—";
      const r = Math.round(n);
      return (r > 0 ? "+" : r < 0 ? "−" : "±")
           + Math.abs(r).toLocaleString("ja-JP") + " kcal";
    }
  };
})();

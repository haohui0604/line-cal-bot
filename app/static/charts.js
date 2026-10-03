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

  window.CalCharts = {
    /* 体重推移: 変動が見えるよう y 軸を min-10% 〜 max+10% に絞る。
       目標体重があれば赤い破線を重ねる。 */
    weight: function (canvas, d, opt) {
      opt = opt || {};
      const tw = num(d.target_weight);
      const vals = (d.weight || []).filter(function (v) { return v != null; });
      const opts = { responsive: true };
      if (vals.length) {
        let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
        if (tw != null) { lo = Math.min(lo, tw); hi = Math.max(hi, tw); }
        const pad = (hi - lo) * 0.1 || hi * 0.02 || 1;
        opts.scales = { y: { min: +(lo - pad).toFixed(1),
                             max: +(hi + pad).toFixed(1) } };
      }
      const ds = [{
        label: "体重 kg", data: d.weight, borderColor: "#3b82f6",
        backgroundColor: "#3b82f6", tension: .3, pointRadius: 2, spanGaps: true
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
          plugins: { legend: { labels: { boxWidth: 12, font: { size: 11 } } } }
        }
      });
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

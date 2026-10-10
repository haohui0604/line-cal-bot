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

    /* 体脂肪率の推移: 実測=実線 / 繰越=破線 / BMIからの推定=点線 */
    bodyFat: function (canvas, d) {
      const labels = (d && d.bc_labels) || [];
      if (!labels.length) return null;
      const ds = [];
      if (d.bc_has_fat) {
        ds.push({
          label: "体脂肪率 %（実測）", data: d.bc_body_fat,
          borderColor: "#8b5cf6", backgroundColor: "#8b5cf6",
          borderWidth: 2, tension: .3, pointRadius: 3, spanGaps: false
        });
      }
      if (d.bc_has_fat_carry) {
        ds.push({
          label: "体脂肪率 %（繰越）", data: d.bc_body_fat_carry,
          borderColor: "#a78bfa", backgroundColor: "#a78bfa",
          borderWidth: 2, borderDash: [6, 4], tension: .3,
          pointRadius: 2, spanGaps: false
        });
      }
      if (d.bc_has_fat_est) {
        ds.push({
          label: "体脂肪率 %（BMIからの推定）", data: d.bc_body_fat_est,
          borderColor: "#c4b5fd", backgroundColor: "#c4b5fd",
          borderWidth: 2, borderDash: [2, 3], tension: .3,
          pointRadius: 0, spanGaps: false
        });
      }
      if (!ds.length) return null;
      return new Chart(canvas, {
        type: "line",
        data: { labels: labels, datasets: ds },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: true,
            labels: { boxWidth: 12, font: { size: 11 } } } },
          scales: { y: { ticks: { callback: function (v) { return v + "%"; } } } }
        }
      });
    },

    /* 筋肉量の推移: 実測=実線 / 繰越=破線 / 除脂肪量の推定=点線 */
    muscle: function (canvas, d) {
      const labels = (d && d.bc_labels) || [];
      if (!labels.length) return null;
      const ds = [];
      if (d.bc_has_muscle_measured) {
        ds.push({
          label: "筋肉量 kg（実測）", data: d.bc_muscle_measured,
          borderColor: "#0ea5e9", backgroundColor: "#0ea5e9",
          borderWidth: 2, tension: .3, pointRadius: 3, spanGaps: false
        });
      }
      if (d.bc_has_muscle_carry) {
        ds.push({
          label: "筋肉量 kg（繰越）", data: d.bc_muscle_carry,
          borderColor: "#7dd3fc", backgroundColor: "#7dd3fc",
          borderWidth: 2, borderDash: [6, 4], tension: .3,
          pointRadius: 2, spanGaps: false
        });
      }
      if (d.bc_has_muscle_estimated) {
        ds.push({
          label: "除脂肪量（推定）", data: d.bc_muscle_estimated,
          borderColor: "#94a3b8", backgroundColor: "#94a3b8",
          borderWidth: 2, borderDash: [2, 3], tension: .3,
          pointRadius: 0, spanGaps: false
        });
      }
      if (!ds.length) return null;
      return new Chart(canvas, {
        type: "line",
        data: { labels: labels, datasets: ds },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: true,
            labels: { boxWidth: 12, font: { size: 11 } } } },
          scales: { y: { ticks: { callback: function (v) { return v + "kg"; } } } }
        }
      });
    },

    /* 体脂肪率・BMI・筋肉量のテキスト（下回る場合は必ず「−」表記） */
    bodyCompText: function (d, ids) {
      ids = ids || {};
      d = d || {};
      function set(id, txt, color) {
        const e = document.getElementById(id);
        if (!e) return;
        e.textContent = txt;
        if (color) e.style.color = color;
      }
      function f1(v) { const n = num(v); return n === null ? null : n.toFixed(1); }
      function signed(v, unit) {
        const n = num(v);
        if (n === null) return "—";
        return (n > 0 ? "+" : n < 0 ? "−" : "±")
             + Math.abs(n).toFixed(1) + " " + unit;
      }
      const fat = num(d.bc_fat_current);
      const src = d.bc_fat_source;
      let tag = "";
      if (fat !== null) {
        if (src === "measured") {
          tag = "（実測 " + (d.bc_fat_last_date || "") + "・"
              + signed(d.bc_fat_delta, "%") + "）";
        } else if (src === "carry") {
          tag = "（繰越・最終実測 " + (d.bc_fat_last_date || "") + "）";
        } else {
          tag = "（BMIからの推定）";
        }
      }
      set(ids.fat || "bcFat", fat === null ? "—" : fat.toFixed(1) + " %", "");
      set(ids.fatDelta || "bcFatDelta", tag,
          src === "measured" ? (num(d.bc_fat_delta) > 0 ? "#ef4444"
            : (num(d.bc_fat_delta) < 0 ? "#10b981" : "")) : "");

      const bmi = num(d.bc_bmi_current);
      set(ids.bmi || "bcBmi", bmi === null ? "—" : bmi.toFixed(1), "");
      set(ids.bmiCat || "bcBmiCat",
          bmi === null ? "" : "（" + (d.bc_bmi_category || "")
            + (num(d.bc_bmi_target) !== null ? "・目標 " + num(d.bc_bmi_target).toFixed(1) : "")
            + "）",
          bmi !== null && bmi >= 25 ? "#ef4444" : "");

      const mm = num(d.bc_muscle_measured_current);
      set(ids.muscleMeasured || "bcMuscle",
          mm === null ? "—" : mm.toFixed(1) + " kg", "");
      const me = num(d.bc_lean_current !== undefined ? d.bc_lean_current
                                                     : d.bc_muscle_estimated_current);
      set(ids.muscleEstimated || "bcEst", me === null ? "—" : me.toFixed(1) + " kg", "");
      const fm = num(d.bc_fat_mass_current);
      set(ids.fatMass || "bcFatMass", fm === null ? "—" : fm.toFixed(1) + " kg", "");

      const note = document.getElementById(ids.note || "bcNote");
      if (note) {
        const parts = [];
        if (fat === null) {
          parts.push("体脂肪率の記録がまだありません。体組成計の写真を送るか、"
                     + "「体重 72 体脂肪18」の形式で送ると表示されます。");
          if (!d.bc_has_body) {
            parts.push("※ 身長・年齢・性別が未登録のためBMIからの推定もできません。"
                       + "LINEで「身体情報」と送ると登録できます。");
          }
        } else {
          parts.push("体脂肪率: 実線＝実測、破線＝直前の実測の繰越、"
                     + "点線＝BMI・年齢・性別からの推定（誤差 ±4% 程度）。");
          parts.push("除脂肪量（推定）＝体重×(1−体脂肪率)で、脂肪以外のすべて"
                     + "（骨・内臓・水分）を含むため、体組成計の筋肉量より大きく出ます。");
          if (!d.bc_has_muscle_measured) {
            parts.push("体組成計の筋肉量は未記録です。");
          }
        }
        note.textContent = parts.join(" ");
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

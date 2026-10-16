/* Trade planner maths. A calculator, not a prediction.
 *
 * It turns the support/resistance levels found on the chart into four example
 * setups (2 bullish, 2 bearish) with entry, stop loss, targets, reward:risk
 * and position size. If you enter an option premium it also estimates the
 * option's stop and target using delta. That estimate ignores time decay,
 * volatility changes and gaps, so treat it as a rough guide only.
 */
(function (root) {
  "use strict";

  const f = (n) => Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const pct = (a, b) => (Math.abs(a - b) / b * 100).toFixed(2) + "%";

  function build(input) {
    const price = Number(input.price);
    if (!(price > 0)) return { setups: [], reason: "Waiting for a price." };

    const atr = Number(input.atr) > 0 ? Number(input.atr) : price * 0.004;
    const levels = input.levels || [];
    const res = levels.filter((l) => l.kind === "resistance").map((l) => l.price).sort((a, b) => a - b);
    const sup = levels.filter((l) => l.kind === "support").map((l) => l.price).sort((a, b) => b - a);
    if (!res.length && !sup.length) {
      return { setups: [], reason: "No support or resistance levels found yet on this chart." };
    }

    const riskAmount = (Number(input.capital) || 0) * (Number(input.riskPct) || 0) / 100;
    const buf = Math.max(0.25 * atr, price * 0.001);      // small gap so orders actually fill
    const gap = Math.max(0.5 * atr, price * 0.0015);      // stop sits beyond the level by this much
    const premium = Number(input.premium) || 0;
    const lot = Math.floor(Number(input.lot)) || 0;
    const delta = Math.min(1, Math.max(0.05, Number(input.delta) || 0.5));
    const isStock = input.kind === "EQ";

    const setups = [];

    // state: "active" (price is in the entry zone), "waiting", "late" (breakout already ran) or "failed"
    function make(id, side, title, why, entry, stop, rawTargets, status, state) {
      const risk = Math.abs(entry - stop);
      if (!(risk > 0)) return;
      const dir = side === "bull" ? 1 : -1;

      let targets = rawTargets
        .filter((t) => (side === "bull" ? t > entry : t < entry))
        .map((p, i) => ({ label: "Target " + (i + 1), price: p }));
      if (!targets.length) {
        targets = [
          { label: "Target 1 (2x risk)", price: entry + dir * 2 * risk },
          { label: "Target 2 (3x risk)", price: entry + dir * 3 * risk },
        ];
      }
      targets = targets.slice(0, 2);

      const rr = Math.abs(targets[0].price - entry) / risk;
      const flags = [];
      if (rr < 1) flags.push("Reward is smaller than the risk, so you would need to win more than half the time just to break even. Most traders skip setups like this.");
      else if (rr < 1.5) flags.push("Reward is under 1.5x the risk. Many traders skip setups like this.");

      let option = null;
      if (premium > 0 && lot > 0) {
        const rawStop = premium - delta * risk;
        const stopPremium = Math.max(rawStop, 0);
        const oneLotRisk = (premium - stopPremium) * lot;
        const lots = oneLotRisk > 0 ? Math.floor(riskAmount / oneLotRisk) : 0;
        option = {
          type: side === "bull" ? "CALL" : "PUT",
          stopPremium,
          stopBeyondPremium: rawStop <= 0,
          targetPremiums: targets.map((t) => premium + delta * Math.abs(t.price - entry)),
          oneLotRisk,
          oneLotCost: premium * lot,
          lots,
          cost: lots * lot * premium,
        };
        if (lots === 0) {
          flags.push("One lot risks about " + f(oneLotRisk) + ", more than your limit of " + f(riskAmount) + ".");
        }
      }

      setups.push({
        id, side, title, why, entry, stop, targets, risk, rr, flags, status, option, state,
        dist: Math.abs(price - entry) / price,
        qty: isStock && riskAmount > 0 ? Math.floor(riskAmount / risk) : null,
        riskAmount,
      });
    }

    if (sup.length) {
      const s1 = sup[0];
      const entry = s1 + buf;
      const stop = s1 - gap;
      let status, state;
      if (price < stop) { status = "Price is already below the stop, so this setup has failed. Wait for new levels."; state = "failed"; }
      else if (price <= entry * 1.002) { status = "Price is at or near the entry zone now."; state = "active"; }
      else { status = "Wait for price to fall to " + f(entry) + " (" + pct(price, entry) + " below now)."; state = "waiting"; }
      make("pb", "bull", "Buy the dip at support",
        "Price has turned up from this area before. Entry just above support, stop just below it.",
        entry, stop, res.slice(0, 2), status, state);
    }

    if (res.length) {
      const r1 = res[0];
      const entry = r1 + buf;
      const stop = r1 - gap;
      const late = price >= entry;
      const status = late
        ? "Price is already above the entry. Chasing a breakout that has moved is risky."
        : "Wait for price to trade above " + f(entry) + " (" + pct(entry, price) + " above now).";
      make("bo", "bull", "Buy the breakout above resistance",
        "Enter only if price pushes through resistance. Stop goes back inside the old range.",
        entry, stop, res.slice(1, 2), status, late ? "late" : "waiting");
    }

    if (res.length) {
      const r1 = res[0];
      const entry = r1 - buf;
      const stop = r1 + gap;
      let status, state;
      if (price > stop) { status = "Price is already above the stop, so this setup has failed. Wait for new levels."; state = "failed"; }
      else if (price >= entry * 0.998) { status = "Price is at or near the entry zone now."; state = "active"; }
      else { status = "Wait for price to rise to " + f(entry) + " (" + pct(entry, price) + " above now)."; state = "waiting"; }
      make("rs", "bear", "Sell the rally at resistance",
        "Price has stalled here before. Entry just below resistance, stop just above it.",
        entry, stop, sup.slice(0, 2), status, state);
    }

    if (sup.length) {
      const s1 = sup[0];
      const entry = s1 - buf;
      const stop = s1 + gap;
      const late = price <= entry;
      const status = late
        ? "Price is already below the entry. Chasing a breakdown that has moved is risky."
        : "Wait for price to trade below " + f(entry) + " (" + pct(price, entry) + " below now).";
      make("bd", "bear", "Sell the breakdown below support",
        "Enter only if price falls through support. Stop goes back inside the old range.",
        entry, stop, sup.slice(1, 2), status, late ? "late" : "waiting");
    }

    // setups sitting in their entry zone first, then the nearest ones, then moved-on and failed ones
    const rank = { active: 0, waiting: 1, late: 2, failed: 3 };
    setups.sort((a, b) => (rank[a.state] - rank[b.state]) || (a.dist - b.dist));

    return { setups, reason: setups.length ? "" : "Could not build a setup from the current levels." };
  }

  const api = { build };
  root.Planner = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);

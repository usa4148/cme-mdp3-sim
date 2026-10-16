/*
 * The dashboard's in-browser engine: a step-for-step port of engine.py driven
 * by a port of CPython's `random.Random`, so a preview in the page is the same
 * walk, the same book and the same trades the Python simulator produces for the
 * same seed. Moving a slider re-runs it instantly; the server then re-runs the
 * session through the real publisher and feed handler, and the two agree.
 *
 * Exact parity needs CPython's exact algorithms, reproduced here:
 *   - MT19937 seeded with init_by_array from the integer's 32-bit words
 *   - random() = 53-bit (a>>5, b>>6) combination
 *   - gauss(): Box–Muller with the cached second value (gauss_next)
 *   - round(): round-half-even;  int(): truncation
 * test_dashboard.py checks parity against engine.py under node.
 *
 * Works as a browser script (window.MDPEngine) and as a CommonJS module.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.MDPEngine = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ---- CPython random.Random -------------------------------------------
  function PyRandom(seed) {
    this.mt = new Uint32Array(624);
    this.mti = 625;
    this.gaussNext = null;
    this.seed(seed);
  }
  PyRandom.prototype.initGenrand = function (s) {
    const mt = this.mt;
    mt[0] = s >>> 0;
    for (let i = 1; i < 624; i++) {
      const prev = mt[i - 1] ^ (mt[i - 1] >>> 30);
      // 1812433253 * prev + i, in 32-bit arithmetic
      mt[i] = (Math.imul(1812433253, prev) + i) >>> 0;
    }
    this.mti = 624;
  };
  PyRandom.prototype.seed = function (n) {
    // CPython: key = the absolute value's 32-bit words, least significant first
    let v = BigInt(Math.abs(Math.trunc(n)));
    const key = [];
    if (v === 0n) key.push(0);
    while (v > 0n) { key.push(Number(v & 0xffffffffn)); v >>= 32n; }
    const mt = this.mt;
    this.initGenrand(19650218);
    let i = 1, j = 0;
    for (let k = Math.max(624, key.length); k > 0; k--) {
      const prev = mt[i - 1] ^ (mt[i - 1] >>> 30);
      mt[i] = ((mt[i] ^ Math.imul(prev, 1664525)) + key[j] + j) >>> 0;
      i++; j++;
      if (i >= 624) { mt[0] = mt[623]; i = 1; }
      if (j >= key.length) j = 0;
    }
    for (let k = 623; k > 0; k--) {
      const prev = mt[i - 1] ^ (mt[i - 1] >>> 30);
      mt[i] = ((mt[i] ^ Math.imul(prev, 1566083941)) - i) >>> 0;
      i++;
      if (i >= 624) { mt[0] = mt[623]; i = 1; }
    }
    mt[0] = 0x80000000;
    this.gaussNext = null;
  };
  PyRandom.prototype.genrandInt32 = function () {
    const mt = this.mt;
    if (this.mti >= 624) {
      let kk = 0, y;
      for (; kk < 624 - 397; kk++) {
        y = (mt[kk] & 0x80000000) | (mt[kk + 1] & 0x7fffffff);
        mt[kk] = mt[kk + 397] ^ (y >>> 1) ^ (y & 1 ? 0x9908b0df : 0);
      }
      for (; kk < 623; kk++) {
        y = (mt[kk] & 0x80000000) | (mt[kk + 1] & 0x7fffffff);
        mt[kk] = mt[kk + (397 - 624)] ^ (y >>> 1) ^ (y & 1 ? 0x9908b0df : 0);
      }
      y = (mt[623] & 0x80000000) | (mt[0] & 0x7fffffff);
      mt[623] = mt[396] ^ (y >>> 1) ^ (y & 1 ? 0x9908b0df : 0);
      this.mti = 0;
    }
    let y = mt[this.mti++];
    y ^= y >>> 11;
    y ^= (y << 7) & 0x9d2c5680;
    y ^= (y << 15) & 0xefc60000;
    y ^= y >>> 18;
    return y >>> 0;
  };
  PyRandom.prototype.random = function () {
    const a = this.genrandInt32() >>> 5, b = this.genrandInt32() >>> 6;
    return (a * 67108864 + b) * (1.0 / 9007199254740992);
  };
  PyRandom.prototype.gauss = function (mu, sigma) {
    let z = this.gaussNext;
    this.gaussNext = null;
    if (z === null) {
      const x2pi = this.random() * 2 * Math.PI;
      const g2rad = Math.sqrt(-2.0 * Math.log(1.0 - this.random()));
      z = Math.cos(x2pi) * g2rad;
      this.gaussNext = Math.sin(x2pi) * g2rad;
    }
    return mu + z * sigma;
  };
  PyRandom.prototype.uniform = function (a, b) { return a + (b - a) * this.random(); };
  PyRandom.prototype.expovariate = function (lambd) {
    return -Math.log(1.0 - this.random()) / lambd;
  };

  // ---- Python numerics ------------------------------------------------------
  function pyRound(x) {                       // round-half-even, like round(x)
    const f = Math.floor(x), d = x - f;
    if (d > 0.5) return f + 1;
    if (d < 0.5) return f;
    return f % 2 === 0 ? f : f + 1;
  }
  const pyInt = Math.trunc;
  // round(round(px / tick) * tick, 10)
  function roundToTick(px, tick) { return Number((pyRound(px / tick) * tick).toFixed(10)); }

  // ---- engine.py MarketEngine ------------------------------------------------
  function MarketEngine(o) {
    this.tick = o.tick;
    const rt = (p) => roundToTick(p, this.tick);
    this.rt = rt;
    this.low = rt(o.low); this.high = rt(o.high);
    this.center = rt((this.low + this.high) / 2);
    this.depth = o.depth; this.volatility = o.volatility; this.reversion = o.reversion;
    this.jumpProb = o.jumpProb; this.jumpSize = o.jumpSize;
    this.rng = new PyRandom(o.seed);
    if (o.start == null) {
      this.bestBid = rt(this.center - this.tick);
    } else {
      this.bestBid = rt(Math.min(Math.max(rt(o.start), this.low), this.high - this.tick));
    }
    this.bids = new Map(); this.offers = new Map();
    this.rptSeq = 0;
  }
  MarketEngine.prototype.mid = function () {
    return Number((this.bestBid + this.tick / 2).toFixed(10));
  };
  MarketEngine.prototype.stepBestBid = function () {
    const rng = this.rng;
    const drift = -this.reversion * (this.bestBid - this.center) / this.tick;
    let shock = rng.gauss(0, this.volatility);
    if (this.jumpProb && rng.random() < this.jumpProb) {
      const mag = Math.abs(rng.gauss(this.jumpSize, this.jumpSize * 0.3));
      shock += rng.random() < 0.5 ? mag : -mag;
    }
    const stepTicks = pyRound(drift + shock);
    let nb = this.bestBid + stepTicks * this.tick;
    const lo = this.low, hi = this.high - this.tick;
    if (nb < lo) nb = lo + (lo - nb);
    if (nb > hi) nb = hi - (nb - hi);
    return this.rt(Math.min(Math.max(nb, lo), hi));
  };
  MarketEngine.prototype.targetLevels = function (side) {
    const levels = new Map(), rng = this.rng, t = this.tick;
    const best = side === "Bid" ? this.bestBid : this.bestBid + t;
    for (let i = 0; i < this.depth; i++) {
      const price = this.rt(side === "Bid" ? best - i * t : best + i * t);
      if (price < this.low || price > this.high) continue;
      const base = 20 + i * 15;
      const size = Math.max(1, pyInt(rng.gauss(base, base * 0.3)));
      const orders = Math.max(1, pyInt(size / rng.uniform(3, 9)));
      levels.set(price, [price, size, orders]);
    }
    return levels;
  };
  function diffCount(cur, tgt) {
    let n = 0;
    for (const [p, l] of tgt) { const o = cur.get(p); if (!o || o[1] !== l[1] || o[2] !== l[2]) n++; }
    for (const p of cur.keys()) if (!tgt.has(p)) n++;
    return n;
  }
  MarketEngine.prototype.step = function () {
    this.bestBid = this.stepBestBid();
    const tb = this.targetLevels("Bid"), to = this.targetLevels("Offer");
    const nInc = diffCount(this.bids, tb) + diffCount(this.offers, to);
    this.bids = tb; this.offers = to;
    const trades = [], rng = this.rng;
    if (rng.random() < 0.35) {
      const buy = rng.random() < 0.5;
      if (buy && to.size) {
        trades.push([Math.min(...to.keys()), Math.max(1, pyInt(rng.expovariate(1 / 4))), "Buy"]);
      } else if (!buy && tb.size) {
        trades.push([Math.max(...tb.keys()), Math.max(1, pyInt(rng.expovariate(1 / 4))), "Sell"]);
      }
    }
    this.rptSeq += nInc + trades.length;
    return { nInc, trades };
  };

  /* Re-simulate one instrument for `steps` steps; frames shaped like the
   * session's ({m, bb, bo, b, o, tr, s, r}). */
  function simulate(o) {
    const eng = new MarketEngine(o);
    const frames = [];
    for (let i = 0; i < o.steps; i++) {
      const { trades } = eng.step();
      const b = [...eng.bids.values()].sort((x, y) => y[0] - x[0]).slice(0, o.depth);
      const of = [...eng.offers.values()].sort((x, y) => x[0] - y[0]).slice(0, o.depth);
      const bb = b.length ? b[0][0] : null, bo = of.length ? of[0][0] : null;
      frames.push({ m: bb != null && bo != null ? Number(((bb + bo) / 2).toFixed(10)) : null,
                    bb, bo, b, o: of, tr: trades, s: 0, r: eng.rptSeq });
    }
    return { frames, low: eng.low, high: eng.high, center: eng.center };
  }

  return { PyRandom, MarketEngine, simulate, pyRound, roundToTick };
});

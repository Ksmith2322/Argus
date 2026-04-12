"""
Bayesian updating model for Atlas event impact predictions.

Uses Normal-Normal conjugate updating for each (event_type, asset, window)
triple.  Starts with priors from the historical impact matrix (154 events)
and updates as new observations arrive.

Usage:
    python -m forge.atlas.impact.bayesian --init
    python -m forge.atlas.impact.bayesian --predict TARIFF_ANNOUNCE
    python -m forge.atlas.impact.bayesian --predict-all
    python -m forge.atlas.impact.bayesian --status
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────

_REPO = Path(__file__).resolve().parents[3]  # forge/atlas/impact -> repo root
IMPACT_MATRIX_PATH = _REPO / "forge" / "data" / "atlas_impact_matrix.json"
STATE_PATH = _REPO / "forge" / "data" / "atlas_bayesian_state.json"

# Weak / uninformative prior for entries with insufficient data
WEAK_PRIOR_MEAN = 0.0
WEAK_PRIOR_STD = 0.05
MIN_HIST_N = 5  # minimum sample size to use historical prior


# ──────────────────────────────────────────────────────────────────────────────
# Model
# ──────────────────────────────────────────────────────────────────────────────

class BayesianImpactModel:
    """
    For each (event_type, asset, window) triple, maintains a posterior
    distribution over the expected CAR.

    Prior:      N(mu_0, sigma_0^2) from historical impact matrix
    Likelihood: Each new observation is N(mu, sigma^2)
    Posterior:  N(mu_n, sigma_n^2) via conjugate update

    The posterior is updated incrementally: each new observation shifts the
    posterior mean toward the observed value, weighted by relative precision.
    """

    def __init__(self, impact_matrix_path: Optional[str | Path] = None):
        self.impact_matrix_path = Path(impact_matrix_path or IMPACT_MATRIX_PATH)
        # Key = (event_type, asset, window) -> state dict
        self.states: dict[tuple[str, str, str], dict] = {}
        self._meta = {
            "last_updated": None,
            "total_observations": 0,
        }

    # ── Prior setup ──────────────────────────────────────────────────────

    def set_prior(
        self,
        event_type: str,
        asset: str,
        window: str,
        mean: float,
        std: float,
        n: int,
    ) -> None:
        """Set prior from historical data for a single triple."""
        prior_var = std ** 2 if std > 0 else WEAK_PRIOR_STD ** 2
        obs_var = std ** 2 if std > 0 else WEAK_PRIOR_STD ** 2
        key = (event_type, asset, window)
        self.states[key] = {
            "prior_mean": mean,
            "prior_var": prior_var,
            "prior_n": n,
            "obs_var": obs_var,
            "posterior_mean": mean,
            "posterior_var": prior_var,
            "observations": [],
        }

    def init_from_matrix(self) -> int:
        """
        Load the impact matrix JSON and initialize priors for every
        (event_type, asset, window) triple.

        Returns the number of triples initialized.
        """
        if not self.impact_matrix_path.exists():
            log.error("Impact matrix not found: %s", self.impact_matrix_path)
            return 0

        with open(self.impact_matrix_path) as f:
            matrix = json.load(f)

        count = 0
        for event_type, assets in matrix.items():
            for asset, windows in assets.items():
                for window, stats in windows.items():
                    n = stats.get("n", 0)
                    if n >= MIN_HIST_N:
                        self.set_prior(
                            event_type, asset, window,
                            mean=stats["mean"],
                            std=stats["std"],
                            n=n,
                        )
                    else:
                        # Weak / uninformative prior
                        self.set_prior(
                            event_type, asset, window,
                            mean=WEAK_PRIOR_MEAN,
                            std=WEAK_PRIOR_STD,
                            n=0,
                        )
                    count += 1

        self._meta["last_updated"] = datetime.now(timezone.utc).isoformat()
        log.info("Initialized %d triples from impact matrix", count)
        return count

    # ── Bayesian update ──────────────────────────────────────────────────

    def update(
        self,
        event_type: str,
        asset: str,
        window: str,
        observed_car: float,
    ) -> None:
        """Apply a single conjugate Normal-Normal update."""
        key = (event_type, asset, window)
        if key not in self.states:
            # Auto-create with weak prior
            self.set_prior(event_type, asset, window,
                           mean=WEAK_PRIOR_MEAN, std=WEAK_PRIOR_STD, n=0)

        state = self.states[key]
        prior_mean = state["posterior_mean"]
        prior_var = state["posterior_var"]
        obs_var = state["obs_var"]

        # Guard against zero variance
        if prior_var <= 0:
            prior_var = WEAK_PRIOR_STD ** 2
        if obs_var <= 0:
            obs_var = WEAK_PRIOR_STD ** 2

        # Posterior precision = prior precision + observation precision
        post_precision = 1.0 / prior_var + 1.0 / obs_var
        post_var = 1.0 / post_precision

        # Posterior mean = precision-weighted average
        post_mean = post_var * (prior_mean / prior_var + observed_car / obs_var)

        state["posterior_mean"] = post_mean
        state["posterior_var"] = post_var
        state["observations"].append(observed_car)

        self._meta["total_observations"] = sum(
            len(s["observations"]) for s in self.states.values()
        )
        self._meta["last_updated"] = datetime.now(timezone.utc).isoformat()

    # ── Predictions ──────────────────────────────────────────────────────

    def predict(
        self,
        event_type: str,
        asset: str,
        window: str,
    ) -> dict:
        """Return posterior prediction for a single triple."""
        key = (event_type, asset, window)
        if key not in self.states:
            return {
                "mean": 0.0,
                "std": WEAK_PRIOR_STD,
                "ci_95_lower": -1.96 * WEAK_PRIOR_STD,
                "ci_95_upper": 1.96 * WEAK_PRIOR_STD,
                "n_observations": 0,
                "data_supported": False,
            }

        state = self.states[key]
        post_std = math.sqrt(state["posterior_var"])
        n_obs = len(state["observations"])
        total_n = state["prior_n"] + n_obs
        ci_lower = state["posterior_mean"] - 1.96 * post_std
        ci_upper = state["posterior_mean"] + 1.96 * post_std

        # Data-supported: enough observations AND CI doesn't cross zero
        data_supported = (total_n > 5) and (ci_lower > 0 or ci_upper < 0)

        return {
            "mean": round(state["posterior_mean"], 6),
            "std": round(post_std, 6),
            "ci_95_lower": round(ci_lower, 6),
            "ci_95_upper": round(ci_upper, 6),
            "n_observations": n_obs,
            "prior_n": state["prior_n"],
            "total_n": total_n,
            "data_supported": data_supported,
        }

    def get_all_predictions(self, event_type: str) -> dict:
        """All asset/window predictions for an event type."""
        results: dict[str, dict[str, dict]] = {}
        for (et, asset, window), _state in self.states.items():
            if et != event_type:
                continue
            results.setdefault(asset, {})[window] = self.predict(et, asset, window)
        return results

    def predict_cascade(
        self,
        event_type: str,
        severity: float = 1.0,
    ) -> list[dict]:
        """
        For all assets in the event's cascade template, return posterior
        predictions scaled by severity.  Sorted by tightest posterior
        variance first.
        """
        # Import cascade templates lazily to avoid circular imports
        from forge.atlas.cascade.templates import get_cascade_template

        template = get_cascade_template(event_type)
        if not template:
            # Fall back to impact-matrix assets if no template
            preds = self.get_all_predictions(event_type)
            results = []
            for asset, windows in preds.items():
                for window, pred in windows.items():
                    results.append({
                        "asset": asset,
                        "window": window,
                        "predicted_car": round(pred["mean"] * severity, 6),
                        "std": pred["std"],
                        "ci_95_lower": round(pred["ci_95_lower"] * severity, 6),
                        "ci_95_upper": round(pred["ci_95_upper"] * severity, 6),
                        "n_observations": pred["n_observations"],
                        "prior_n": pred["prior_n"],
                        "total_n": pred["total_n"],
                        "data_supported": pred["data_supported"],
                    })
            results.sort(key=lambda r: r["std"])
            return results

        results = []
        for entry in template:
            asset = entry["asset"]
            direction = entry.get("direction", 1)
            # Use CAR(1,5) as the default window for cascade predictions
            window = "CAR(1,5)"
            pred = self.predict(event_type, asset, window)

            scaled_mean = pred["mean"] * severity * direction
            scaled_lower = pred["ci_95_lower"] * severity * direction
            scaled_upper = pred["ci_95_upper"] * severity * direction

            # If direction is negative, CI bounds flip
            if direction < 0:
                scaled_lower, scaled_upper = scaled_upper, scaled_lower

            results.append({
                "asset": asset,
                "wave": entry.get("wave", 1),
                "direction": direction,
                "window": window,
                "predicted_car": round(scaled_mean, 6),
                "std": pred["std"],
                "ci_95_lower": round(scaled_lower, 6),
                "ci_95_upper": round(scaled_upper, 6),
                "confidence": entry.get("confidence", "low"),
                "lag_days": entry.get("lag_days", 0),
                "n_observations": pred["n_observations"],
                "prior_n": pred["prior_n"],
                "total_n": pred["total_n"],
                "data_supported": pred["data_supported"],
                "template_validated": entry.get("data_validated", False),
            })

        # Sort by tightest posterior variance first
        results.sort(key=lambda r: r["std"])
        return results

    # ── Persistence ──────────────────────────────────────────────────────

    def save_state(self, path: Optional[str | Path] = None) -> None:
        """Persist model state to JSON."""
        path = Path(path or STATE_PATH)
        serializable = {}
        for (event_type, asset, window), state in self.states.items():
            key_str = f"{event_type}|{asset}|{window}"
            serializable[key_str] = state

        payload = {
            "meta": self._meta,
            "states": serializable,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        log.info("Saved Bayesian state to %s (%d triples)", path, len(self.states))

    def load_state(self, path: Optional[str | Path] = None) -> bool:
        """Load model state from JSON.  Returns True if loaded successfully."""
        path = Path(path or STATE_PATH)
        if not path.exists():
            log.info("No saved state at %s", path)
            return False

        with open(path) as f:
            payload = json.load(f)

        self._meta = payload.get("meta", self._meta)
        self.states.clear()
        for key_str, state in payload.get("states", {}).items():
            parts = key_str.split("|")
            if len(parts) == 3:
                self.states[tuple(parts)] = state  # type: ignore[arg-type]

        log.info("Loaded Bayesian state: %d triples", len(self.states))
        return True

    # ── Diagnostics ──────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return summary statistics about the model."""
        total_triples = len(self.states)
        if total_triples == 0:
            return {"total_triples": 0}

        n_with_obs = sum(
            1 for s in self.states.values() if len(s["observations"]) > 0
        )
        avg_post_std = sum(
            math.sqrt(s["posterior_var"]) for s in self.states.values()
        ) / total_triples
        total_obs = sum(len(s["observations"]) for s in self.states.values())

        event_types = sorted(set(k[0] for k in self.states))
        assets = sorted(set(k[1] for k in self.states))

        n_data_supported = 0
        for key, state in self.states.items():
            total_n = state["prior_n"] + len(state["observations"])
            post_std = math.sqrt(state["posterior_var"])
            ci_lower = state["posterior_mean"] - 1.96 * post_std
            ci_upper = state["posterior_mean"] + 1.96 * post_std
            if total_n > 5 and (ci_lower > 0 or ci_upper < 0):
                n_data_supported += 1

        return {
            "total_triples": total_triples,
            "triples_with_new_obs": n_with_obs,
            "total_new_observations": total_obs,
            "avg_posterior_std": round(avg_post_std, 6),
            "n_data_supported": n_data_supported,
            "n_event_types": len(event_types),
            "event_types": event_types,
            "n_assets": len(assets),
            "last_updated": self._meta.get("last_updated"),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Convenience function for the Atlas runner
# ──────────────────────────────────────────────────────────────────────────────

def get_bayesian_prediction(
    event_type: str,
    severity: float = 1.0,
) -> list[dict]:
    """
    Convenience function for the Atlas runner.
    Loads model state, returns cascade predictions for the event type.
    Returns empty list if model isn't initialized.
    """
    model = BayesianImpactModel()
    if not model.load_state():
        return []
    return model.predict_cascade(event_type, severity)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Bayesian impact model for Atlas event predictions",
    )
    parser.add_argument("--init", action="store_true",
                        help="Initialize model from impact matrix and save state")
    parser.add_argument("--predict", metavar="EVENT_TYPE",
                        help="Show predictions for an event type")
    parser.add_argument("--predict-all", action="store_true",
                        help="Show predictions for all event types with data")
    parser.add_argument("--status", action="store_true",
                        help="Show model statistics")
    parser.add_argument("--severity", type=float, default=1.0,
                        help="Severity multiplier for predictions (default 1.0)")

    args = parser.parse_args()

    if not any([args.init, args.predict, args.predict_all, args.status]):
        parser.print_help()
        sys.exit(1)

    model = BayesianImpactModel()

    # ── Init ─────────────────────────────────────────────────────────────
    if args.init:
        n = model.init_from_matrix()
        model.save_state()
        print(f"\nInitialized Bayesian model with {n} triples")
        st = model.status()
        print(f"  Event types : {st['n_event_types']}")
        print(f"  Assets      : {st['n_assets']}")
        print(f"  Data-supported (CI excludes 0, N>5): {st['n_data_supported']}")
        print(f"  Avg posterior std: {st['avg_posterior_std']:.4f}")
        return

    # For all other commands, load saved state
    if not model.load_state():
        print("No saved model state. Run --init first.")
        sys.exit(1)

    # ── Predict ──────────────────────────────────────────────────────────
    if args.predict:
        event_type = args.predict.upper()
        preds = model.predict_cascade(event_type, severity=args.severity)
        if not preds:
            print(f"No predictions for {event_type}")
            sys.exit(1)

        print(f"\n{'='*78}")
        print(f"  Bayesian Cascade Predictions: {event_type}  (severity={args.severity})")
        print(f"{'='*78}")
        print(f"  {'Asset':<6} {'Wave':>4} {'Dir':>4} {'CAR':>9} {'Std':>8} "
              f"{'95% CI':>19} {'N':>4} {'Support':>8}")
        print(f"  {'-'*6} {'-'*4} {'-'*4} {'-'*9} {'-'*8} "
              f"{'-'*19} {'-'*4} {'-'*8}")

        for p in preds:
            ci = f"[{p['ci_95_lower']:+.4f}, {p['ci_95_upper']:+.4f}]"
            flag = "YES" if p["data_supported"] else "no"
            print(f"  {p['asset']:<6} {p.get('wave','-'):>4} "
                  f"{p.get('direction',0):>+4d} "
                  f"{p['predicted_car']:>+9.4f} {p['std']:>8.4f} "
                  f"{ci:>19} {p['total_n']:>4} {flag:>8}")
        print()
        return

    # ── Predict All ──────────────────────────────────────────────────────
    if args.predict_all:
        event_types = sorted(set(k[0] for k in model.states))
        for et in event_types:
            preds = model.predict_cascade(et, severity=args.severity)
            supported = [p for p in preds if p["data_supported"]]
            if supported:
                print(f"\n{et}: {len(supported)} data-supported predictions")
                for p in supported[:5]:
                    print(f"  {p['asset']:<6} CAR={p['predicted_car']:+.4f} "
                          f"std={p['std']:.4f} N={p['total_n']}")
        return

    # ── Status ───────────────────────────────────────────────────────────
    if args.status:
        st = model.status()
        print(f"\nBayesian Impact Model Status")
        print(f"{'='*40}")
        print(f"  Total triples        : {st['total_triples']}")
        print(f"  With new observations: {st['triples_with_new_obs']}")
        print(f"  Total new obs        : {st['total_new_observations']}")
        print(f"  Avg posterior std     : {st['avg_posterior_std']:.4f}")
        print(f"  Data-supported       : {st['n_data_supported']}")
        print(f"  Event types          : {st['n_event_types']}")
        print(f"  Assets               : {st['n_assets']}")
        print(f"  Last updated         : {st['last_updated']}")
        print()
        return


if __name__ == "__main__":
    _cli()

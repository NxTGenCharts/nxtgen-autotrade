"""
Bot presets. Explicitly worded as risk levels, not profitability promises —
per product requirement, never marketed as "guaranteed" anything.
"""

DCA_TREND_PRESETS = {
    "conservative": dict(
        risk_per_trade_pct=0.5, max_dca_orders=3, dca_step_pct=0.6,
        take_profit_pct=1.2, stop_loss_pct=3.0, max_drawdown_pct=12,
        max_open_positions=3, max_exposure_pct=60,
    ),
    "balanced": dict(
        risk_per_trade_pct=1.0, max_dca_orders=5, dca_step_pct=0.8,
        take_profit_pct=1.8, stop_loss_pct=4.5, max_drawdown_pct=20,
        max_open_positions=5, max_exposure_pct=80,
    ),
    "aggressive": dict(
        risk_per_trade_pct=1.75, max_dca_orders=7, dca_step_pct=1.0,
        take_profit_pct=2.5, stop_loss_pct=6.0, max_drawdown_pct=30,
        max_open_positions=7, max_exposure_pct=100,
    ),
}

GRID_SIDEWAYS_PRESETS = {
    "conservative": dict(
        grid_levels=5, grid_spacing_pct=0.4, take_profit_pct_per_grid=0.4,
        max_drawdown_pct=10, max_open_positions=5, max_exposure_pct=60,
        trend_escape_threshold_pct=1.2,
    ),
    "balanced": dict(
        grid_levels=8, grid_spacing_pct=0.5, take_profit_pct_per_grid=0.5,
        max_drawdown_pct=18, max_open_positions=8, max_exposure_pct=80,
        trend_escape_threshold_pct=1.6,
    ),
    "aggressive": dict(
        grid_levels=12, grid_spacing_pct=0.6, take_profit_pct_per_grid=0.6,
        max_drawdown_pct=28, max_open_positions=12, max_exposure_pct=100,
        trend_escape_threshold_pct=2.2,
    ),
}


def resolve_config(strategy, risk_profile, capital):
    table = DCA_TREND_PRESETS if strategy == "dca_trend" else GRID_SIDEWAYS_PRESETS
    base = dict(table[risk_profile])
    base["max_exposure_usd"] = round(capital * base.pop("max_exposure_pct") / 100, 2)
    base["capital"] = capital
    return base

from __future__ import annotations

import numpy as np


def error_stats(errors) -> dict:
    arr = np.asarray(errors, dtype=np.float64).reshape(-1)
    if len(arr) == 0:
        return {"rmse": 0.0, "median_abs": 0.0, "p90_abs": 0.0, "max_abs": 0.0}
    abs_err = np.abs(arr)
    return {
        "rmse": float(np.sqrt(np.mean(arr * arr))),
        "median_abs": float(np.median(abs_err)),
        "p90_abs": float(np.percentile(abs_err, 90)),
        "max_abs": float(abs_err.max()),
    }

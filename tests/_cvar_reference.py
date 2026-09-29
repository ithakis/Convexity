"""Ground-truth mean-CVaR LP reference solver — TEST-ONLY.

This is the slow, trusted reference the custom numba interior-point solver
(`mpt._cvar_pdip`) is certified against. It solves the identical
Rockafellar-Uryasev min-CVaR linear program (optional return floor, long-only
box, Σw = 1 or ≤ 1) with SciPy's HiGHS backend. It is deliberately NOT on the
production hot path — `mpt.py` imports no scipy. Keeping the reference here means
CI (`pytest tests/`) certifies the fast solver on every push without shipping
scipy in the request path.
"""

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csr_matrix, eye as speye, hstack as sphstack, vstack as spvstack


def cvar_lp_reference(R, a_ret, b_ret, l, h, alpha, fully_invested):
    """Solve one mean-CVaR LP with HiGHS. Returns (w, zeta, cvar) or None.

    Variables [w(N), ζ(1), u(T)]; minimise ζ + 1/((1-α)T) Σ u_t subject to
    u_t ≥ -R_t·w - ζ, u ≥ 0, a_ret·w ≥ b_ret (if b_ret > -1e17), box l ≤ w ≤ h,
    and Σw = 1 (fully invested) or Σw ≤ 1 (cash allowed).
    """
    R = np.asarray(R, dtype=float)
    T, N = R.shape
    tail = (1.0 - alpha) * T
    nv = N + 1 + T
    c = np.zeros(nv)
    c[N] = 1.0
    c[N + 1 :] = 1.0 / tail

    blocks = [
        sphstack(
            [csr_matrix(-R), csr_matrix(-np.ones((T, 1))), -speye(T, format="csr")], format="csr"
        )
    ]
    b_ub = [np.zeros(T)]
    if b_ret > -1e17:
        row = np.zeros((1, nv))
        row[0, :N] = -np.asarray(a_ret, float)
        blocks.append(csr_matrix(row))
        b_ub.append(np.array([-b_ret]))
    if not fully_invested:
        row = np.zeros((1, nv))
        row[0, :N] = 1.0
        blocks.append(csr_matrix(row))
        b_ub.append(np.array([1.0]))
    A_ub = spvstack(blocks, format="csr")
    b_ub = np.concatenate(b_ub)

    A_eq = b_eq = None
    if fully_invested:
        row = np.zeros((1, nv))
        row[0, :N] = 1.0
        A_eq = csr_matrix(row)
        b_eq = np.array([1.0])

    bounds = [(l[i], h[i]) for i in range(N)] + [(None, None)] + [(0.0, None)] * T
    res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if not res.success:
        return None
    x = res.x
    return x[:N], x[N], res.fun

"""Finite-difference tridiagonal operator assembly for log-space PDE.

PDE in log-space  x = log(S):
    ∂V/∂t + ½σ²·∂²V/∂x² + (r − q − ½σ²)·∂V/∂x − r·V = 0

Spatial discretisation (central differences):
    ∂²V/∂x² ≈ (V_{i+1} − 2V_i + V_{i-1}) / dx²
    ∂V/∂x  ≈ (V_{i+1} − V_{i-1}) / (2·dx)

This gives the tridiagonal system  A·V = rhs  solved by scipy.linalg.solve_banded.
"""

from __future__ import annotations

import numpy as np


def build_coefficients(
    sigma: np.ndarray,
    r: float,
    q: float,
    dx: float,
    dt: float,
    theta: float = 0.5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Assemble Crank-Nicolson (θ=0.5) tridiagonal coefficients.

    Parameters
    ----------
    sigma : 1-D array, shape (N,)
        Local volatility at each interior spatial node at the current time.
    r, q : float
        Risk-free rate and dividend yield.
    dx : float
        Spatial step in log-space.
    dt : float
        Time step.
    theta : float
        Crank-Nicolson parameter (0.5 = symmetric, 1.0 = implicit).

    Returns
    -------
    a, b, c : 1-D arrays of length N
        Sub-, main-, and super-diagonal coefficients of the **implicit** part
        of the LHS matrix  M_impl  in  M_impl·V^{n+1} = M_expl·V^n.
        The explicit RHS matrix is  M_expl = I − (1−θ)·L·dt  where
        L is the differential operator.
    """
    N = len(sigma)
    var = sigma**2  # σ²

    alpha = 0.5 * var / dx**2         # diffusion coefficient
    beta = (r - q - 0.5 * var) / (2.0 * dx)  # advection coefficient

    # Tridiagonal entries for L (excluding dt and θ factors)
    l_sub = alpha - beta              # coefficient of V_{i-1}
    l_main = -2.0 * alpha - r        # coefficient of V_i  (reaction term)
    l_sup = alpha + beta              # coefficient of V_{i+1}

    # Implicit LHS:  I − θ·dt·L
    a = -theta * dt * l_sub
    b = 1.0 - theta * dt * l_main
    c = -theta * dt * l_sup

    return a, b, c


def apply_explicit(
    V: np.ndarray,
    sigma: np.ndarray,
    r: float,
    q: float,
    dx: float,
    dt: float,
    theta: float = 0.5,
) -> np.ndarray:
    """Compute the explicit RHS vector  rhs = [I + (1−θ)·dt·L]·V.

    Parameters
    ----------
    V : 1-D array, shape (N,)
        Option value vector at the current time step.
    sigma, r, q, dx, dt, theta : same as ``build_coefficients``.

    Returns
    -------
    rhs : 1-D array, shape (N,)
    """
    N = len(V)
    var = sigma**2

    alpha = 0.5 * var / dx**2
    beta = (r - q - 0.5 * var) / (2.0 * dx)

    l_sub = alpha - beta
    l_main = -2.0 * alpha - r
    l_sup = alpha + beta

    rhs = np.empty(N, dtype=float)
    # Interior nodes
    rhs[1:-1] = (V[1:-1]
                 + (1.0 - theta) * dt * (l_sub[1:-1] * V[:-2]
                                          + l_main[1:-1] * V[1:-1]
                                          + l_sup[1:-1] * V[2:]))
    # Boundary nodes (will be overwritten after this call)
    rhs[0] = V[0]
    rhs[-1] = V[-1]
    return rhs


def to_banded(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Pack (a, b, c) into scipy banded storage (shape 3×N).

    scipy.linalg.solve_banded expects the matrix in the format::

        ab[0, 1:]  = c[:-1]   (super-diagonal, offset by 1)
        ab[1, :]   = b        (main diagonal)
        ab[2, :-1] = a[1:]    (sub-diagonal, offset by 1)
    """
    N = len(b)
    ab = np.zeros((3, N), dtype=float)
    ab[0, 1:] = c[:-1]
    ab[1, :] = b
    ab[2, :-1] = a[1:]
    return ab

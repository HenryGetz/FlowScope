"""Track 4.1 — physics-informed neural operator surrogates (CPU-viable, compact).

Two operators predicting steady flow fields on a regular grid from geometry +
boundary-condition conditioning:

* ``GINOOperator``   — GINO-style: 3D CNN geometry encoder over an occupancy/SDF
  grid -> latent low-res grid, trilinearly projected onto the fine grid, fused
  with coordinate/BC features, an FNO-style spectral-convolution trunk, and a
  pointwise projection decoder.
* ``TransolverOperator`` — Transolver-style physics-attention transformer over
  grid tokens: points are softly projected onto a small set of learnable slice
  tokens, attention runs among the slices (O(N) instead of O(N^2)), features are
  unprojected back to points, followed by MLP blocks.

Both share the same call signature:

    model(occ, cond) -> {"u": [B, G, G, G, 3], "p": [B, G, G, G]}

``occ``  is [B, 1, G, G, G] occupancy (inside = 1) or a signed-distance grid
passed through ``sdf_to_occupancy``; ``cond`` is [B, P] the flattened params
vector (Re, D, inlet BC, geometry parameters).

Dependencies: torch + numpy only. Parameter budget: <= 5M per model
(``assert_param_budget``).
"""

import math

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

PARAM_BUDGET = 5_000_000


def count_params(module: nn.Module) -> int:
    return sum(p.numel() for p in module.parameters())


def assert_param_budget(module: nn.Module, budget: int = PARAM_BUDGET) -> int:
    n = count_params(module)
    if n > budget:
        raise ValueError(f"{type(module).__name__} has {n:,} params > budget {budget:,}")
    return n


def sdf_to_occupancy(sdf: torch.Tensor) -> torch.Tensor:
    """Signed distance (negative inside) -> occupancy (1 inside)."""
    return (sdf < 0).to(sdf.dtype)


def grid_coords(grid: int, device=None, dtype=torch.float32) -> torch.Tensor:
    """Cell-centered normalized coordinates [G, G, G, 3] in [0, 1]^3 (xyz order)."""
    ax = torch.linspace(0.0, 1.0, grid, device=device, dtype=dtype)
    gx, gy, gz = torch.meshgrid(ax, ax, ax, indexing="ij")
    return torch.stack([gx, gy, gz], dim=-1)


class SpectralConv3d(nn.Module):
    """FNO-style 3D spectral convolution.

    Real FFT along the last axis (half-spectrum), full FFT on the first two, and
    four truncated-mode quadrant blocks [+,+], [+,-], [-,+], [-,-].
    """

    def __init__(self, in_ch: int, out_ch: int, modes: int):
        super().__init__()
        self.modes = modes
        scale = 1.0 / (in_ch * out_ch)
        self.w = nn.ParameterList(
            nn.Parameter(
                scale * torch.rand(in_ch, out_ch, modes, modes, modes, dtype=torch.cfloat)
            )
            for _ in range(4)
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        b, _, nx, ny, nz = x.shape
        m1 = min(self.modes, nx)
        m2 = min(self.modes, ny)
        m3 = min(self.modes, nz // 2 + 1)
        x_ft = torch.fft.rfftn(x, dim=(-3, -2, -1))
        out_ft = torch.zeros(
            b, self.w[0].shape[1], nx, ny, nz // 2 + 1, dtype=torch.cfloat, device=x.device
        )
        specs = (
            ((slice(0, m1), slice(0, m2), slice(0, m3)), 0),
            ((slice(0, m1), slice(-m2, None), slice(0, m3)), 1),
            ((slice(-m1, None), slice(0, m2), slice(0, m3)), 2),
            ((slice(-m1, None), slice(-m2, None), slice(0, m3)), 3),
        )
        for sidx, k in specs:
            w = self.w[k][:, :, :m1, :m2, :m3]
            out_ft[(slice(None), slice(None)) + sidx] = torch.einsum(
                "bixyz,ioxyz->boxyz", x_ft[(slice(None), slice(None)) + sidx], w
            )
        return torch.fft.irfftn(out_ft, s=(nx, ny, nz), dim=(-3, -2, -1))


class FNOBlock(nn.Module):
    def __init__(self, width: int, modes: int):
        super().__init__()
        self.spec = SpectralConv3d(width, width, modes)
        self.skip = nn.Conv3d(width, width, kernel_size=1)
        self.act = nn.GELU()

    def forward(self, x):
        return self.act(self.spec(x) + self.skip(x))


class GeometryEncoder(nn.Module):
    """Occupancy/SDF grid -> latent grid [B, C, M, M, M]."""

    def __init__(self, width: int, latent: int = 8):
        super().__init__()
        self.latent = latent
        self.net = nn.Sequential(
            nn.Conv3d(1, width, 3, padding=1), nn.GELU(),
            nn.Conv3d(width, width, 3, stride=2, padding=1), nn.GELU(),
            nn.Conv3d(width, width, 3, stride=2, padding=1), nn.GELU(),
        )

    def forward(self, occ):
        z = self.net(occ)
        return F.adaptive_avg_pool3d(z, self.latent)


class GINOOperator(nn.Module):
    """GINO-style operator: geometry latent + spectral trunk + projection decoder."""

    def __init__(self, grid: int, cond_dim: int, width: int = 20, modes: int = 6, depth: int = 4,
                 latent: int = 8):
        super().__init__()
        self.grid = grid
        self.encoder = GeometryEncoder(width, latent)
        self.cond_mlp = nn.Sequential(nn.Linear(cond_dim, width), nn.GELU(), nn.Linear(width, width))
        self.lift = nn.Sequential(
            nn.Linear(3 + width + width, width), nn.GELU(), nn.Linear(width, width)
        )
        self.trunk = nn.Sequential(*[FNOBlock(width, modes) for _ in range(depth)])
        self.proj = nn.Sequential(nn.Linear(width, width), nn.GELU(), nn.Linear(width, 4))

    def forward(self, occ, cond):
        b = cond.shape[0]
        g = self.grid
        lat = self.encoder(occ)  # [B, C, m, m, m]
        lat = F.interpolate(lat, size=(g, g, g), mode="trilinear", align_corners=True)
        coords = grid_coords(g, device=cond.device, dtype=cond.dtype).reshape(1, g * g * g, 3)
        coords = coords.expand(b, -1, -1)
        c = self.cond_mlp(cond).unsqueeze(1).expand(-1, g * g * g, -1)
        lat_f = lat.reshape(b, -1, g * g * g).transpose(1, 2)
        h = self.lift(torch.cat([coords, c, lat_f], dim=-1))  # [B, G^3, C]
        h = h.transpose(1, 2).reshape(b, -1, g, g, g)
        h = self.trunk(h)
        h = h.reshape(b, -1, g * g * g).transpose(1, 2)
        out = self.proj(h).reshape(b, g, g, g, 4)
        return {"u": out[..., :3], "p": out[..., 3]}


class PhysicsAttention(nn.Module):
    """Transolver-style physics attention via projected slice tokens.

    Points softly pool into S slice tokens (softmax over points per slice),
    attention runs among slices only, then each point gathers from the slices
    (softmax over slices per point). O(N*S) instead of O(N^2).
    """

    def __init__(self, dim: int, n_slices: int, heads: int = 4, mlp_ratio: float = 2.0):
        super().__init__()
        self.score = nn.Linear(dim, n_slices)
        self.slice_tokens = nn.Parameter(torch.randn(n_slices, dim) / math.sqrt(dim))
        self.norm1 = nn.LayerNorm(dim)
        self.attn = nn.MultiheadAttention(dim, heads, batch_first=True)
        self.norm2 = nn.LayerNorm(dim)
        hidden = int(dim * mlp_ratio)
        self.mlp = nn.Sequential(nn.Linear(dim, hidden), nn.GELU(), nn.Linear(hidden, dim))

    def forward(self, x):
        s = self.score(x)                                   # [B, N, S]
        a = torch.softmax(s, dim=1)                         # points -> slices
        slices = torch.einsum("bns,bnc->bsc", a, x) + self.slice_tokens
        slices = slices + self.attn(self.norm1(slices), self.norm1(slices), self.norm1(slices))[0]
        w = torch.softmax(s, dim=-1)                        # slices -> points
        x = x + torch.einsum("bns,bsc->bnc", w, slices)
        x = x + self.mlp(self.norm2(x))
        return x


class TransolverOperator(nn.Module):
    """Transolver-style operator: physics-attention transformer over grid tokens.

    The grid is patch-embedded (2x2x2 point patches -> one token, as in the
    original Transolver) before physics attention; the head expands each token
    back to its 2x2x2x4 field values.
    """

    def __init__(self, grid: int, cond_dim: int, dim: int = 48, depth: int = 4,
                 n_slices: int = 64, heads: int = 4, patch: int = 2):
        super().__init__()
        if grid % patch:
            raise ValueError(f"grid {grid} must be divisible by patch {patch}")
        self.grid = grid
        self.patch = patch
        self.n_tok = (grid // patch) ** 3
        self.embed = nn.Sequential(
            nn.Linear(patch**3 * (3 + 1), dim), nn.GELU(), nn.Linear(dim, dim)
        )
        self.cond_mlp = nn.Linear(cond_dim, dim)
        self.blocks = nn.ModuleList(PhysicsAttention(dim, n_slices, heads) for _ in range(depth))
        self.head = nn.Sequential(nn.LayerNorm(dim), nn.Linear(dim, dim), nn.GELU(),
                                  nn.Linear(dim, patch**3 * 4))
        # Small output init is essential: default-init outputs (rms ~2-3 from the
        # unnormalized residual stream) land optimization in a collapse basin that
        # decays to near-zero predictions and stays there; the healthy regime is
        # init output rms ~0.03.
        with torch.no_grad():
            self.head[-1].weight.mul_(0.1)
            self.head[-1].bias.mul_(0.1)

    def forward(self, occ, cond):
        b = cond.shape[0]
        g, p = self.grid, self.patch
        coords = grid_coords(g, device=cond.device, dtype=cond.dtype)  # [G,G,G,3]
        geo = occ.reshape(b, 1, g, g, g).permute(0, 2, 3, 4, 1)       # [B,G,G,G,1]
        vol = torch.cat([coords.unsqueeze(0).expand(b, -1, -1, -1, -1), geo], dim=-1)
        # patchify: [B, G/p, p, G/p, p, G/p, p, 4] -> [B, (G/p)^3, p^3*4]
        hp = vol.reshape(b, g // p, p, g // p, p, g // p, p, 4)
        hp = hp.permute(0, 1, 3, 5, 2, 4, 6, 7).reshape(b, self.n_tok, p**3 * 4)
        x = self.embed(hp) + self.cond_mlp(cond).unsqueeze(1)
        for blk in self.blocks:
            x = blk(x)
        out = self.head(x).reshape(b, g // p, g // p, g // p, p, p, p, 4)
        out = out.permute(0, 1, 4, 2, 5, 3, 6, 7).reshape(b, g, g, g, 4)
        return {"u": out[..., :3], "p": out[..., 3]}


def build_model(name: str, grid: int, cond_dim: int) -> nn.Module:
    name = name.lower()
    if name in ("gino", "gino-style"):
        model = GINOOperator(grid, cond_dim)
    elif name in ("transolver", "transolver-style"):
        model = TransolverOperator(grid, cond_dim)
    else:
        raise ValueError(f"unknown model {name!r}; expected 'gino' or 'transolver'")
    assert_param_budget(model)
    return model


MODEL_NAMES = ("gino", "transolver")


def rel_l2(pred: torch.Tensor, true: torch.Tensor, eps: float = 1e-8) -> torch.Tensor:
    """Relative L2 error per batch element, flattened over all remaining dims."""
    p = pred.reshape(pred.shape[0], -1)
    t = true.reshape(true.shape[0], -1)
    return torch.linalg.vector_norm(p - t, dim=-1) / (torch.linalg.vector_norm(t, dim=-1) + eps)

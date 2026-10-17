"""
QPainter orb HUD — third centrepiece alongside the holographic face and reactor.

Mirrors the layered CodePen / openclaw-jarvis-ui look (wireframe icosahedron,
glow, pulse ring, frequency rings, particles) without WebGL, Three.js, or any
new dependency. Colours come from the caller so the hue wheel retints it for free.
"""

from __future__ import annotations

import math
import random
from typing import Sequence

from PyQt6.QtCore import QLineF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QPainter, QPainterPath, QPen, QRadialGradient,
)


def _blend(bg: QColor, col: QColor, a: float) -> QColor:
    """Pre-mix `col` at alpha `a` (0–1) onto `bg` — opaque fast path."""
    k = max(0.0, min(1.0, a))
    return QColor(
        int(bg.red()   + (col.red()   - bg.red())   * k),
        int(bg.green() + (col.green() - bg.green()) * k),
        int(bg.blue()  + (col.blue()  - bg.blue())  * k),
    )


def _icosahedron(radius: float = 1.0) -> tuple[list[tuple[float, float, float]],
                                               list[tuple[int, int]]]:
    """Unit icosahedron verts + unique edges (Three.js detail=0 equivalent)."""
    t = (1.0 + math.sqrt(5.0)) * 0.5
    raw = [
        (-1,  t,  0), ( 1,  t,  0), (-1, -t,  0), ( 1, -t,  0),
        ( 0, -1,  t), ( 0,  1,  t), ( 0, -1, -t), ( 0,  1, -t),
        ( t,  0, -1), ( t,  0,  1), (-t,  0, -1), (-t,  0,  1),
    ]
    verts: list[tuple[float, float, float]] = []
    for x, y, z in raw:
        n = math.sqrt(x * x + y * y + z * z) or 1.0
        verts.append((x / n * radius, y / n * radius, z / n * radius))

    faces = (
        (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
        (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
        (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
        (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1),
    )
    edge_set: set[tuple[int, int]] = set()
    for a, b, c in faces:
        for u, v in ((a, b), (b, c), (c, a)):
            edge_set.add((u, v) if u < v else (v, u))
    return verts, sorted(edge_set)


def _subdivide(verts: list[tuple[float, float, float]],
               faces: list[tuple[int, int, int]],
               radius: float) -> tuple[list[tuple[float, float, float]],
                                       list[tuple[int, int, int]]]:
    """One Loop-style mid-edge subdivision for a denser wireframe."""
    mid_cache: dict[tuple[int, int], int] = {}
    out_verts = list(verts)

    def midpoint(i: int, j: int) -> int:
        key = (i, j) if i < j else (j, i)
        if key in mid_cache:
            return mid_cache[key]
        ax, ay, az = verts[i]
        bx, by, bz = verts[j]
        x, y, z = (ax + bx) * 0.5, (ay + by) * 0.5, (az + bz) * 0.5
        n = math.sqrt(x * x + y * y + z * z) or 1.0
        idx = len(out_verts)
        out_verts.append((x / n * radius, y / n * radius, z / n * radius))
        mid_cache[key] = idx
        return idx

    new_faces: list[tuple[int, int, int]] = []
    for a, b, c in faces:
        ab, bc, ca = midpoint(a, b), midpoint(b, c), midpoint(c, a)
        new_faces.extend(((a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)))
    return out_verts, new_faces


def _build_mesh(detail: int = 1, radius: float = 1.0):
    verts, edges0 = _icosahedron(radius)
    if detail <= 0:
        return verts, edges0
    faces = [
        (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
        (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
        (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
        (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1),
    ]
    for _ in range(detail):
        verts, faces = _subdivide(verts, faces, radius)
    edge_set: set[tuple[int, int]] = set()
    for a, b, c in faces:
        for u, v in ((a, b), (b, c), (c, a)):
            edge_set.add((u, v) if u < v else (v, u))
    return verts, sorted(edge_set)


_N_PARTICLES = 320
_RING_POINTS = 96
_CAM_D = 3.6
# Milder than the original 0.45 — still wavy, less aggressive.
_WAVE_AMOUNT = 0.18


class OrbHud:
    """Layered wireframe orb. One instance per HUD canvas.

    Lifecycle matches HoloAvatar:
        orb = OrbHud()
        orb.step(dt, amp, speaking=..., muted=..., state=...)
        orb.paint(painter, cx, cy, r, main, acc, bg, amp, state, ...)
    """

    def __init__(self) -> None:
        self._verts, self._edges = _build_mesh(detail=1, radius=1.0)
        self._yaw = 0.0
        self._pitch = 0.18
        self._phase = 0.0
        self._pulse = 1.0
        rng = random.Random(42)
        self._parts = [
            [
                rng.uniform(-1.8, 1.8),
                rng.uniform(-1.8, 1.8),
                rng.uniform(-1.8, 1.8),
                rng.uniform(0.4, 1.0),
                rng.uniform(0.15, 0.55),
                rng.uniform(-0.15, 0.15),
                rng.uniform(-0.12, 0.12),
                rng.uniform(-0.10, 0.10),
            ]
            for _ in range(_N_PARTICLES)
        ]

    def step(self, dt: float, amp: float, *, speaking: bool = False,
             muted: bool = False, state: str = "IDLE") -> None:
        dt = max(0.0, min(0.10, float(dt)))
        amp = max(0.0, min(1.0, float(amp)))
        rate = 0.55
        if muted:
            rate = 0.12
        elif state in ("THINKING", "PROCESSING"):
            rate = 1.55
        elif speaking:
            rate = 1.05
        elif amp > 0.04:
            rate = 0.85
        self._yaw += dt * rate
        self._pitch = 0.18 + 0.06 * math.sin(self._phase * 0.7)
        self._phase += dt * (1.0 + 0.8 * amp + (0.6 if speaking else 0.0))
        tgt = 1.0 + amp * (0.14 if speaking else 0.09)
        self._pulse += (tgt - self._pulse) * min(1.0, dt * 8.0)

        for p in self._parts:
            p[0] += p[5] * dt
            p[1] += p[6] * dt
            p[2] += p[7] * dt
            for i in range(3):
                if p[i] > 2.0:
                    p[i] = -2.0
                elif p[i] < -2.0:
                    p[i] = 2.0

    def paint(self, p: QPainter, cx: float, cy: float, r: float,
              main: QColor, acc: QColor, bg: QColor,
              amp: float, state: str, *, muted: bool = False,
              bands: Sequence[float] | None = None,
              W: float = 0.0, H: float = 0.0) -> None:
        if r < 8.0:
            return
        amp = max(0.0, min(1.0, float(amp)))
        live = (not muted) and (amp > 0.04 or state in ("THINKING", "PROCESSING")
                                or "SPEAK" in state.upper())

        # 1. Atmosphere — centre-bright soft bloom (original look)
        lift = 1.0 + 0.5 * amp + (0.15 if live else 0.0)
        p.setPen(Qt.PenStyle.NoPen)
        for gr, a0 in ((r * 1.15, 0.22), (r * 0.62, 0.28), (r * 0.30, 0.36)):
            g = QRadialGradient(cx, cy, gr)
            g.setColorAt(0.00, _blend(bg, main, min(0.9, a0 * lift)))
            g.setColorAt(0.55, _blend(bg, main, min(0.9, a0 * lift * 0.45)))
            g.setColorAt(1.00, _blend(bg, main, 0.0))
            p.setBrush(QBrush(g))
            p.drawEllipse(QRectF(cx - gr, cy - gr, gr * 2, gr * 2))
        p.setBrush(Qt.BrushStyle.NoBrush)

        # 2. Particles behind
        self._paint_particles(p, cx, cy, r, main, bg, amp, behind=True)

        # 3. Back-face glow shell — bright core, soft falloff
        glow_r = r * 0.92 * self._pulse
        p.setPen(Qt.PenStyle.NoPen)
        gg = QRadialGradient(cx, cy, glow_r)
        gg.setColorAt(0.00, _blend(bg, acc, 0.18 + 0.25 * amp))
        gg.setColorAt(0.55, _blend(bg, main, 0.10 + 0.12 * amp))
        gg.setColorAt(1.00, _blend(bg, main, 0.0))
        p.setBrush(QBrush(gg))
        p.drawEllipse(QRectF(cx - glow_r, cy - glow_r, glow_r * 2, glow_r * 2))
        p.setBrush(Qt.BrushStyle.NoBrush)

        # 4. Wireframe icosahedron
        self._paint_mesh(p, cx, cy, r * 0.78 * self._pulse, main, acc, bg, amp)

        # 5. Pulse ring
        pr = r * 1.05 * self._pulse
        p.setPen(QPen(_blend(bg, acc if live else main, 0.28 + 0.45 * amp), 1.6))
        p.drawEllipse(QRectF(cx - pr, cy - pr, pr * 2, pr * 2))
        p.setPen(QPen(_blend(bg, main, 0.12 + 0.2 * amp), 1.0))
        p.drawEllipse(QRectF(cx - pr * 1.04, cy - pr * 1.04,
                             pr * 2.08, pr * 2.08))

        # 6. Mild wavy frequency rings
        self._paint_freq_rings(p, cx, cy, r, main, acc, bg, amp, bands, muted)

        # 7. Near particles
        self._paint_particles(p, cx, cy, r, acc, bg, amp, behind=False)

    # ── internals ────────────────────────────────────────────────────────────

    def _project(self, x: float, y: float, z: float, cx: float, cy: float,
                 scale: float) -> tuple[float, float, float]:
        cy_, sy_ = math.cos(self._yaw), math.sin(self._yaw)
        x1 = x * cy_ - z * sy_
        z1 = x * sy_ + z * cy_
        cp, sp = math.cos(self._pitch), math.sin(self._pitch)
        y2 = y * cp - z1 * sp
        z2 = y * sp + z1 * cp
        d = _CAM_D
        f = d / (d + z2 + 1e-6)
        return cx + x1 * scale * f, cy - y2 * scale * f, z2

    def _paint_mesh(self, p: QPainter, cx: float, cy: float, scale: float,
                    main: QColor, acc: QColor, bg: QColor, amp: float) -> None:
        proj = [self._project(x, y, z, cx, cy, scale) for x, y, z in self._verts]
        buckets: list[list[QLineF]] = [[] for _ in range(4)]
        for i, j in self._edges:
            xi, yi, zi = proj[i]
            xj, yj, zj = proj[j]
            zmid = (zi + zj) * 0.5
            t = max(0.0, min(1.0, (zmid + 1.0) * 0.5))
            bi = min(3, int(t * 4))
            buckets[bi].append(QLineF(xi, yi, xj, yj))
        alphas = (0.18, 0.32, 0.48, 0.72 + 0.2 * amp)
        widths = (0.8, 1.0, 1.15, 1.35)
        for bi, lines in enumerate(buckets):
            if not lines:
                continue
            col = acc if bi == 3 and amp > 0.08 else main
            p.setPen(QPen(_blend(bg, col, alphas[bi]), widths[bi]))
            p.drawLines(lines)

    def _paint_freq_rings(self, p: QPainter, cx: float, cy: float, r: float,
                          main: QColor, acc: QColor, bg: QColor, amp: float,
                          bands: Sequence[float] | None, muted: bool) -> None:
        if muted:
            amp = 0.0
        n = _RING_POINTS
        if bands and len(bands) >= 8:
            derived = [max(0.0, min(1.0, float(b))) for b in bands]
        else:
            derived = [
                max(0.0, min(1.0,
                    amp * (0.55 + 0.45 * math.sin(self._phase * 2.1 + i * 0.37))
                    + 0.04 * math.sin(self._phase * 0.9 + i * 0.2)))
                for i in range(n)
            ]
        nb = len(derived)
        for ring in range(3):
            base = r * (0.88 + ring * 0.14)
            opacity = 0.75 - ring * 0.18
            width = 2.4 - ring * 0.45
            path = QPainterPath()
            for i in range(n):
                bi = (i + ring * (nb // 3)) % nb
                seg = derived[bi]
                for k in range(1, 3):
                    seg += derived[(bi + k) % nb]
                seg /= 3.0
                dyn = base * (1.0 + seg * _WAVE_AMOUNT)
                ang = (i / n) * math.pi * 2.0
                x = cx + math.cos(ang) * dyn
                y = cy + math.sin(ang) * dyn
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            path.closeSubpath()
            col = acc if ring == 0 else main
            p.setPen(QPen(_blend(bg, col, opacity), width))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

    def _paint_particles(self, p: QPainter, cx: float, cy: float, r: float,
                         col: QColor, bg: QColor, amp: float,
                         *, behind: bool) -> None:
        scale = r * 0.95
        pts_far: list[tuple[float, float, float, float]] = []
        pts_near: list[tuple[float, float, float, float]] = []
        for part in self._parts:
            x, y, z, sz, a0, *_ = part
            px, py, pz = self._project(x * 0.55, y * 0.55, z * 0.55, cx, cy, scale)
            if (px - cx) ** 2 + (py - cy) ** 2 > (r * 1.6) ** 2:
                continue
            entry = (px, py, sz, a0)
            if pz < 0.05:
                pts_far.append(entry)
            else:
                pts_near.append(entry)
        target = pts_far if behind else pts_near
        step = 1 if amp > 0.03 else 2
        for i, (px, py, sz, a0) in enumerate(target):
            if i % step:
                continue
            a = a0 * (0.55 + 0.45 * amp)
            s = 1.0 + sz * (1.2 + amp)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_blend(bg, col, a))
            p.drawEllipse(QRectF(px - s * 0.5, py - s * 0.5, s, s))
        p.setBrush(Qt.BrushStyle.NoBrush)

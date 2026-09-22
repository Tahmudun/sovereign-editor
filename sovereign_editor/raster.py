"""Orthographic inspection rasterizer: shared depth, cutouts and alpha blending."""
import numpy as np
from PIL import Image


def _linear(rgb):
    return np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)


def _sample_axis(uv, size, repeat, mirror):
    value = np.floor(uv * size).astype(np.int64)
    if not repeat:
        return np.clip(value, 0, size - 1)
    if mirror:
        value %= 2 * size
        return np.where(value >= size, 2 * size - 1 - value, value)
    return value % size


def render(primitives, size, projection, fit=False, background=(24, 34, 41)):
    """Render immutable primitives. Projection maps world XYZ to screen XY/depth."""
    width, height = size
    projected = [p.vertices @ np.asarray(projection).T for p in primitives]
    if fit:
        vertices = np.concatenate(projected)
        lo, hi = vertices[:, :2].min(0), vertices[:, :2].max(0)
        scale = min((width - 24) / max(hi[0] - lo[0], 1), (height - 24) / max(hi[1] - lo[1], 1))
        for v in projected:
            v[:, :2] = (v[:, :2] - (lo + hi) / 2) * scale + [width / 2, height / 2]
    frame = np.empty((height, width, 3), dtype=float)
    frame[:] = _linear(np.array(background) / 255)
    depth = np.full((height, width), -np.inf)
    opaque, translucent, textures = [], [], []
    for pi, (p, vertices) in enumerate(zip(primitives, projected)):
        texture = p.texture.astype(float) / 255 if p.texture is not None else None
        if texture is not None:
            texture[:, :, :3] = _linear(texture[:, :, :3])
        textures.append(texture)
        alpha = p.material["alpha"]
        soft = alpha < 1 or texture is not None and np.any((texture[:, :, 3] > 0) & (texture[:, :, 3] < 1))
        if alpha == 0:
            continue
        target = translucent if soft else opaque
        for ids in p.triangles:
            target.append((float(vertices[ids, 2].mean()), pi, ids))
    translucent.sort(key=lambda v: v[0])
    for _, pi, ids in opaque + translucent:
        p, vertices, texture = primitives[pi], projected[pi], textures[pi]
        tri = vertices[ids]
        a, b, c = tri[:, :2]
        den = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(den) < 1e-8:
            continue
        if den < 0:
            ids = ids[[0, 2, 1]]
            tri = vertices[ids]
            a, b, c = tri[:, :2]
            den = -den
        lo = np.maximum(np.floor(tri[:, :2].min(0)).astype(int), [0, 0])
        hi = np.minimum(np.ceil(tri[:, :2].max(0)).astype(int), [width - 1, height - 1])
        if (hi < lo).any():
            continue
        xx, yy = np.meshgrid(np.arange(lo[0], hi[0] + 1) + .5, np.arange(lo[1], hi[1] + 1) + .5)
        w0 = ((b[1] - c[1]) * (xx - c[0]) + (c[0] - b[0]) * (yy - c[1])) / den
        w1 = ((c[1] - a[1]) * (xx - c[0]) + (a[0] - c[0]) * (yy - c[1])) / den
        weights = np.stack([w0, w1, 1 - w0 - w1], axis=-1)
        mask = weights.min(-1) >= -1e-9
        # Half-open triangle edges avoid blending a shared diagonal twice.
        for k, (start, end) in enumerate(((b, c), (c, a), (a, b))):
            dx, dy = end - start
            inclusive = dy < 0 or dy == 0 and dx > 0
            if not inclusive:
                mask &= weights[:, :, k] > 1e-9
        region = np.s_[lo[1]:hi[1] + 1, lo[0]:hi[0] + 1]
        zz = weights @ tri[:, 2]
        mask &= zz >= depth[region] - 1e-7
        if not mask.any():
            continue
        rgb = weights @ p.colors[ids]
        alpha = np.full(xx.shape, p.material["alpha"], dtype=float)
        if texture is not None:
            uv = weights @ p.uvs[ids]
            repeat, mirror = p.material["repeat"], p.material["mirror"]
            tx = _sample_axis(uv[:, :, 0], texture.shape[1], repeat[0], mirror[0])
            ty = _sample_axis(uv[:, :, 1], texture.shape[0], repeat[1], mirror[1])
            sample = texture[ty, tx]
            rgb *= sample[:, :, :3]
            alpha *= sample[:, :, 3]
        mask &= alpha > 0
        blend = alpha[mask, None]
        frame[region][mask] = rgb[mask] * blend + frame[region][mask] * (1 - blend)
        solid = mask & (alpha >= 1 - 1e-9)
        depth[region][solid] = zz[solid]
    rgb = np.where(frame <= .0031308, frame * 12.92, 1.055 * np.maximum(frame, 0) ** (1 / 2.4) - .055)
    return Image.fromarray(np.rint(np.clip(rgb, 0, 1) * 255).astype(np.uint8))

import numpy as np
import pytest
import tifffile

SAMPLINGS = {"444": (1, 1), "422": (2, 1), "420": (2, 2)}


def make_image(w: int, h: int, seed: int = 0) -> np.ndarray:
    """Tissue-like test pattern: smooth colour fields, edges and noise, so JPEG blocks vary a lot."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    r = 150 + 90 * np.sin(x / 37.0) * np.cos(y / 53.0)
    g = 120 + 80 * np.sin((x + y) / 71.0)
    b = 170 + 60 * np.cos(x / 23.0 + y / 41.0)
    img = np.stack([r, g, b], axis=-1)
    img[(x - w / 2) ** 2 + (y - h / 2) ** 2 < (min(w, h) / 4) ** 2] *= 0.55  # a dark "tissue" disc with sharp edges
    img += rng.normal(0, 6, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


@pytest.fixture(params=list(SAMPLINGS))
def svs(request, tmp_path):
    """A three-level tiled JPEG TIFF with non-multiple-of-MCU sizes (exercises padding and edge tiles)."""
    sub = SAMPLINGS[request.param]
    base = make_image(1003, 709)
    levels = [base, base[::2, ::2], base[::4, ::4]]
    path = tmp_path / f"synthetic_{request.param}.svs"
    with tifffile.TiffWriter(path) as tif:
        for k, a in enumerate(levels):
            tif.write(
                a, tile=(256, 256), photometric="ycbcr", compression="jpeg", compressionargs={"level": 88},
                subsampling=sub, subfiletype=0 if k == 0 else 1,
                description="Aperio synthetic test | AppMag = 20 | MPP = 0.5" if k == 0 else None,
            )
    return str(path), request.param, levels

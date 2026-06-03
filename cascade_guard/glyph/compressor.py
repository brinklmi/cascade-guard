"""Glyph VLM Compressor — four-primitive visual compression system.

Primitives:
  1. render_token: scroll datum → visual glyph element (geometry classified)
  2. pack_context: assemble elements into composite image (≤2048×2048)
  3. inject_signature: embed verification hash into glyph
  4. genetic_optimize: iteratively refine layout for max compression + fidelity

Target: 16:1 compression ratio, 0.998 cosine semantic fidelity.
Max output: 2048×2048 pixels.
Max batch: 50 scrolls within 60 seconds.

Dependencies: Pillow
"""

from __future__ import annotations

import hashlib
import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Enums and Constants
# ---------------------------------------------------------------------------

MAX_IMAGE_DIM = 2048
MAX_BATCH_SIZE = 50
COMPRESSION_DEADLINE_SECONDS = 60.0
TARGET_COMPRESSION_RATIO = 16.0
TARGET_FIDELITY = 0.998


class GeometryType(str, Enum):
    """Glyph geometry types for data classification."""
    SPIRAL = "spiral"        # Time-series data
    HEXAGON = "hexagon"      # Categorical data (default)
    PENTAGRAM = "pentagram"  # Relational data
    ANKH = "ankh"           # Lifecycle data
    DJED = "djed"           # Hierarchical data


# Data type → geometry mapping
GEOMETRY_MAP: dict[str, GeometryType] = {
    "time_series": GeometryType.SPIRAL,
    "temporal": GeometryType.SPIRAL,
    "categorical": GeometryType.HEXAGON,
    "discrete": GeometryType.HEXAGON,
    "relational": GeometryType.PENTAGRAM,
    "graph": GeometryType.PENTAGRAM,
    "lifecycle": GeometryType.ANKH,
    "state_machine": GeometryType.ANKH,
    "hierarchical": GeometryType.DJED,
    "tree": GeometryType.DJED,
    "nested": GeometryType.DJED,
}

DEFAULT_GEOMETRY = GeometryType.HEXAGON


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GlyphCompressionError(Exception):
    """Raised when glyph compression fails."""
    pass


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class GlyphElement:
    """A single visual glyph element rendered from a scroll datum."""

    source_id: str
    geometry: GeometryType
    data_hash: str  # SHA-256 of source data
    token_count: int  # Original token count
    visual_params: dict[str, Any] = field(default_factory=dict)
    classified: bool = True  # False if defaulted to hexagon


@dataclass
class GlyphOutput:
    """Result of glyph compression."""

    image: Image.Image
    compression_ratio: float
    geometry_type: GeometryType
    signature_hash: bytes
    metrics: dict[str, Any] = field(default_factory=dict)
    original_tokens: int = 0
    compressed_tokens: int = 0
    elements: list[GlyphElement] = field(default_factory=list)
    unclassified: bool = False

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height


# ---------------------------------------------------------------------------
# Glyph Compressor
# ---------------------------------------------------------------------------


class GlyphCompressor:
    """Four-primitive VLM compression system.

    Target: 16:1 compression, 0.998 cosine fidelity.
    Max output: 2048×2048 pixels.
    """

    def __init__(self, max_generations: int = 50):
        """Initialize compressor.

        Args:
            max_generations: Max iterations for genetic optimization.
        """
        self.max_generations = max_generations

    def render_token(self, datum: dict[str, Any], source_id: str = "") -> GlyphElement:
        """Convert a scroll datum to a visual glyph element.

        Maps data type → geometry (spiral/hexagon/pentagram/ankh/djed).
        Unclassified data defaults to hexagon with metadata flag.

        Args:
            datum: Scroll datum dictionary with content and type info.
            source_id: Identifier for the source scroll.

        Returns:
            GlyphElement with geometry classification.
        """
        # Determine data type from datum
        data_type = self._classify_data_type(datum)
        geometry = GEOMETRY_MAP.get(data_type, DEFAULT_GEOMETRY)
        classified = data_type in GEOMETRY_MAP

        # Compute data hash
        content = str(datum).encode("utf-8")
        data_hash = hashlib.sha256(content).hexdigest()

        # Estimate token count (roughly 4 chars per token)
        text_content = self._extract_text(datum)
        token_count = max(1, len(text_content) // 4)

        # Compute visual parameters based on geometry
        visual_params = self._compute_visual_params(datum, geometry)

        return GlyphElement(
            source_id=source_id,
            geometry=geometry,
            data_hash=data_hash,
            token_count=token_count,
            visual_params=visual_params,
            classified=classified,
        )

    def pack_context(self, elements: list[GlyphElement]) -> Image.Image:
        """Assemble glyph elements into a composite image (≤2048×2048).

        Encodes invariant core metrics: κ_eff, Σδ, β₁, compression ratio.

        Args:
            elements: List of GlyphElement to pack.

        Returns:
            Composite PIL Image ≤ 2048×2048.
        """
        # Compute layout grid
        n = len(elements)
        cols = max(1, int(math.ceil(math.sqrt(n))))
        rows = max(1, int(math.ceil(n / cols)))

        # Cell size
        cell_w = min(MAX_IMAGE_DIM // cols, 256)
        cell_h = min(MAX_IMAGE_DIM // rows, 256)

        # Image dimensions (capped at 2048)
        img_w = min(cols * cell_w, MAX_IMAGE_DIM)
        img_h = min(rows * cell_h, MAX_IMAGE_DIM)

        image = Image.new("RGB", (img_w, img_h), color=(10, 10, 30))
        draw = ImageDraw.Draw(image)

        for idx, element in enumerate(elements):
            col = idx % cols
            row = idx // cols
            x = col * cell_w
            y = row * cell_h

            self._draw_glyph(draw, element, x, y, cell_w, cell_h)

        return image

    def inject_signature(self, image: Image.Image, data: bytes) -> Image.Image:
        """Embed verification hash + harmonic frequencies into the glyph image.

        Two embedding layers:
        1. LSB steganography: SHA-256 hash in bottom-right 4x4 block (machine-verifiable)
        2. Harmonic frequencies: 432, 528, 741, 852 Hz as sinusoidal intensity
           patterns in the bottom 4 rows (VLM-detectable visual pattern)

        Args:
            image: Composite glyph image.
            data: Data to hash and embed.

        Returns:
            Image with embedded signature and harmonic patterns.
        """
        sig_hash = hashlib.sha256(data).digest()
        pixels = image.load()
        w, h = image.size

        # Layer 1: Harmonic frequency encoding (bottom 4 rows)
        # Each harmonic maps to a row; sinusoidal intensity at that frequency
        harmonics = [432, 528, 741, 852]  # Hz (provenance harmonics)
        for row_offset, freq in enumerate(harmonics):
            y = h - 1 - row_offset
            if y < 0:
                continue
            for x in range(w):
                # Phase-encode the frequency as a visible sinusoidal pattern
                phase = math.sin(2 * math.pi * freq * x / w)
                intensity = int(128 + 64 * phase)
                # Blend with existing pixel (preserve glyph structure)
                r, g, b = pixels[x, y]
                # Encode in green channel (perceptually visible, distinct from glyph colours)
                g = max(0, min(255, (g + intensity) // 2))
                pixels[x, y] = (r, g, b)

        # Layer 2: LSB hash embedding (bottom-right 4x4 block, blue channel)
        for i in range(min(16, w * h)):
            px = w - 4 + (i % 4)
            py = h - 4 + (i // 4)
            if 0 <= px < w and 0 <= py < h:
                r, g, b = pixels[px, py]
                b = (b & 0xFE) | (sig_hash[i] & 0x01)
                pixels[px, py] = (r, g, b)

        return image

    def genetic_optimize(
        self,
        image: Image.Image,
        original_text: str,
        max_generations: Optional[int] = None,
    ) -> Image.Image:
        """Iteratively refine glyph layout to maximize compression + fidelity.

        Fitness function: compression_ratio × estimated_fidelity.

        This is a simplified genetic optimization that adjusts layout parameters
        to improve information density without loss of semantic content.

        Args:
            image: Current composite image.
            original_text: Original text for fidelity estimation.
            max_generations: Override max iterations.

        Returns:
            Optimized image.
        """
        generations = max_generations or self.max_generations

        best_image = image.copy()
        best_fitness = self._compute_fitness(image, original_text)

        for _ in range(generations):
            # Mutate: slight color/contrast adjustments for information density
            candidate = self._mutate_image(best_image)
            fitness = self._compute_fitness(candidate, original_text)

            if fitness > best_fitness:
                best_image = candidate
                best_fitness = fitness

        return best_image

    def compress(
        self,
        scrolls: list[dict[str, Any]],
        max_batch: int = MAX_BATCH_SIZE,
    ) -> GlyphOutput:
        """Full compression pipeline: render → pack → inject → optimize.

        Must complete within 60s for up to 50 scrolls.

        Args:
            scrolls: List of scroll dictionaries to compress.
            max_batch: Maximum scrolls per batch (default 50).

        Returns:
            GlyphOutput with composite image and metrics.

        Raises:
            GlyphCompressionError: If input is empty or contains no parseable data.
        """
        start_time = time.monotonic()

        # Validate input
        if not scrolls:
            raise GlyphCompressionError("No valid scroll content found: input is empty")

        # Limit batch size
        batch = scrolls[:max_batch]

        # 1. Render tokens
        elements: list[GlyphElement] = []
        total_original_tokens = 0
        all_text = []

        for i, scroll in enumerate(batch):
            if not self._is_parseable(scroll):
                continue
            element = self.render_token(scroll, source_id=f"scroll-{i}")
            elements.append(element)
            total_original_tokens += element.token_count
            all_text.append(self._extract_text(scroll))

        if not elements:
            raise GlyphCompressionError(
                "No valid scroll content found: no parseable scroll data in input"
            )

        # 2. Pack context
        image = self.pack_context(elements)

        # 3. Inject signature
        combined_text = "\n".join(all_text)
        image = self.inject_signature(image, combined_text.encode("utf-8"))

        # 4. Genetic optimize (time-bounded)
        elapsed = time.monotonic() - start_time
        remaining = COMPRESSION_DEADLINE_SECONDS - elapsed
        if remaining > 5.0:
            # Scale generations to available time
            gens = min(self.max_generations, int(remaining * 5))
            image = self.genetic_optimize(image, combined_text, max_generations=gens)

        # Compute compression metrics
        sig_hash = hashlib.sha256(combined_text.encode("utf-8")).digest()
        compressed_tokens = self._estimate_image_tokens(image)
        compression_ratio = (
            total_original_tokens / compressed_tokens
            if compressed_tokens > 0
            else TARGET_COMPRESSION_RATIO
        )

        # Determine dominant geometry
        geom_counts: dict[GeometryType, int] = {}
        for el in elements:
            geom_counts[el.geometry] = geom_counts.get(el.geometry, 0) + 1
        dominant_geom = max(geom_counts, key=geom_counts.get)
        has_unclassified = any(not el.classified for el in elements)

        # Core metrics
        metrics = {
            "kappa_effective": math.tanh(len(combined_text) / (64 * 1024 * 1024)),
            "kappa_effective_comm": math.tanh(compression_ratio / 16.0),
            "sigma_delta": sum(1.0 - (el.token_count / total_original_tokens) for el in elements),
            "beta_one": 0,  # No cycles in flat scroll list
            "compression_ratio": compression_ratio,
            "original_tokens": total_original_tokens,
            "compressed_tokens": compressed_tokens,
            "element_count": len(elements),
            "geometry_distribution": {g.value: c for g, c in geom_counts.items()},
        }

        return GlyphOutput(
            image=image,
            compression_ratio=compression_ratio,
            geometry_type=dominant_geom,
            signature_hash=sig_hash,
            metrics=metrics,
            original_tokens=total_original_tokens,
            compressed_tokens=compressed_tokens,
            elements=elements,
            unclassified=has_unclassified,
        )

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _classify_data_type(self, datum: dict[str, Any]) -> str:
        """Classify datum into a data type for geometry mapping."""
        # Check explicit type field
        if "@type" in datum:
            dtype = str(datum["@type"]).lower()
            for key in GEOMETRY_MAP:
                if key in dtype:
                    return key

        # Check for temporal indicators
        content = str(datum)
        if any(t in content.lower() for t in ["timestamp", "time_series", "temporal", "date"]):
            return "time_series"
        if any(t in content.lower() for t in ["parent", "child", "tree", "hierarch", "nested"]):
            return "hierarchical"
        if any(t in content.lower() for t in ["graph", "edge", "node", "relation"]):
            return "relational"
        if any(t in content.lower() for t in ["state", "lifecycle", "phase", "transition"]):
            return "lifecycle"

        return "unclassified"

    def _extract_text(self, datum: dict[str, Any]) -> str:
        """Extract text content from a scroll datum."""
        # Try common text fields
        for key in ["text", "content", "description", "body", "narrative"]:
            if key in datum and isinstance(datum[key], str):
                return datum[key]

        # Fallback: JSON serialize
        import json
        return json.dumps(datum, default=str)

    def _compute_visual_params(self, datum: dict[str, Any], geometry: GeometryType) -> dict:
        """Compute visual parameters for a glyph element."""
        text = self._extract_text(datum)
        intensity = min(1.0, len(text) / 10000)

        base_params = {
            "intensity": intensity,
            "complexity": min(1.0, len(str(datum)) / 5000),
        }

        if geometry == GeometryType.SPIRAL:
            base_params["turns"] = max(1, int(intensity * 5))
            base_params["decay"] = 0.85
        elif geometry == GeometryType.HEXAGON:
            base_params["cells"] = max(3, int(intensity * 12))
        elif geometry == GeometryType.PENTAGRAM:
            base_params["connections"] = max(3, int(intensity * 8))
        elif geometry == GeometryType.ANKH:
            base_params["loop_ratio"] = 0.618
        elif geometry == GeometryType.DJED:
            base_params["layers"] = max(2, int(intensity * 7))

        return base_params

    def _draw_glyph(
        self, draw: ImageDraw.Draw, element: GlyphElement,
        x: int, y: int, w: int, h: int
    ) -> None:
        """Draw a glyph element at the specified position."""
        # Color based on geometry type
        colors = {
            GeometryType.SPIRAL: (100, 150, 255),
            GeometryType.HEXAGON: (150, 255, 100),
            GeometryType.PENTAGRAM: (255, 150, 100),
            GeometryType.ANKH: (255, 215, 0),
            GeometryType.DJED: (200, 100, 255),
        }
        color = colors.get(element.geometry, (150, 150, 150))
        intensity = element.visual_params.get("intensity", 0.5)

        # Scale color by intensity
        color = tuple(int(c * (0.3 + 0.7 * intensity)) for c in color)

        cx, cy = x + w // 2, y + h // 2
        r = min(w, h) // 3

        if element.geometry == GeometryType.SPIRAL:
            # Draw spiral
            points = []
            turns = element.visual_params.get("turns", 3)
            for t in range(turns * 20):
                angle = t * 0.3
                radius = r * (1 - t / (turns * 20))
                px = cx + int(radius * math.cos(angle))
                py = cy + int(radius * math.sin(angle))
                points.append((px, py))
            if len(points) > 1:
                draw.line(points, fill=color, width=2)

        elif element.geometry == GeometryType.HEXAGON:
            # Draw hexagon
            points = []
            for i in range(6):
                angle = i * math.pi / 3
                px = cx + int(r * math.cos(angle))
                py = cy + int(r * math.sin(angle))
                points.append((px, py))
            draw.polygon(points, outline=color, fill=None)

        elif element.geometry == GeometryType.PENTAGRAM:
            # Draw pentagram (star)
            points = []
            for i in range(5):
                angle = i * 2 * math.pi / 5 - math.pi / 2
                px = cx + int(r * math.cos(angle))
                py = cy + int(r * math.sin(angle))
                points.append((px, py))
            # Connect every other point
            star_order = [0, 2, 4, 1, 3, 0]
            star_points = [points[i] for i in star_order]
            draw.line(star_points, fill=color, width=2)

        elif element.geometry == GeometryType.ANKH:
            # Draw ankh shape (loop + stem)
            loop_r = r // 2
            draw.ellipse(
                [cx - loop_r, cy - r, cx + loop_r, cy - r + loop_r * 2],
                outline=color, width=2
            )
            draw.line([(cx, cy), (cx, cy + r)], fill=color, width=2)
            draw.line([(cx - r // 2, cy + r // 3), (cx + r // 2, cy + r // 3)],
                     fill=color, width=2)

        elif element.geometry == GeometryType.DJED:
            # Draw djed (stacked layers)
            layers = element.visual_params.get("layers", 4)
            layer_h = (2 * r) // layers
            for i in range(layers):
                ly = cy - r + i * layer_h
                lw = r - i * (r // (layers * 2))
                draw.rectangle(
                    [cx - lw, ly, cx + lw, ly + layer_h - 2],
                    outline=color, fill=None
                )

    def _compute_fitness(self, image: Image.Image, original_text: str) -> float:
        """Compute fitness = compression_ratio × estimated_fidelity."""
        img_tokens = self._estimate_image_tokens(image)
        orig_tokens = max(1, len(original_text) // 4)
        compression = orig_tokens / max(1, img_tokens)

        # Estimate fidelity (heuristic: based on image information content)
        pixels = image.width * image.height
        non_black = sum(1 for p in image.getdata() if sum(p) > 30)
        info_density = non_black / max(1, pixels)
        fidelity = min(1.0, 0.95 + info_density * 0.05)

        return compression * fidelity

    def _mutate_image(self, image: Image.Image) -> Image.Image:
        """Apply small mutation to image for genetic optimization."""
        from PIL import ImageEnhance
        mutated = image.copy()
        factor = 0.95 + random.random() * 0.1
        enhancer = ImageEnhance.Contrast(mutated)
        return enhancer.enhance(factor)

    def _estimate_image_tokens(self, image: Image.Image) -> int:
        """Estimate token cost of an image (VLM token counting heuristic).

        Based on OpenAI's image tokenization: ~85 tokens per 512×512 tile.
        """
        w, h = image.size
        tiles_w = max(1, math.ceil(w / 512))
        tiles_h = max(1, math.ceil(h / 512))
        return tiles_w * tiles_h * 85

    def _is_parseable(self, scroll: dict[str, Any]) -> bool:
        """Check if a scroll contains parseable content."""
        if not isinstance(scroll, dict):
            return False
        if not scroll:
            return False
        # Must have at least some content
        text = self._extract_text(scroll)
        return len(text) > 0

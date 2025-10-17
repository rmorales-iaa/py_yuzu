#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
photometry_progress.py - Enhanced progress reporting for photometry operations

This module provides progress tracking utilities that can be integrated into
the photometry command to give users detailed feedback during long-running
photometry operations.

Usage:
    from photometry_progress import PhotometryProgress, ProgressTracker

    tracker = ProgressTracker(total_images=100, total_stars=5000)
    with PhotometryProgress(tracker, enabled=True) as progress:
        for img in images:
            progress.start_image(img)
            # ... do photometry ...
            progress.complete_image(measurements=120, saturated=5, indef=2)
"""

from __future__ import annotations

import sys
import time
import shutil
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path


@dataclass
class PhotometryStats:
    """Statistics for photometry operations."""
    total_images: int = 0
    processed_images: int = 0
    failed_images: int = 0
    total_stars: int = 0
    total_measurements: int = 0
    saturated_measurements: int = 0
    indef_measurements: int = 0
    snr_filtered: int = 0
    start_time: float = field(default_factory=time.time)
    current_filter: Optional[str] = None
    current_image: Optional[str] = None

    def get_success_rate(self) -> float:
        """Return percentage of successful images."""
        if self.processed_images == 0:
            return 0.0
        return ((self.processed_images - self.failed_images) / self.processed_images) * 100.0

    def get_elapsed_time(self) -> float:
        """Return elapsed time in seconds."""
        return time.time() - self.start_time

    def get_processing_rate(self) -> float:
        """Return images processed per second."""
        elapsed = self.get_elapsed_time()
        if elapsed <= 0:
            return 0.0
        return self.processed_images / elapsed

    def get_eta_seconds(self) -> Optional[float]:
        """Estimate time remaining in seconds."""
        if self.processed_images == 0 or self.total_images == 0:
            return None
        rate = self.get_processing_rate()
        if rate <= 0:
            return None
        remaining = self.total_images - self.processed_images
        return remaining / rate

    def format_time(self, seconds: float) -> str:
        """Format seconds as HH:MM:SS or MM:SS."""
        if seconds < 0:
            return "??:??"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"


class ProgressTracker:
    """Thread-safe progress tracker for photometry operations."""

    def __init__(self, total_images: int, total_stars: int):
        self.stats = PhotometryStats(total_images=total_images, total_stars=total_stars)
        self._last_update = time.time()
        self._update_interval = 1.0  # Update every second minimum

    def should_update(self) -> bool:
        """Check if enough time has passed for an update."""
        now = time.time()
        if now - self._last_update >= self._update_interval:
            self._last_update = now
            return True
        return False

    def start_image(self, image_path: str, pfilter: Optional[str] = None):
        """Mark the start of processing an image."""
        self.stats.current_image = str(Path(image_path).name)
        if pfilter:
            self.stats.current_filter = pfilter

    def complete_image(self, measurements: int = 0, saturated: int = 0,
                      indef: int = 0, snr_filtered: int = 0, failed: bool = False):
        """Mark completion of an image."""
        self.stats.processed_images += 1
        if failed:
            self.stats.failed_images += 1
        else:
            self.stats.total_measurements += measurements
            self.stats.saturated_measurements += saturated
            self.stats.indef_measurements += indef
            self.stats.snr_filtered += snr_filtered

    def get_stats(self) -> PhotometryStats:
        """Get current statistics."""
        return self.stats


class PhotometryProgress:
    """Context manager for displaying photometry progress."""

    def __init__(self, tracker: ProgressTracker, enabled: bool = True,
                 prefix: str = ">>", verbose: bool = False):
        self.tracker = tracker
        self.enabled = enabled and sys.stdout.isatty()
        self.prefix = prefix
        self.verbose = verbose
        self._last_line = ""
        self._in_progress = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._in_progress:
            self._clear_line()
        return False

    def _term_width(self) -> int:
        """Get terminal width."""
        try:
            return shutil.get_terminal_size(fallback=(80, 24)).columns
        except (OSError, AttributeError):
            return 80

    def _clear_line(self):
        """Clear the current line."""
        if not self.enabled:
            return
        try:
            width = self._term_width()
            sys.stdout.write("\r" + " " * width + "\r")
            sys.stdout.flush()
        except (OSError, UnicodeEncodeError):
            pass
        self._in_progress = False

    def _write_line(self, line: str, newline: bool = False):
        """Write a line to stdout."""
        if not self.enabled:
            return
        try:
            if newline:
                self._clear_line()
                print(line)
                self._in_progress = False
            else:
                sys.stdout.write("\r" + line)
                sys.stdout.flush()
                self._in_progress = True
                self._last_line = line
        except (OSError, UnicodeEncodeError):
            pass

    def print_above(self, text: str):
        """Print a line above the progress bar."""
        if self._in_progress:
            self._clear_line()
        print(text)
        if self._in_progress and self._last_line:
            self._write_line(self._last_line)

    def update(self):
        """Update the progress display."""
        if not self.enabled:
            return

        stats = self.tracker.get_stats()

        # Calculate progress percentage
        if stats.total_images > 0:
            pct = (stats.processed_images / stats.total_images) * 100.0
        else:
            pct = 0.0

        # Build progress bar
        bar_width = max(10, self._term_width() - 80)
        filled = int(round(bar_width * min(1.0, pct / 100.0)))
        bar = "#" * filled + "-" * (bar_width - filled)

        # Format ETA
        eta = stats.get_eta_seconds()
        eta_str = stats.format_time(eta) if eta is not None else "??:??"

        # Build status line
        line_parts = [
            f"{self.prefix} [{bar}] {pct:5.1f}%",
            f"{stats.processed_images}/{stats.total_images} images",
        ]

        if stats.failed_images > 0:
            line_parts.append(f"?{stats.failed_images}")

        rate = stats.get_processing_rate()
        if rate > 0:
            line_parts.append(f"{rate:.1f} img/s")

        line_parts.append(f"ETA {eta_str}")

        # Add measurements info if available
        if stats.total_measurements > 0:
            line_parts.append(f"{stats.total_measurements} meas")

        line = " | ".join(line_parts)

        # Truncate if too long
        max_width = self._term_width() - 2
        if len(line) > max_width:
            line = line[:max_width - 3] + "..."

        self._write_line(line)

    def update_detailed(self):
        """Show detailed progress with current filter and image."""
        if not self.enabled or not self.verbose:
            return

        stats = self.tracker.get_stats()

        if stats.current_image:
            msg = f"{self.prefix}   Processing: {stats.current_image}"
            if stats.current_filter:
                msg += f" [{stats.current_filter}]"
            self.print_above(msg)

    def finish(self):
        """Complete the progress bar."""
        if not self.enabled:
            return

        stats = self.tracker.get_stats()

        # Final progress line
        line = (f"{self.prefix} [{'#' * 20}] 100.0% | "
                f"{stats.processed_images}/{stats.total_images} images | "
                f"Done!")

        self._write_line(line, newline=True)

    def print_summary(self):
        """Print a summary of the photometry run."""
        stats = self.tracker.get_stats()
        elapsed = stats.get_elapsed_time()

        print(f"\n{self.prefix}")
        print(f"{self.prefix}{'=' * 60}")
        print(f"{self.prefix}PHOTOMETRY SUMMARY")
        print(f"{self.prefix}{'=' * 60}")
        print(f"{self.prefix}Total execution time: {stats.format_time(elapsed)}")
        print(f"{self.prefix}Images processed: {stats.processed_images}/{stats.total_images}")

        if stats.failed_images > 0:
            print(f"{self.prefix}Failed images: {stats.failed_images} "
                  f"({(stats.failed_images/stats.total_images)*100:.1f}%)")

        print(f"{self.prefix}Success rate: {stats.get_success_rate():.1f}%")
        print(f"{self.prefix}Processing rate: {stats.get_processing_rate():.2f} images/second")
        print(f"{self.prefix}")
        print(f"{self.prefix}Measurements:")
        print(f"{self.prefix}  Total valid: {stats.total_measurements}")

        if stats.saturated_measurements > 0:
            pct = (stats.saturated_measurements / max(1, stats.total_measurements)) * 100
            print(f"{self.prefix}  Saturated: {stats.saturated_measurements} ({pct:.1f}%)")

        if stats.indef_measurements > 0:
            print(f"{self.prefix}  INDEF: {stats.indef_measurements}")

        if stats.snr_filtered > 0:
            print(f"{self.prefix}  Filtered (SNR): {stats.snr_filtered}")

        if stats.total_stars > 0 and stats.total_measurements > 0:
            avg_per_star = stats.total_measurements / stats.total_stars
            avg_per_img = stats.total_measurements / max(1, stats.processed_images - stats.failed_images)
            print(f"{self.prefix}  Average per star: {avg_per_star:.1f}")
            print(f"{self.prefix}  Average per image: {avg_per_img:.1f}")

        print(f"{self.prefix}{'=' * 60}")
        print(f"{self.prefix}")


if __name__ == "__main__":
    # Demo
    import random

    def mock_photometry(img):
        time.sleep(0.1)  # Simulate work
        return {
            'measurements': random.randint(100, 150),
            'saturated': random.randint(0, 5),
            'indef': random.randint(0, 3),
            'snr_filtered': random.randint(0, 10),
        }

    print("Demo: Photometry Progress Tracker")
    print("=" * 60)

    mock_images = [f"image_{i:03d}.fits" for i in range(50)]
    example_photometry_with_progress(mock_images, 1200, mock_photometry)

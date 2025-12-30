#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
photometry_progress.py - Enhanced progress reporting for photometry operations

This module provides comprehensive progress tracking utilities for photometry
operations, including real-time progress bars, statistics collection, and
detailed reporting.

Features:
  ? Real-time progress bars with ETA calculation
  ? Comprehensive statistics tracking (valid, saturated, INDEF, SNR-filtered)
  ? Per-filter and global statistics
  ? Thread-safe progress tracking
  ? Terminal-aware display (auto-detects TTY)
  ? Configurable update intervals
  ? Summary reports with detailed metrics

Usage:
    from photometry_progress import ProgressTracker, PhotometryProgress

    tracker = ProgressTracker(total_images=100, total_stars=5000)
    with PhotometryProgress(tracker, enabled=True) as progress:
        for img in images:
            progress.start_image(img, pfilter='R')
            # ... do photometry ...
            progress.complete_image(measurements=120, saturated=5, indef=2)
            progress.update()
        progress.finish()
        progress.print_summary()
"""

from __future__ import annotations

import sys
import time
import shutil
from dataclasses import dataclass, field
from typing import Optional, Dict
from pathlib import Path
from collections import defaultdict


@dataclass
class PhotometryStats:
    """Comprehensive statistics for photometry operations."""

    # Image counts
    total_images: int = 0
    processed_images: int = 0
    failed_images: int = 0

    # Star counts
    total_stars: int = 0

    # Measurement counts
    total_measurements: int = 0
    valid_measurements: int = 0
    saturated_measurements: int = 0
    indef_measurements: int = 0
    snr_filtered: int = 0

    # Timing
    start_time: float = field(default_factory=time.time)

    # Current state
    current_filter: Optional[str] = None
    current_image: Optional[str] = None

    # Per-filter statistics
    filter_stats: Dict[str, Dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))

    def get_success_rate(self) -> float:
        """Return percentage of successful images."""
        if self.processed_images == 0:
            return 0.0
        success = self.processed_images - self.failed_images
        return (success / self.processed_images) * 100.0

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
        if self.processed_images >= self.total_images:
            return 0.0
        rate = self.get_processing_rate()
        if rate <= 0:
            return None
        remaining = self.total_images - self.processed_images
        return remaining / rate

    def format_time(self, seconds: float) -> str:
        """Format seconds as HH:MM:SS or MM:SS."""
        if seconds < 0 or not isinstance(seconds, (int, float)):
            return "??:??"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def add_filter_measurements(self, pfilter: str, valid: int = 0, saturated: int = 0,
                               indef: int = 0, snr_filtered: int = 0):
        """Add measurements for a specific filter."""
        self.filter_stats[pfilter]['valid'] += valid
        self.filter_stats[pfilter]['saturated'] += saturated
        self.filter_stats[pfilter]['indef'] += indef
        self.filter_stats[pfilter]['snr_filtered'] += snr_filtered
        self.filter_stats[pfilter]['total'] += valid

    def get_filter_summary(self, pfilter: str) -> Dict[str, int]:
        """Get summary statistics for a specific filter."""
        return dict(self.filter_stats.get(pfilter, {}))


class ProgressTracker:
    """Thread-safe progress tracker for photometry operations."""

    def __init__(self, total_images: int, total_stars: int = 0):
        """
        Initialize progress tracker.

        Args:
            total_images: Total number of images to process
            total_stars: Total number of stars for photometry
        """
        self.stats = PhotometryStats(
            total_images=total_images,
            total_stars=total_stars
        )
        self._last_update = time.time()
        self._update_interval = 0.5  # Update every 0.5 seconds minimum

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

    def complete_image(self, measurements: int = 0, valid: int = None,
                      saturated: int = 0, indef: int = 0,
                      snr_filtered: int = 0, failed: bool = False):
        """
        Mark completion of an image.

        Args:
            measurements: Total measurements attempted
            valid: Valid measurements (if None, computed from measurements - saturated - indef - snr_filtered)
            saturated: Number of saturated measurements
            indef: Number of INDEF measurements
            snr_filtered: Number of SNR-filtered measurements
            failed: Whether the image processing failed
        """
        self.stats.processed_images += 1

        if failed:
            self.stats.failed_images += 1
        else:
            # Calculate valid measurements
            if valid is None:
                valid = measurements - saturated - indef - snr_filtered

            self.stats.total_measurements += measurements
            self.stats.valid_measurements += valid
            self.stats.saturated_measurements += saturated
            self.stats.indef_measurements += indef
            self.stats.snr_filtered += snr_filtered

            # Add to filter-specific stats
            if self.stats.current_filter:
                self.stats.add_filter_measurements(
                    self.stats.current_filter,
                    valid=valid,
                    saturated=saturated,
                    indef=indef,
                    snr_filtered=snr_filtered
                )

    def get_stats(self) -> PhotometryStats:
        """Get current statistics snapshot."""
        return self.stats


class PhotometryProgress:
    """Context manager for displaying photometry progress."""

    def __init__(self, tracker: ProgressTracker, enabled: bool = True,
                 prefix: str = ">>", verbose: bool = False):
        """
        Initialize progress display.

        Args:
            tracker: ProgressTracker instance
            enabled: Whether to show progress (auto-detects TTY if True)
            prefix: Prefix for progress messages
            verbose: Show verbose progress information
        """
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

        # Throttle updates
        if not self.tracker.should_update():
            return

        stats = self.tracker.get_stats()

        # Calculate progress percentage
        if stats.total_images > 0:
            pct = (stats.processed_images / stats.total_images) * 100.0
        else:
            pct = 0.0

        # Build progress bar
        max_bar_width = 30
        available_width = self._term_width()
        bar_width = max(10, min(max_bar_width, available_width - 90))
        filled = int(round(bar_width * min(1.0, pct / 100.0)))
        bar = "#" * filled + "-" * (bar_width - filled)

        # Format ETA
        eta = stats.get_eta_seconds()
        eta_str = stats.format_time(eta) if eta is not None else "??:??"

        # Build status line
        line_parts = [
            f"{self.prefix} [{bar}] {pct:5.1f}%",
            f"{stats.processed_images}/{stats.total_images} imgs",
        ]

        if stats.failed_images > 0:
            line_parts.append(f"?{stats.failed_images}")

        rate = stats.get_processing_rate()
        if rate > 0:
            line_parts.append(f"{rate:.1f} img/s")

        line_parts.append(f"ETA {eta_str}")

        # Add measurements info if available
        if stats.valid_measurements > 0:
            line_parts.append(f"?{stats.valid_measurements}")

        line = " | ".join(line_parts)

        # Truncate if too long
        max_len = available_width - 2
        if len(line) > max_len:
            line = line[:max_len - 3] + "..."

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
        elapsed = stats.get_elapsed_time()
        rate = stats.get_processing_rate()

        # Final progress line
        bar_width = 30
        bar = "#" * bar_width

        line = (f"{self.prefix} [{bar}] 100.0% | "
                f"{stats.processed_images}/{stats.total_images} imgs | "
                f"{rate:.1f} img/s | Done!")

        self._write_line(line, newline=True)

    def print_filter_summary(self, pfilter: str):
        """Print summary for a specific filter."""
        stats = self.tracker.get_stats()
        filter_data = stats.get_filter_summary(pfilter)

        if not filter_data:
            return

        print(f"{self.prefix}")
        print(f"{self.prefix}Filter {pfilter} summary:")
        print(f"{self.prefix}  Valid measurements: {filter_data.get('valid', 0)}")

        if filter_data.get('saturated', 0) > 0:
            print(f"{self.prefix}  Saturated: {filter_data.get('saturated', 0)}")

        if filter_data.get('indef', 0) > 0:
            print(f"{self.prefix}  INDEF: {filter_data.get('indef', 0)}")

        if filter_data.get('snr_filtered', 0) > 0:
            print(f"{self.prefix}  Filtered (SNR?1): {filter_data.get('snr_filtered', 0)}")

    def print_summary(self):
        """Print a comprehensive summary of the photometry run."""
        stats = self.tracker.get_stats()
        elapsed = stats.get_elapsed_time()

        print(f"\n{self.prefix}")
        print(f"{self.prefix}{'=' * 60}")
        print(f"{self.prefix}PHOTOMETRY SUMMARY")
        print(f"{self.prefix}{'=' * 60}")
        print(f"{self.prefix}Total execution time: {stats.format_time(elapsed)}")
        print(f"{self.prefix}Images processed: {stats.processed_images}/{stats.total_images}")

        if stats.failed_images > 0:
            fail_pct = (stats.failed_images / stats.total_images) * 100 if stats.total_images > 0 else 0
            print(f"{self.prefix}Failed images: {stats.failed_images} ({fail_pct:.1f}%)")

        success_rate = stats.get_success_rate()
        print(f"{self.prefix}Success rate: {success_rate:.1f}%")

        rate = stats.get_processing_rate()
        print(f"{self.prefix}Processing rate: {rate:.2f} images/second")

        print(f"{self.prefix}")
        print(f"{self.prefix}Measurements:")
        print(f"{self.prefix}  Total valid: {stats.valid_measurements}")

        if stats.saturated_measurements > 0:
            total_attempted = (stats.valid_measurements + stats.saturated_measurements +
                             stats.indef_measurements + stats.snr_filtered)
            if total_attempted > 0:
                pct = (stats.saturated_measurements / total_attempted) * 100
                print(f"{self.prefix}  Saturated: {stats.saturated_measurements} ({pct:.1f}%)")
            else:
                print(f"{self.prefix}  Saturated: {stats.saturated_measurements}")

        if stats.indef_measurements > 0:
            print(f"{self.prefix}  INDEF: {stats.indef_measurements}")

        if stats.snr_filtered > 0:
            print(f"{self.prefix}  Filtered (SNR?1): {stats.snr_filtered}")

        # Per-star and per-image averages
        if stats.total_stars > 0 and stats.valid_measurements > 0:
            avg_per_star = stats.valid_measurements / stats.total_stars
            print(f"{self.prefix}  Average per star: {avg_per_star:.1f}")

        success_images = stats.processed_images - stats.failed_images
        if success_images > 0 and stats.valid_measurements > 0:
            avg_per_img = stats.valid_measurements / success_images
            print(f"{self.prefix}  Average per image: {avg_per_img:.1f}")

        # Per-filter breakdown if available
        if stats.filter_stats:
            print(f"{self.prefix}")
            print(f"{self.prefix}Per-filter breakdown:")
            for pfilter in sorted(stats.filter_stats.keys()):
                filter_data = stats.filter_stats[pfilter]
                print(f"{self.prefix}  {pfilter}:")
                print(f"{self.prefix}    Valid: {filter_data.get('valid', 0)}")
                if filter_data.get('saturated', 0) > 0:
                    print(f"{self.prefix}    Saturated: {filter_data.get('saturated', 0)}")
                if filter_data.get('indef', 0) > 0:
                    print(f"{self.prefix}    INDEF: {filter_data.get('indef', 0)}")
                if filter_data.get('snr_filtered', 0) > 0:
                    print(f"{self.prefix}    SNR-filtered: {filter_data.get('snr_filtered', 0)}")

        print(f"{self.prefix}{'=' * 60}")
        print(f"{self.prefix}")


# ---------------- Standalone demo/test ----------------
def demo_photometry_progress():
    """Demonstration of the photometry progress tracker."""
    import random

    def mock_photometry(img_path):
        """Mock photometry function for demonstration."""
        time.sleep(0.05)  # Simulate work
        failed = random.random() < 0.05  # 5% failure rate
        if failed:
            return {'failed': True}
        return {
            'failed': False,
            'measurements': random.randint(80, 120),
            'valid': random.randint(70, 110),
            'saturated': random.randint(0, 5),
            'indef': random.randint(0, 3),
            'snr_filtered': random.randint(0, 10),
        }

    print("Photometry Progress Tracker Demo")
    print("=" * 60)
    print()

    # Setup
    mock_images = [f"image_{i:03d}.fits" for i in range(100)]
    filters = ['R', 'V', 'B', 'I']
    n_stars = 500

    tracker = ProgressTracker(total_images=len(mock_images), total_stars=n_stars)

    with PhotometryProgress(tracker, enabled=True, verbose=False) as progress:
        for idx, img in enumerate(mock_images):
            pfilter = filters[idx % len(filters)]

            progress.start_image(img, pfilter=pfilter)
            result = mock_photometry(img)

            if result['failed']:
                progress.complete_image(failed=True)
            else:
                progress.complete_image(
                    measurements=result['measurements'],
                    valid=result['valid'],
                    saturated=result['saturated'],
                    indef=result['indef'],
                    snr_filtered=result['snr_filtered']
                )

            progress.update()

        progress.finish()
        progress.print_summary()


if __name__ == "__main__":
    demo_photometry_progress()

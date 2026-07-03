from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def load_plot_module():
    module_path = Path("figure/plot_sota_comparison.py")
    spec = spec_from_file_location("plot_sota_comparison", module_path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_benchmark_config_and_relative_output_path(tmp_path):
    module = load_plot_module()

    assert module.BENCHMARKS == [
        ("SWE-bench\nVerified", 95.0),
        ("TerminalBench\n2.0", 83.4),
        ("SWE-bench\nPro", 80.0),
        ("MLE-Bench", 64.4),
        ("PaperBench", 62.6),
        ("LoopsBench", 24.2),
    ]
    assert module.DEFAULT_OUTPUT_PATH == Path("figure") / "sota-comparison.pdf"

    output_path = tmp_path / "sota-comparison.pdf"
    module.build_chart(output_path)

    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_lowest_baseline_for_headroom_is_paperbench():
    module = load_plot_module()

    assert module.lowest_baseline_benchmark() == ("PaperBench", 62.6, 4)


def test_layout_uses_narrow_bars_and_text_only_headroom():
    module = load_plot_module()

    fig, ax, bars = module.create_chart()

    assert module.BAR_WIDTH < 0.65
    assert all(bar.get_width() == module.BAR_WIDTH for bar in bars)
    assert min(np.diff(module.bar_positions())) > 1.0
    assert any(text.get_text().endswith("headroom") for text in ax.texts)
    assert not any(
        getattr(text, "arrow_patch", None) is not None for text in ax.texts
    )

    plt.close(fig)

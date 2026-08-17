"""One look for every figure in figures/."""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PALETTE = ["#3366aa", "#dd7733", "#339966", "#aa3355", "#7755aa", "#888888"]


def apply():
    plt.rcParams.update({
        "figure.figsize": (7.5, 4.2),
        "figure.dpi": 110,
        "savefig.dpi": 160,
        "savefig.bbox": "tight",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.alpha": 0.25,
        "axes.prop_cycle": matplotlib.cycler(color=PALETTE),
        "font.size": 10,
    })
    return plt

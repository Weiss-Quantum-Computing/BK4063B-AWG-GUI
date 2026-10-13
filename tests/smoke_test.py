"""Hardware-free smoke test for the BK4063B AWG GUI preview.

Run:  python tests\\smoke_test.py

Builds the whole App in a withdrawn window off screen (+6000+200, the lab
convention, so it never lifts over a panel in live use) and drives the
preview through the shared plot panel: a built pending record, a wheel-style
zoom, the view held across a redraw, Home, the hold dropped by a layout
change, the placeholder, and Save.

Two things are stubbed BEFORE the App is built, and must stay that way:

- CONFIG_PATH goes to a temp folder. The real one is
  %APPDATA%\\BK4063B-AWG-GUI\\config.json and the test must not touch it.
- App.do_connect is a no-op. The App schedules a connect 300 ms after start
  and root.update() fires it; a real connect would open the bench generator
  from a second process.

Nothing here talks to an instrument.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import numpy as np                      # noqa: E402
import tkinter as tk                    # noqa: E402

import bk4063b_awg_gui as gui           # noqa: E402

CHECKS = 0


def ok(cond, what):
    global CHECKS
    CHECKS += 1
    if not cond:
        raise AssertionError("FAILED: " + what)
    print("  ok  " + what)


def close(a, b, rel=1e-9):
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def same_lim(lim_a, lim_b):
    return close(lim_a[0], lim_b[0]) and close(lim_a[1], lim_b[1])


def main():
    ok(gui.labplot is not None, "labplot.py imports from beside the GUI")
    ok(gui.labplot.HAVE_MPL, "matplotlib available (the test needs a figure)")
    ok(gui.CH_COLOUR is gui.CH_COLOURS and gui.CH_COLOUR[1] == "#1f77b4"
       and gui.CH_COLOUR[2] == "#d62728", "CH_COLOUR is labplot's blue/red")

    tmp = tempfile.mkdtemp(prefix="bk4063b_smoke_")
    gui.CONFIG_PATH = os.path.join(tmp, "config.json")   # before App
    gui.App.do_connect = lambda self: None                # before App

    def no_dialog(*args, **kwargs):
        raise AssertionError("a dialog opened: %r %r" % (args, kwargs))
    gui.messagebox.showerror = no_dialog
    gui.messagebox.showwarning = no_dialog
    gui.messagebox.askyesno = no_dialog

    root = tk.Tk()
    root.withdraw()
    app = gui.App(root)
    root.geometry("+6000+200")
    root.update()
    ok(app.preview is not None and app.preview.toolbar is not None,
       "preview is a labplot.PlotPanel with a toolbar")
    ok(app.fig is app.preview.fig and app.canvas is app.preview.canvas,
       "App.fig / App.canvas are the panel's")
    ok(str(app.alias_note.master) == str(app.preview.controls),
       "trace switches live in panel.controls")

    # A built pending record, through the builder's own button handler.
    app.do_build()
    ok(app.arb_samples is not None and app.arb_samples.size >= 2,
       "do_build left a pending record (%s pts)"
       % (None if app.arb_samples is None else app.arb_samples.size))
    ok(app.show_pending.get(), "pending trace switched on by the build")
    app.redraw_preview()
    root.update()
    ok(len(app.fig.axes) == 1 and app.axes_key == (1, False),
       "one shared panel with both channels and the pending trace")
    ax = app.ax
    ok(len(ax.get_lines()) >= 1, "the pending trace is drawn")
    ok(not app.preview.view_locked and app.preview.view_note.cget("text") == "",
       "auto-scaling, no note, at the start")

    # Zoom about the middle, as the wheel would.
    x0, y0 = ax.get_xlim(), ax.get_ylim()
    xc, yc = 0.5 * (x0[0] + x0[1]), 0.5 * (y0[0] + y0[1])
    app.preview.zoom_at(ax, xc, yc, 0.8)
    zx, zy = ax.get_xlim(), ax.get_ylim()
    ok(app.preview.view_locked, "zoom_at locks the view")
    ok(app.preview.view_note.cget("text") == "zoomed - Home to auto-scale",
       "blue note says so")
    ok(close((zx[1] - zx[0]) / (x0[1] - x0[0]), 0.8) and
       close((zy[1] - zy[0]) / (y0[1] - y0[0]), 0.8), "zoomed by 0.8 in x and y")

    # The view is held across a redraw (a keystroke, here a switch).
    app.redraw_preview()
    root.update()
    ok(app.ax is ax, "redraw reused the axes (layout unchanged)")
    ok(same_lim(ax.get_xlim(), zx) and same_lim(ax.get_ylim(), zy),
       "view held across the redraw")
    ok(app.preview.view_locked, "still locked after the redraw")

    # Home releases it and auto-scales.
    app.preview.home()
    root.update()
    ok(not app.preview.view_locked and app.preview.view_note.cget("text") == "",
       "Home unlocks and clears the note")
    ok(same_lim(app.ax.get_xlim(), x0) and same_lim(app.ax.get_ylim(), y0),
       "Home auto-scaled back to the original limits")

    # A layout change that changes the axes count (1 panel -> 2 stacked
    # panels) drops the hold on its own.
    app.preview.zoom_at(app.ax, xc, yc, 0.8)
    ok(app.preview.view_locked, "locked again before the layout change")
    app.split_t.set(True)
    app.redraw_preview()
    root.update()
    ok(len(app.axes) == 2 and app.axes_key == (2, False),
       "separate time axes: two stacked panels")
    ok(not app.preview.view_locked, "hold dropped by the panel-count change")
    ok(all(a.get_xlabel() == "time (ms)" for a in app.axes),
       "every panel has its time axis labelled")

    # And one that keeps the count: two stacked panels (2 axes) -> one
    # twinned panel (also 2 axes). labplot's own check cannot tell these
    # apart; the GUI's layout flag has to.
    app.preview.zoom_at(app.axes[0], *[0.5 * sum(app.axes[0].get_xlim()),
                                       0.5 * sum(app.axes[0].get_ylim())], 0.8)
    ok(app.preview.view_locked, "locked on the stacked layout")
    app.split_t.set(False)
    app.split_y.set(True)
    app.redraw_preview()
    root.update()
    ok(app.axes_key == (1, True) and app.ax2 is not None
       and len(app.fig.axes) == 2, "separate Y axes: one panel, twinned")
    ok(not app.preview.view_locked,
       "hold dropped by a layout change that kept the axes count")
    ok(app.ax2.yaxis.get_label_position() == "right",
       "twinned axis labelled on the right")
    app.split_y.set(False)
    app.redraw_preview()
    root.update()
    ok(app.axes_key == (1, False) and len(app.fig.axes) == 1,
       "back to one shared panel")

    # Nothing selected: the placeholder, then the axes come back.
    for ch in gui.CHANNELS:
        app.show_ch[ch].set(False)
    app.show_pending.set(False)
    app.redraw_preview()
    root.update()
    ok(len(app.fig.axes) == 0 and app.axes_key is None,
       "placeholder clears the figure and forgets the layout")
    ok(any("nothing selected" in t.get_text() for t in app.fig.texts),
       "placeholder text says nothing is selected")
    for ch in gui.CHANNELS:
        app.show_ch[ch].set(True)
    app.show_pending.set(True)
    app.redraw_preview()
    root.update()
    ok(len(app.fig.axes) == 1 and app.axes_key == (1, False),
       "axes rebuilt after the placeholder")
    lg = app.ax.get_legend()
    ok(lg is not None and lg.get_texts()[0].get_fontsize() == 7,
       "legend at 7 pt from labplot.legend")

    # Save: the panel proposes the name and the output folder, and writes.
    ok(app.preview.proposed_name() == "awg_preview", "Save proposes awg_preview")
    app.outdir.set(tmp)
    ok(app.preview.proposed_dir() == tmp, "Save proposes the output folder")
    path = os.path.join(tmp, "awg_preview.png")
    out = app.preview.save_figure(path)
    ok(out == path and os.path.isfile(path) and os.path.getsize(path) > 1000,
       "save_figure wrote %s" % path)

    root.update()
    root.destroy()
    print("All %d checks passed." % CHECKS)


if __name__ == "__main__":
    main()

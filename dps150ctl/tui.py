"""Textual dashboard. All control actions go through the same local RPC client."""
import asyncio
from collections import deque
import math
import time

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Digits, Footer, Input, Static
from textual.worker import get_current_worker
from textual_plotext import PlotextPlot

from .config import tighter
from .errors import control_error
from .state import STALE_SECONDS

WINDOWS = (30, 120, 600)


class History:
    """Bounded real samples. Reconnects and missing data split plotted lines."""
    def __init__(self):
        self.samples = deque(maxlen=4096)
        self.last_time = None
        self.segment = 0
        self.gapped = False
        self.generation = None

    def add(self, report):
        connection = report["connection"]
        if connection["state"] != "connected":
            if not self.gapped:
                self.segment += 1
                self.gapped = True
            return
        timestamp = report.get("sample_monotonic")
        if timestamp is None or (self.last_time is not None and timestamp <= self.last_time):
            return
        if self.generation != connection["generation"] or (
            self.last_time is not None and timestamp - self.last_time > STALE_SECONDS
        ):
            self.segment += 1
        self.generation = connection["generation"]
        self.last_time, self.gapped = timestamp, False
        self.samples.append((timestamp, report["output_voltage_v"], report["output_current_a"], self.segment))
        while self.samples and timestamp - self.samples[0][0] > WINDOWS[-1]:
            self.samples.popleft()

    def series(self, window, field, now=None):
        now = time.monotonic() if now is None else now
        groups, current, previous = [], ([], []), None
        for sample in self.samples:
            if sample[0] < now - window:
                continue
            if sample[3] != previous:
                current = ([], [])
                groups.append(current)
                previous = sample[3]
            current[0].append(sample[0] - now)
            current[1].append(sample[field])
        return groups


class StateChanged(Message):
    def __init__(self, report):
        super().__init__()
        self.report = report


class StreamFailed(Message):
    def __init__(self, error):
        super().__init__()
        self.error = error


class SetpointScreen(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    SetpointScreen { align: center middle; background: transparent; }
    #form { width: 54; max-width: 95%; height: auto; padding: 1 2; border: round $primary; background: ansi_default; }
    #form-title { text-style: bold; color: $primary; margin-bottom: 1; }
    #form-range { height: auto; margin-bottom: 1; }
    #form-error { color: $error; height: auto; min-height: 1; margin-top: 1; }
    #form-buttons { height: 3; align-horizontal: right; margin-top: 1; }
    #form-buttons Button { margin-left: 1; min-width: 10; }
    """

    def __init__(self, field, value, maximum, output_on):
        super().__init__()
        self.field, self.value, self.maximum, self.output_on = field, value, maximum, output_on

    def compose(self) -> ComposeResult:
        unit = "V" if self.field == "voltage" else "A"
        with Vertical(id="form"):
            yield Static(f"Set {self.field}", id="form-title")
            note = "Applies immediately while output is on." if self.output_on else "Output remains off."
            yield Static(f"Allowed: 0–{self.maximum:g} {unit}\n{note}", id="form-range", markup=False)
            yield Input(f"{self.value:.3f}", select_on_focus=True, id="setpoint", type="text")
            yield Static("", id="form-error", markup=False)
            with Horizontal(id="form-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Apply", id="apply", variant="primary")

    def on_mount(self):
        self.query_one(Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#apply")
    async def apply_value(self):
        if self.app.pending:
            return
        try:
            value = float(self.query_one(Input).value)
            if not math.isfinite(value) or not 0 <= value <= self.maximum:
                raise ValueError(f"Enter a finite value from 0 to {self.maximum:g}.")
        except ValueError as exc:
            self.query_one("#form-error", Static).update(str(exc))
            return
        self.query_one("#apply", Button).disabled = True
        success, error = await self.app.issue("set", {self.field: value})
        self.query_one("#apply", Button).disabled = False
        if success:
            self.dismiss()
        else:
            self.query_one("#form-error", Static).update(error)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self):
        if not self.app.pending:
            self.dismiss()


class HelpScreen(ModalScreen):
    BINDINGS = [("escape", "close", "Close"), ("question_mark", "close", "Close")]
    CSS = """
    HelpScreen { align: center middle; background: transparent; }
    #help { width: 65; max-width: 95%; height: auto; padding: 1 2; border: round $primary; background: ansi_default; }
    """

    def compose(self):
        yield Static(
            "[b]dps150ctl · keyboard controls[/b]\n\n"
            "v / i   Set voltage / current limit\n"
            "o       Toggle output, verified by readback\n"
            "1 / 2 / 3   Numbers / charts / combined\n"
            "[ / ]   30 s / 2 min / 10 min chart window\n"
            "q       Quit dashboard\n\n"
            "Quitting preserves output and the panel lock.\n"
            "An attached headless service keeps running.\n"
            "Use 'dps150ctl unlock' to release the panel;\n"
            "that also stops the owning service.\n\n"
            "Old readings are dimmed; gaps are not connected.\n"
            "Software voltage caps are separate from hardware OVP.\n\n"
            "[dim]Escape to return[/dim]", id="help")

    def action_close(self):
        self.dismiss()


class Dashboard(App):
    TITLE = "dps150ctl"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("v", "edit_voltage", "Voltage"), Binding("i", "edit_current", "Current"),
        Binding("o", "toggle_output", "Output"),
        Binding("1", "view('numbers')", "Numbers", show=False),
        Binding("2", "view('charts')", "Charts", show=False),
        Binding("3", "view('combined')", "Combined", show=False),
        Binding("left_square_bracket", "window(-1)", "Shorter", show=False),
        Binding("right_square_bracket", "window(1)", "Longer", show=False),
        Binding("question_mark", "help", "Help"), Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]
    CSS = """
    Screen { background: $background; color: $foreground; }
    #masthead { height: 2; padding: 0 2; background: $panel; content-align: left middle; }
    #connection { height: auto; min-height: 2; padding: 0 2; margin-top: 1; }
    #notice { height: auto; min-height: 1; max-height: 4; padding: 0 2; color: $primary; }
    #notice.failed { color: $error; }
    #body { height: 1fr; padding: 0 1; }
    #numbers { height: 8; min-height: 8; }
    .reading { width: 1fr; height: 8; padding: 0 1; margin: 0 1; background: $surface; }
    #voltage-card { border: round $primary; }
    #current-card { border: round $secondary; }
    .reading-label { height: 1; text-align: center; text-style: bold; }
    #voltage-card .reading-label, #voltage { color: $primary; }
    #current-card .reading-label, #current { color: $secondary; }
    Digits { width: 100%; height: 3; text-align: center; }
    .setpoint { height: 1; text-align: center; color: $text-muted; }
    #numbers.stale Digits { opacity: 45%; }
    #details { height: auto; min-height: 2; padding: 0 2; margin-top: 1; }
    #charts { height: 1fr; min-height: 10; layout: horizontal; }
    #charts.stacked { layout: vertical; min-height: 18; }
    PlotextPlot { width: 1fr; height: 1fr; min-height: 8; margin: 0 1; padding: 0 1; border: round ansi_bright_black; }
    #view-label { height: 1; padding: 0 2; color: $text-muted; }
    Footer { background: $panel; }
    .hidden { display: none; }
    """

    def __init__(self, client, *, view="combined", max_voltage=None, demo=False, owns_service=False):
        super().__init__(ansi_color=True)
        self.client, self.view, self.max_voltage = client, view, max_voltage
        self.demo, self.owns_service = demo, owns_service
        self.report = None
        self.history = History()
        self.window_index = 1
        self.pending = False
        self.subscription_failed = False
        self.register_theme(Theme(name="dps150", ansi=True, primary="#edba68", secondary="#68dbcc",
            foreground="ansi_default", background="ansi_default", surface="ansi_default", panel="ansi_default",
            success="#68dbcc", warning="#edba68", error="#f48b91", dark=True,
            variables={"ansi-background": "ansi_default", "ansi-foreground": "ansi_default",
                "footer-key-foreground": "#edba68", "scrollbar-background": "ansi_default",
                "scrollbar-background-hover": "ansi_default", "scrollbar-background-active": "ansi_default"}))
        self.theme = "dps150"

    def compose(self):
        badge = "  [bold #edba68]DEMO · synthetic readings[/]" if self.demo else ""
        owner = "device owner" if self.owns_service else "attached to service"
        yield Static(f"[b]dps150ctl[/b]  /  FNIRSI DPS-150   [dim]{owner}[/]{badge}", id="masthead")
        yield Static("Connecting to device service…", id="connection", markup=False)
        yield Static("", id="notice", markup=False)
        with VerticalScroll(id="body"):
            with Horizontal(id="numbers"):
                with Vertical(id="voltage-card", classes="reading"):
                    yield Static("VOLTAGE / V", classes="reading-label")
                    yield Digits("--.---", id="voltage")
                    yield Static("Setpoint —", id="voltage-set", classes="setpoint", markup=False)
                with Vertical(id="current-card", classes="reading"):
                    yield Static("CURRENT / A", classes="reading-label")
                    yield Digits("-.---", id="current")
                    yield Static("Current limit —", id="current-set", classes="setpoint", markup=False)
            yield Static("Waiting for fresh measurements", id="details", markup=False)
            with Container(id="charts"):
                for name in ("voltage", "current"):
                    plot = PlotextPlot(id=f"{name}-chart")
                    # Auto themes convert the terminal default into an RGB fill.
                    # Plotext's clear theme leaves canvas, axes and ticks native.
                    plot.theme = "clear"
                    yield plot
        yield Static("", id="view-label", markup=False)
        yield Footer()

    def on_mount(self):
        self.follow_events()
        self.set_interval(0.25, self.draw)
        self.layout_view()

    @work(thread=True, exclusive=True, group="events")
    def follow_events(self):
        worker = get_current_worker()
        try:
            for event in self.client.events():
                if worker.is_cancelled:
                    break
                if event.get("event") in ("state", "ready"):
                    self.post_message(StateChanged(event["data"]))
                    if event["data"]["connection"]["state"] == "stopped":
                        break
        except Exception as exc:
            if not worker.is_cancelled:
                self.post_message(StreamFailed(control_error(exc)))

    def on_state_changed(self, message):
        self.report = message.report
        self.history.add(self.report)
        if self.report["connection"]["state"] == "stopped":
            self.exit()

    def on_stream_failed(self, message):
        self.subscription_failed = True
        self.notice(f"Service connection lost: {message.error}. Commands disabled; quit and reattach.", failed=True)

    def on_resize(self):
        if self.is_mounted:
            self.layout_view()

    def layout_view(self):
        compact = self.size.width < 76 or self.size.height < 22
        self.query_one("#numbers").set_class(self.view == "charts", "hidden")
        self.query_one("#charts").set_class(self.view == "numbers" or (compact and self.view == "combined"), "hidden")
        self.query_one("#charts").set_class(self.size.width < 100 and self.size.height >= 32 or self.size.width < 76, "stacked")
        window = WINDOWS[self.window_index]
        note = " · compact display: press 2 for charts" if compact and self.view == "combined" else ""
        self.query_one("#view-label", Static).update(
            f"1 numbers · 2 charts · 3 combined   |   [ ] history: {window}s{note}")

    def notice(self, message, *, failed=False):
        widget = self.query_one("#notice", Static)
        widget.update(message)
        widget.set_class(failed, "failed")

    def fresh(self):
        if not self.report or self.subscription_failed:
            return False
        sample = self.report.get("sample_monotonic")
        return (self.report["connection"]["state"] == "connected" and sample is not None
                and time.monotonic() - sample <= STALE_SECONDS)

    def maximum(self, field):
        maximum = self.report["limits"][f"max_{field}_{'v' if field == 'voltage' else 'a'}"]
        return tighter(maximum, self.max_voltage) if field == "voltage" else maximum

    def draw(self):
        if not self.report:
            return
        report, fresh = self.report, self.fresh()
        identity, connection = report["identity"], report["connection"]
        status = "service lost" if self.subscription_failed else connection["state"]
        if status == "connected" and not fresh:
            status = "stale"
        age = None if report.get("sample_monotonic") is None else max(0, time.monotonic() - report["sample_monotonic"])
        age_text = "no sample" if age is None else f"sample {age:.1f}s ago"
        retry = connection.get("retry_in_s")
        retry_text = f" · retry {retry:.0f}s" if retry is not None else ""
        line = Text(f"● {status.upper()}", style="bold #68dbcc" if fresh else "bold #edba68")
        line.append(f"   USB {identity.get('serial_number') or 'no stable serial'}  ·  {identity.get('port') or 'no tty'}\n")
        line.append(f"{age_text}{retry_text}  ·  voltage cap {self.maximum('voltage'):g} V  ·  panel lock requested (unverified)")
        if connection.get("last_error"):
            line.append(f"\n{connection['last_error']['message']}", style="#f48b91")
        self.query_one("#connection", Static).update(line)
        self.query_one("#numbers").set_class(not fresh, "stale")
        for name, field in (("voltage", "output_voltage_v"), ("current", "output_current_a")):
            value = report.get(field)
            self.query_one(f"#{name}", Digits).update(f"{value:.3f}" if value is not None else "--.---")
        self.query_one("#voltage-set", Static).update(f"Setpoint {report.get('set_voltage_v', 0):.3f} V   [v]")
        self.query_one("#current-set", Static).update(f"Limit {report.get('set_current_a', 0):.3f} A   [i]")
        output = {True: "ON", False: "OFF", None: "UNKNOWN"}.get(report.get("output_enabled"), "UNKNOWN")
        def value(key, unit, precision=2):
            return f"{report[key]:.{precision}f} {unit}" if key in report else "—"
        details = (f"OUTPUT {output} [o]   {report.get('mode', '—')}   ·   {value('output_power_w', 'W', 3)}"
                   f"   ·   input {value('input_voltage_v', 'V')}   ·   {value('temperature_c', '°C', 1)}\n"
                   f"Protection: {report.get('protection', '—')}  ·  Current range 0–{self.maximum('current'):g} A")
        if report.get("set_voltage_v", 0) > self.maximum("voltage"):
            details += "  ·  SETPOINT ABOVE VOLTAGE CAP"
        self.query_one("#details", Static).update(details)
        if not fresh:
            gap_report = {**report, "connection": {**connection, "state": "stale"}}
            self.history.add(gap_report)
        if not self.query_one("#charts").has_class("hidden"):
            self.draw_charts()

    def draw_charts(self):
        window, now = WINDOWS[self.window_index], time.monotonic()
        for name, field, unit, color, floor in (
            ("voltage", 1, "V", (237, 186, 104), 0.01), ("current", 2, "A", (104, 219, 204), 0.001)
        ):
            widget = self.query_one(f"#{name}-chart", PlotextPlot)
            widget.border_title = f"{name.upper()} / {unit}"
            plot = widget.plt
            plot.clear_data()
            groups = self.history.series(window, field, now)
            maximum = max((value for _, values in groups for value in values), default=0)
            plot.xlim(-window, 0)
            plot.ylim(0, max(floor, maximum * 1.15))
            plot.xticks([-window, -window / 2, 0], [f"−{window}s", f"−{window // 2}s", "now"])
            plot.yfrequency(4)
            plot.grid(True, True)
            for xs, ys in groups:
                plot.plot(xs, ys, marker="braille", color=color)
            widget.refresh()

    def action_view(self, view):
        if isinstance(self.screen, ModalScreen):
            return
        self.view = view
        self.layout_view()

    def action_window(self, direction):
        if not isinstance(self.screen, ModalScreen):
            self.window_index = max(0, min(len(WINDOWS) - 1, self.window_index + direction))
            self.layout_view()

    def edit(self, field):
        if isinstance(self.screen, ModalScreen) or self.pending:
            return
        if not self.fresh():
            self.notice("Waiting for fresh state; setpoint controls are disabled.", failed=True)
            return
        key = "set_voltage_v" if field == "voltage" else "set_current_a"
        self.push_screen(SetpointScreen(field, self.report[key], self.maximum(field), self.report.get("output_enabled")))

    def action_edit_voltage(self):
        self.edit("voltage")

    def action_edit_current(self):
        self.edit("current")

    async def action_toggle_output(self):
        if isinstance(self.screen, ModalScreen) or self.pending:
            return
        if not self.report or self.subscription_failed:
            return
        enabled = self.report.get("output_enabled")
        transport = self.report["connection"]["state"] in ("connected", "stale")
        if not transport or enabled is None or (enabled is False and not self.fresh()):
            self.notice("Output control needs a connected device; enabling also needs fresh state.", failed=True)
            return
        await self.issue("output", {"enabled": not enabled})

    async def issue(self, operation, params):
        self.pending = True
        self.notice("Applying command… waiting for device readback")
        try:
            report = await asyncio.to_thread(self.client.request, operation, {**params, "max_voltage": self.max_voltage})
            self.report = report
            self.history.add(report)
            self.notice("Verified by device readback.")
            return True, ""
        except Exception as exc:
            error = control_error(exc)
            self.notice(f"{error.code}: {error}", failed=True)
            return False, str(error)
        finally:
            self.pending = False

    def action_help(self):
        if not isinstance(self.screen, ModalScreen):
            self.push_screen(HelpScreen())

"""Dashboard NiceGUI per il monitoraggio del rumore da porta seriale."""
from __future__ import annotations

import csv
import io
import math
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import serial
from nicegui import ui
from serial.tools import list_ports

SPL = re.compile(r"\[SPL:\s*([0-9]+(?:[.,][0-9]+)?)\s*dB\]", re.I)
BAUDRATE = 115200


@dataclass
class Value:
    timestamp: datetime
    db: float
    seconds: int


@dataclass
class Monitor:
    low: float = 80.0
    high: float = 100.0
    period: int = 60
    port: str | None = None
    latest: float | None = None
    base: deque = field(default_factory=lambda: deque(maxlen=1500))
    hourly: deque = field(default_factory=lambda: deque(maxlen=1000))
    daily: deque = field(default_factory=lambda: deque(maxlen=730))
    _buffer: list = field(default_factory=list)
    _hour: list = field(default_factory=list)
    _day: list = field(default_factory=list)
    _base_start: datetime | None = None
    _hour_start: datetime | None = None
    _day_start: datetime | None = None
    _serial: serial.Serial | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    @staticmethod
    def laeq(values: list[float]) -> float:
        return 10 * math.log10(sum(10 ** (x / 10) for x in values) / len(values))

    @staticmethod
    def merge(values: list[Value]) -> float:
        duration = sum(x.seconds for x in values)
        return 10 * math.log10(sum(10 ** (x.db / 10) * x.seconds for x in values) / duration)

    def configure(self, low: float, high: float, period: int) -> None:
        if low >= high:
            raise ValueError("La soglia 1 deve essere minore della soglia 2")
        with self._lock:
            self.low, self.high = low, high
            if period != self.period:
                self.period, self._base_start = period, None
                self._buffer.clear()

    def add(self, db: float, at: datetime | None = None) -> None:
        at = at or datetime.now()
        with self._lock:
            self.latest = db
            if self._base_start is None:
                self._base_start = at
            self._buffer.append(db)
            if (at - self._base_start).total_seconds() >= self.period:
                item = Value(self._base_start, self.laeq(self._buffer), self.period)
                self.base.append(item)
                self._buffer.clear(); self._base_start = at
                self._add_hour(item)

    def _add_hour(self, item: Value) -> None:
        if self._hour_start is None: self._hour_start = item.timestamp
        self._hour.append(item)
        if (item.timestamp - self._hour_start).total_seconds() + item.seconds >= 3600:
            result = Value(self._hour_start, self.merge(self._hour), 3600)
            self.hourly.append(result); self._hour.clear(); self._hour_start = item.timestamp
            self._add_day(result)

    def _add_day(self, item: Value) -> None:
        if self._day_start is None: self._day_start = item.timestamp
        self._day.append(item)
        if (item.timestamp - self._day_start).total_seconds() + item.seconds >= 86400:
            self.daily.append(Value(self._day_start, self.merge(self._day), 86400))
            self._day.clear(); self._day_start = item.timestamp

    def connect(self, port: str) -> None:
        self.disconnect()
        connection = serial.Serial(port, BAUDRATE, timeout=1)
        with self._lock:
            self._serial, self.port = connection, port
            self._stop.clear()
        threading.Thread(target=self._reader, daemon=True).start()

    def disconnect(self) -> None:
        self._stop.set()
        with self._lock:
            connection, self._serial, self.port = self._serial, None, None
        if connection and connection.is_open: connection.close()

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock: connection = self._serial
                if not connection: return
                match = SPL.search(connection.readline().decode(errors="ignore"))
                if match: self.add(float(match.group(1).replace(",", ".")))
            except (serial.SerialException, OSError):
                self.disconnect(); return

    def snapshot(self):
        with self._lock:
            return self.latest, self.port, self.low, self.high, list(self.base), list(self.hourly), list(self.daily)

    def csv(self) -> bytes:
        with self._lock: rows = [("LAeqT", x) for x in self.base] + [("LAeq 1h", x) for x in self.hourly] + [("LAeq giornaliero", x) for x in self.daily]
        out = io.StringIO(newline=""); writer = csv.writer(out, delimiter=";")
        writer.writerow(["timestamp", "aggregazione", "laeq_dB", "durata_s"])
        for name, item in rows:
            writer.writerow([item.timestamp.strftime("%Y-%m-%d %H:%M:%S"), name, f"{item.db:.2f}".replace(".", ","), item.seconds])
        return out.getvalue().encode("utf-8-sig")


monitor = Monitor()
def available_ports(): return {p.device: f"{p.device} - {p.description}" for p in list_ports.comports()}

ui.colors(primary="#0d6b66", secondary="#1565c0", positive="#15803d", negative="#dc2626")
ui.add_head_html("<style>body{background:#f5f7f9}.top{background:#073b3a;color:white}.value{font-size:2.4rem;font-weight:700}.zone{border-radius:999px;padding:6px 12px;font-weight:700}</style>")

@ui.page("/")
def page():
    with ui.header().classes("top items-center justify-between px-6"):
        ui.label("🎚️  Noise Detection Board").classes("text-h6 font-bold")
        state = ui.label("● Disconnesso").classes("text-grey-4")
    with ui.column().classes("w-full max-w-7xl mx-auto p-5 gap-5"):
        with ui.card().classes("w-full rounded-2xl"):
            with ui.row().classes("w-full items-end gap-4"):
                selector = ui.select(available_ports(), label="Porta COM").classes("w-72")
                ui.label("115200 baud").classes("pb-3 text-grey-7")
                connect = ui.button("Connetti", icon="link")
                disconnect = ui.button("Disconnetti", icon="link_off").props("outline color=negative")
                ui.space(); ui.button("Esporta CSV", icon="download", on_click=lambda: ui.download(monitor.csv(), "laeq_export.csv", "text/csv"))
        with ui.row().classes("w-full gap-5"):
            with ui.card().classes("grow rounded-2xl p-5"):
                ui.label("Livello istantaneo SPL").classes("text-grey-7")
                current = ui.label("--,- dB").classes("value")
                zone = ui.label("In attesa di campioni").classes("zone bg-grey-3 text-grey-8 self-start")
            with ui.card().classes("grow rounded-2xl p-5"):
                ui.label("Soglia 1").classes("text-grey-7"); low = ui.number("dB", value=80, min=0, max=160).classes("w-full")
                led1 = ui.label("LED verde: zona sicura").classes("text-positive")
            with ui.card().classes("grow rounded-2xl p-5"):
                ui.label("Soglia 2").classes("text-grey-7"); high = ui.number("dB", value=100, min=0, max=160).classes("w-full")
                led2 = ui.label("LED rosso: non attivo").classes("text-grey-7")
        with ui.card().classes("w-full rounded-2xl p-5"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Andamento temporale LAeq").classes("text-h6 font-bold")
                period = ui.select({60:"1 minuto",300:"5 minuti",900:"15 minuti",1800:"30 minuti"}, value=60, label="Tempo base LAeqT").classes("w-52")
            chart = ui.echart({"tooltip":{"trigger":"axis"},"legend":{"data":["LAeqT","LAeq 1h","LAeq giornaliero"]},"xAxis":{"type":"category","data":[]},"yAxis":{"type":"value","name":"dB","min":0,"max":120},"series":[{"name":"LAeqT","type":"line","data":[],"smooth":True},{"name":"LAeq 1h","type":"line","data":[],"smooth":True},{"name":"LAeq giornaliero","type":"line","data":[],"smooth":True}]}).classes("w-full h-96")
        ui.label("Tre zone: verde sotto soglia 1, ambra tra le soglie, rosso sopra soglia 2.").classes("text-caption text-grey-7")

    def do_connect():
        if not selector.value: ui.notify("Seleziona una porta COM", type="warning"); return
        try: monitor.connect(selector.value); ui.notify(f"Connesso a {selector.value}", type="positive")
        except serial.SerialException as error: ui.notify(f"Connessione non riuscita: {error}", type="negative")
    connect.on("click", lambda _: do_connect())
    disconnect.on("click", lambda _: monitor.disconnect())
    def refresh():
        try: monitor.configure(float(low.value), float(high.value), int(period.value))
        except ValueError as error: ui.notify(str(error), type="warning"); return
        latest, port, first, second, base, hourly, daily = monitor.snapshot()
        current.text = "--,- dB" if latest is None else f"{latest:.1f}".replace(".", ",") + " dB"
        state.text = f"● Connesso: {port}" if port else "● Disconnesso"
        state.classes(replace="text-positive" if port else "text-grey-4")
        if latest is None: text, css = "In attesa di campioni", "zone bg-grey-3 text-grey-8 self-start"
        elif latest < first: text, css = "● ZONA VERDE - sotto soglia 1", "zone bg-green-1 text-positive self-start"
        elif latest < second: text, css = "● ZONA AMBRA - attenzione", "zone bg-amber-1 text-amber-10 self-start"
        else: text, css = "● ZONA ROSSA - soglia 2 superata", "zone bg-red-1 text-negative self-start"
        zone.text, zone.classes = text, css
        led1.text = f"LED verde: attivo sotto {first:.0f} dB"; active = latest is not None and latest >= second
        led2.text = f"LED rosso: {'ATTIVO' if active else 'non attivo'} sopra {second:.0f} dB"; led2.classes(replace="text-negative font-bold" if active else "text-grey-7")
        all_dates = sorted({x.timestamp for series in (base,hourly,daily) for x in series})
        chart.options["xAxis"]["data"] = [x.strftime("%d/%m %H:%M") for x in all_dates]
        for n, series in enumerate((base,hourly,daily)):
            values = {x.timestamp:round(x.db,1) for x in series}; chart.options["series"][n]["data"] = [values.get(x) for x in all_dates]
        chart.update()
        new_ports = available_ports()
        if new_ports != selector.options: selector.options = new_ports; selector.update()
    ui.timer(1.0, refresh)

ui.run(title="Noise Detection Board", favicon="🎚️")

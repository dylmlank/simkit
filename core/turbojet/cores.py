"""Turbocharger cores, as the starting point for a homebuilt turbojet.

Nearly every successful amateur turbojet is built around a turbocharger. The
reason is not cost, it is safety: the compressor and turbine wheels are cast,
heat-treated, balanced and speed-rated by a manufacturer, and those are exactly
the two parts an amateur cannot make safely. A 50 mm compressor wheel at
160,000 rpm holds its own weight times several thousand; a wheel that is
slightly out of balance or slightly porous does not fail gently.

So SimKit treats the core as a *given* and designs around it. You supply the
wheel it actually has, and the design solves for the thrust that follows,
rather than demanding a wheel that would have to be made.

ON THE NUMBERS BELOW
--------------------
These entries are nominal, drawn from commonly quoted figures for each family,
and every family has many variants with different wheels under the same name.
They are here so you have somewhere to start, not so you can skip measuring.
Before cutting metal, measure your actual wheel with calipers and use
`from_measurements`. `TurboCore.measured` records which you did, and the design
report says so plainly.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TurboCore:
    """One turbocharger core, described by what you can measure on it."""

    name: str
    #: Compressor wheel outside (exducer) diameter, mm. Sets pressure ratio.
    compressor_od_mm: float
    #: Compressor inducer (inlet) diameter, mm. Sets how much air it can pass.
    compressor_inducer_mm: float
    #: Turbine wheel outside diameter, mm.
    turbine_od_mm: float
    #: Manufacturer speed limit, rpm. The single most important safety number.
    max_rpm: float
    #: Wheel material. Cast aluminium alloy on essentially all of these.
    wheel_material: str = "al2618"
    notes: str = ""
    #: True when these came from calipers rather than from this table.
    measured: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def inducer_ratio(self) -> float:
        """Inducer / exducer diameter -- the wheel's 'eye ratio'."""
        return self.compressor_inducer_mm / self.compressor_od_mm

    def tip_speed(self, rpm: float | None = None) -> float:
        """Wheel tip speed in m/s. Cast aluminium is unhappy much past 500."""
        import math

        rpm = self.max_rpm if rpm is None else rpm
        return math.pi * (self.compressor_od_mm / 1000.0) * rpm / 60.0

    def check(self) -> list[str]:
        """Anything about this core that should be said out loud."""
        out = list(self.warnings)
        if not self.measured:
            out.append(
                f"'{self.name}' is a nominal entry, not a measurement. Wheel "
                "sizes vary between variants of the same turbo family - measure "
                "yours and pass it with from_measurements before building.")
        tip = self.tip_speed()
        if tip > 520:
            out.append(
                f"at its rated {self.max_rpm:,.0f} rpm this wheel tips "
                f"{tip:.0f} m/s, past what cast aluminium normally survives. "
                "Check the rating.")
        if not 0.4 <= self.inducer_ratio <= 0.85:
            out.append(
                f"inducer/exducer ratio {self.inducer_ratio:.2f} is outside the "
                "usual 0.45-0.80 for a compressor wheel. Check which diameter "
                "was measured - the inducer is the smaller, inlet-side one.")
        return out

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "compressor_od_mm": self.compressor_od_mm,
            "compressor_inducer_mm": self.compressor_inducer_mm,
            "turbine_od_mm": self.turbine_od_mm,
            "max_rpm": self.max_rpm,
            "tip_speed_at_max_m_s": round(self.tip_speed(), 1),
            "inducer_ratio": round(self.inducer_ratio, 3),
            "measured": self.measured,
            "notes": self.notes,
        }


#: Nominal cores, smallest first. See the module docstring before trusting one.
CORES: dict[str, TurboCore] = {
    core.name.lower(): core
    for core in [
        TurboCore(
            "GT1544", 44.0, 30.0, 41.0, 180_000,
            notes="Small Garrett frame. Suits a modest first engine, roughly "
                  "the 20-40 N class. Popular because it is cheap and common.",
        ),
        TurboCore(
            "TD04", 49.0, 33.0, 45.0, 165_000,
            notes="Mitsubishi frame, widely available secondhand. A sensible "
                  "30-60 N starting point.",
        ),
        TurboCore(
            "GT2560R", 53.0, 36.0, 47.0, 150_000,
            notes="Garrett ball-bearing frame. Ball bearings tolerate the "
                  "axial load of a jet better than a plain journal bearing.",
        ),
        TurboCore(
            "T3 60-trim", 60.0, 46.0, 55.0, 130_000,
            notes="Very common, very cheap, journal bearing. Frequently the "
                  "first engine people build; oil supply needs care.",
        ),
        TurboCore(
            "HX35", 76.0, 54.0, 67.0, 120_000,
            notes="Holset frame from a Cummins diesel. Big, tough and heavy - "
                  "the classic large amateur jet core, well past 100 N.",
        ),
        TurboCore(
            "GT3076R", 76.0, 58.0, 60.0, 125_000,
            notes="Larger Garrett ball-bearing frame. Capable, and the wheels "
                  "are better made than the older journal-bearing units.",
        ),
    ]
}


def get(name: str) -> TurboCore:
    """Look a core up by name."""
    core = CORES.get(name.strip().lower())
    if core is None:
        raise KeyError(
            f"no core called {name!r}. Known: {', '.join(sorted(CORES))}. "
            "If yours is not listed, measure it and use from_measurements - "
            "that is the better path anyway.")
    return core


def from_measurements(name: str, compressor_od_mm: float,
                      compressor_inducer_mm: float, turbine_od_mm: float,
                      max_rpm: float, wheel_material: str = "al2618",
                      notes: str = "") -> TurboCore:
    """Describe the core you actually have.

    Measure the compressor wheel at its widest point (the exducer, at the back)
    and at the inlet (the inducer, at the front). `max_rpm` should come from the
    manufacturer's data for your variant; if you genuinely cannot find it, a
    tip speed of 500 m/s is the physical ceiling for cast aluminium and the
    design will warn you as you approach it.
    """
    core = TurboCore(name, float(compressor_od_mm), float(compressor_inducer_mm),
                     float(turbine_od_mm), float(max_rpm), wheel_material,
                     notes, measured=True)
    if compressor_inducer_mm >= compressor_od_mm:
        core.warnings.append(
            f"the inducer ({compressor_inducer_mm} mm) is not smaller than the "
            f"exducer ({compressor_od_mm} mm). These are probably swapped: the "
            "inducer is the inlet eye, the exducer is the wide back face.")
    return core


def listing() -> list[dict]:
    return [core.as_dict() for core in CORES.values()]

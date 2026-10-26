"""A small air-fed thruster: supply, chamber, nozzle. Nothing that spins.

When the air comes from outside, the compressor disappears -- and so does the
turbine, whose only job was to drive that compressor. What is left is a plenum
and a nozzle, which is three parts and no rotating machinery.

That makes the physics simple enough to be exact rather than correlated. The
nozzle is choked for any supply above about 1.9 bar absolute, so the mass flow
follows from the throat area and the supply conditions alone, and the thrust
from the momentum leaving plus any pressure still unspent at the exit.

The binding constraint is almost never the nozzle. It is the compressor: a
hobby unit delivering 3 CFM continuous is 0.0018 kg/s, and no nozzle can give
back more momentum than the air you can feed it. Every design here is stated
against a supply you have to actually own.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

GAMMA = 1.4
R_AIR = 287.05          # J/kg/K
CP_AIR = 1005.0         # J/kg/K
P_ATM = 101325.0        # Pa
T_ATM = 288.15          # K
G0 = 9.80665

#: Free air at standard conditions, kg per cubic metre.
RHO_STD = P_ATM / (R_AIR * T_ATM)


def cfm_to_kg_s(cfm: float) -> float:
    """Compressor ratings are in CFM of *free* air; the physics wants kg/s."""
    return float(cfm) * 0.0283168 / 60.0 * RHO_STD


def kg_s_to_cfm(mass_flow: float) -> float:
    return float(mass_flow) / RHO_STD * 60.0 / 0.0283168


def litres_per_min_to_kg_s(lpm: float) -> float:
    return float(lpm) / 1000.0 / 60.0 * RHO_STD


def choked_pressure_ratio() -> float:
    """Total-to-ambient ratio above which the throat runs sonic."""
    return ((GAMMA + 1.0) / 2.0) ** (GAMMA / (GAMMA - 1.0))


def choked_flux(total_pressure: float, total_temperature: float) -> float:
    """Mass flow per unit throat area at choked conditions, kg/s/m2."""
    factor = math.sqrt(GAMMA / R_AIR) * (2.0 / (GAMMA + 1.0)) ** \
        ((GAMMA + 1.0) / (2.0 * (GAMMA - 1.0)))
    return total_pressure * factor / math.sqrt(total_temperature)


@dataclass
class Thruster:
    """One air-fed thruster operating point."""

    supply_pressure_bar_g: float
    supply_mass_flow: float          # kg/s
    chamber_temperature: float       # K
    throat_diameter_mm: float
    exit_diameter_mm: float
    exit_velocity: float             # m/s
    thrust_n: float
    choked: bool
    divergent: bool
    discharge_coefficient: float
    warnings: list

    def as_dict(self) -> dict:
        return {
            "supply_pressure_bar_g": round(self.supply_pressure_bar_g, 3),
            "supply_mass_flow_kg_s": round(self.supply_mass_flow, 6),
            "supply_cfm_free_air": round(kg_s_to_cfm(self.supply_mass_flow), 2),
            "chamber_temperature_K": round(self.chamber_temperature, 1),
            "throat_diameter_mm": round(self.throat_diameter_mm, 3),
            "exit_diameter_mm": round(self.exit_diameter_mm, 3),
            "exit_velocity_m_s": round(self.exit_velocity, 1),
            "thrust_N": round(self.thrust_n, 4),
            "thrust_gf": round(self.thrust_n / G0 * 1000, 1),
            "choked": self.choked,
            "nozzle": "convergent-divergent" if self.divergent else "convergent",
            "discharge_coefficient": self.discharge_coefficient,
        }


def design(supply_pressure_bar_g: float = 6.0,
           supply_cfm: float = 0.0, supply_mass_flow: float = 0.0,
           chamber_temperature: float = T_ATM,
           divergent: bool = False,
           discharge_coefficient: float = 0.95,
           ambient_pressure: float = P_ATM) -> Thruster:
    """Size a thruster around the air you can actually supply.

    Give the supply as CFM of free air (what a compressor is rated in) or
    directly as kg/s. `chamber_temperature` is ambient for a cold-gas thruster,
    or the burnt-gas temperature if you add a burner -- exit velocity rises
    with its square root, so heat is the cheapest thrust there is once the air
    supply is fixed.

    `divergent` adds a diverging cone after the throat. It buys real exit
    velocity at high supply pressure but only helps if it is expanded roughly
    to ambient; a badly sized one is worse than none.
    """
    if supply_mass_flow <= 0:
        if supply_cfm <= 0:
            raise ValueError(
                "give the air supply, either as supply_cfm (what your "
                "compressor is rated at, in CFM of free air) or as "
                "supply_mass_flow in kg/s")
        supply_mass_flow = cfm_to_kg_s(supply_cfm)

    total_pressure = float(supply_pressure_bar_g) * 1e5 + ambient_pressure
    ratio = total_pressure / ambient_pressure
    choked = ratio >= choked_pressure_ratio()
    warnings = []

    if not choked:
        warnings.append(
            f"the supply is only {ratio:.2f} times ambient, below the "
            f"{choked_pressure_ratio():.2f} needed to choke the throat. The "
            "thruster will still work but its flow depends on back pressure, "
            "so thrust will wander. Above about 0.9 bar gauge it chokes and "
            "becomes predictable.")

    # Throat area follows from the flow you can supply.
    flux = choked_flux(total_pressure, chamber_temperature)
    throat_area = supply_mass_flow / (discharge_coefficient * flux)
    throat_d = 2000.0 * math.sqrt(throat_area / math.pi)

    if divergent:
        # Perfectly expanded to ambient: all the pressure becomes velocity.
        pressure_term = (ambient_pressure / total_pressure) ** \
            ((GAMMA - 1.0) / GAMMA)
        exit_velocity = math.sqrt(
            2.0 * CP_AIR * chamber_temperature * (1.0 - pressure_term))
        exit_mach = exit_velocity / math.sqrt(
            GAMMA * R_AIR * chamber_temperature * pressure_term)
        area_ratio = (1.0 / exit_mach) * (
            (2.0 / (GAMMA + 1.0)) * (1.0 + 0.5 * (GAMMA - 1.0) * exit_mach ** 2)
        ) ** ((GAMMA + 1.0) / (2.0 * (GAMMA - 1.0)))
        exit_area = throat_area * area_ratio
        exit_d = 2000.0 * math.sqrt(exit_area / math.pi)
        thrust = supply_mass_flow * exit_velocity
    else:
        # Convergent only: sonic at the exit, with pressure left over.
        throat_t = chamber_temperature * 2.0 / (GAMMA + 1.0)
        exit_velocity = math.sqrt(GAMMA * R_AIR * throat_t)
        throat_p = total_pressure * (2.0 / (GAMMA + 1.0)) ** \
            (GAMMA / (GAMMA - 1.0))
        exit_area, exit_d = throat_area, throat_d
        thrust = (supply_mass_flow * exit_velocity
                  + max(0.0, throat_p - ambient_pressure) * throat_area)
        if not choked:
            # Subsonic: exit is at ambient and velocity follows the pressure drop.
            pressure_term = (ambient_pressure / total_pressure) ** \
                ((GAMMA - 1.0) / GAMMA)
            exit_velocity = math.sqrt(
                2.0 * CP_AIR * chamber_temperature * (1.0 - pressure_term))
            thrust = supply_mass_flow * exit_velocity

    if throat_d < 0.8:
        warnings.append(
            f"a {throat_d:.2f} mm throat is small enough that machining "
            "tolerance and any dirt in the air become the dominant error. "
            "Consider a larger supply.")
    if chamber_temperature > T_ATM + 50:
        warnings.append(
            "with a burner the chamber and nozzle run hot: use steel or "
            "stainless, not aluminium or a printed polymer, and keep the "
            "supply line away from the hot end.")
    if supply_pressure_bar_g > 10:
        warnings.append(
            f"{supply_pressure_bar_g:.0f} bar is above what ordinary push-fit "
            "pneumatic fittings are rated for. Check every fitting in the line.")

    return Thruster(
        supply_pressure_bar_g=float(supply_pressure_bar_g),
        supply_mass_flow=supply_mass_flow,
        chamber_temperature=float(chamber_temperature),
        throat_diameter_mm=throat_d,
        exit_diameter_mm=exit_d,
        exit_velocity=exit_velocity,
        thrust_n=thrust,
        choked=choked,
        divergent=bool(divergent),
        discharge_coefficient=float(discharge_coefficient),
        warnings=warnings,
    )


def supply_sweep(supply_cfm_list, supply_pressure_bar_g: float = 6.0,
                 chamber_temperature: float = T_ATM,
                 divergent: bool = False) -> list:
    """Thrust against the air supply -- the curve that decides the project."""
    out = []
    for cfm in supply_cfm_list:
        t = design(supply_pressure_bar_g, supply_cfm=cfm,
                   chamber_temperature=chamber_temperature, divergent=divergent)
        out.append(t.as_dict())
    return out


def heat_benefit(supply_cfm: float, supply_pressure_bar_g: float = 6.0,
                 temperatures=(T_ATM, 600.0, 900.0, 1200.0),
                 divergent: bool = False) -> list:
    """What a burner buys you, for a fixed air supply.

    Exit velocity goes as the square root of chamber temperature, so heating
    the same air is the cheapest thrust available once the compressor is
    chosen. It is also the step that turns a harmless air jet into something
    that needs steel and a fire plan.
    """
    return [design(supply_pressure_bar_g, supply_cfm=supply_cfm,
                   chamber_temperature=T, divergent=divergent).as_dict()
            for T in temperatures]


def endurance_from_tank(tank_litres: float, tank_pressure_bar_g: float,
                        mass_flow: float, usable_fraction: float = 0.85,
                        temperature: float = T_ATM) -> dict:
    """How long a bottle lasts, and what it weighs.

    This is the question that decides whether an air-fed thruster can fly or
    only run on a bench. A compressor hose is not going airborne, so if the
    model has to be free the air must come from a tank -- and then endurance
    and tank weight are the design, not the nozzle.

    `usable_fraction` allows for the tank pressure falling until it can no
    longer choke the throat; the last of the air comes out at reducing thrust.
    """
    if mass_flow <= 0:
        raise ValueError("mass flow must be positive")
    volume = float(tank_litres) / 1000.0
    absolute = float(tank_pressure_bar_g) * 1e5 + P_ATM
    stored = absolute * volume / (R_AIR * temperature)
    usable = stored * float(usable_fraction)
    seconds = usable / mass_flow

    # Composite HPA bottles run about 1 kg per litre of water volume filled;
    # steel is roughly twice that.
    tank_mass_kg = 1.0 * float(tank_litres)

    return {
        "tank_litres": float(tank_litres),
        "tank_pressure_bar_g": float(tank_pressure_bar_g),
        # Kept to more places than a readout needs: these feed the
        # thrust-to-weight calculation, and rounding here would show up there.
        "stored_air_kg": round(stored, 6),
        "usable_air_kg": round(usable, 6),
        "run_time_s": round(seconds, 2),
        "approx_tank_mass_kg": round(tank_mass_kg, 2),
        "note": (
            "a composite HPA bottle is roughly 1 kg per litre filled. Compare "
            "that against the thrust: if the tank outweighs what the thruster "
            "can lift, this is a bench demonstrator or a surface vehicle, not "
            "something that flies."),
    }


def can_it_fly(thrust_n: float, tank_litres: float,
               tank_pressure_bar_g: float, mass_flow: float,
               airframe_mass_kg: float = 0.0) -> dict:
    """Set the thrust against the weight of the air supply it needs.

    Stated plainly because it is the honest limit of an air-fed thruster: it
    makes thrust in newtons and needs a tank measured in kilograms.
    """
    tank = endurance_from_tank(tank_litres, tank_pressure_bar_g, mass_flow)
    total_mass = tank["approx_tank_mass_kg"] + float(airframe_mass_kg)
    weight = total_mass * G0
    ratio = thrust_n / weight if weight > 0 else 0.0
    return {
        **tank,
        "thrust_N": round(thrust_n, 3),
        "total_mass_kg": round(total_mass, 3),
        "weight_N": round(weight, 2),
        "thrust_to_weight": round(ratio, 3),
        "verdict": (
            "thrust exceeds the weight it has to carry, so free flight is at "
            "least arguable"
            if ratio >= 1.0 else
            f"thrust is {ratio:.2f} of the weight it must carry. This will not "
            "lift itself. It is a bench or surface-vehicle thruster unless the "
            "air comes from somewhere else."),
    }


def parts_list(thruster: Thruster) -> list:
    """Everything you have to make or buy. Deliberately short."""
    hot = thruster.chamber_temperature > T_ATM + 50
    material = "stainless or mild steel" if hot else "aluminium, brass or PETG"
    parts = [
        {"part": "plenum / chamber",
         "spec": f"tube, roughly {max(20.0, 4 * thruster.throat_diameter_mm):.0f} mm "
                 f"bore x {max(40.0, 8 * thruster.throat_diameter_mm):.0f} mm long",
         "material": material,
         "note": "big enough that the air slows to near rest before the nozzle, "
                 "so the supply pressure is what the nozzle actually sees"},
        {"part": "nozzle",
         "spec": (f"convergent-divergent, {thruster.throat_diameter_mm:.2f} mm "
                  f"throat to {thruster.exit_diameter_mm:.2f} mm exit"
                  if thruster.divergent else
                  f"convergent, {thruster.throat_diameter_mm:.2f} mm exit"),
         "material": material,
         "note": "the one dimension that matters. Make it a separate screw-in "
                 "piece so you can try a few sizes without rebuilding anything"},
        {"part": "supply fitting",
         "spec": "push-fit or threaded, to suit your compressor line",
         "material": "brass",
         "note": "size the line generously; a restrictive hose costs you "
                 "pressure exactly where it counts"},
    ]
    if hot:
        parts.append({
            "part": "fuel injector + igniter",
            "spec": "gas jet or small nozzle, plus a glow plug or spark",
            "material": "steel",
            "note": "this is the step that makes the project a fire risk rather "
                    "than a loud air jet. Add it last, outdoors, with a "
                    "extinguisher to hand"})
    return parts

"""Snow and wind loads by the codes of Kazakhstan, as in the owner's «ЛИРА+ Записка» (its
calculations "по нормам", checked against the source text of the codes; ported one to one, the
same numbers by the same rounding):

- SP RK EN 1991-1-3:2004/2011 with NTP RK 01-01-3.1 (4.1)-2017: duo-pitched roofs (5.3.3),
  multi-span roofs (5.3.4, annex B.2), roofs abutting taller construction (5.3.6, annex B.3);
- SP RK EN 1991-1-4:2005/2011 with NTP RK annex Zh: peak velocity pressure with zmin (table
  4.1), walls (table 7.1), flat roofs (table 7.2), vaulted roofs (7.2.8, figure 7.11).

Characteristic values, no partial factor. Each function returns the values and ``lines``: the
calculation in Russian, for the RPZ. Units: kPa and kg/m² (kPa / 9.81 · 1000), m, degrees.
"""

import math
from dataclasses import dataclass, field

# Districts: wind (NTP RK annex Zh) -> vb,0 m/s; snow -> sk map 4, map 5 (exceptional), map 6
# (exceptional drifts), kPa.
WIND = {"I": 20, "II": 24, "III": 30, "IV": 35, "V": 40, "VI": 45, "VII": 50, "VIII": 60}
SNOW = {
    "I": (0.8, 1.6, 0.8),
    "II": (1.2, 2.4, 1.2),
    "III": (1.5, 3.0, 1.5),
    "IV": (1.8, 3.6, 1.8),
    "V": (2.4, 4.8, 2.4),
    "VI": (3.2, 6.4, 3.2),
    "VII": (4.0, 8.0, 4.0),
    "VIII": (4.8, 9.6, 4.8),
}
# City -> wind district, snow district by maps 4, 5, 6 (the owner's list; extend per project).
CITIES = {
    "Алматы": ("II", "II", "II", "II"),
    "Астана": ("IV", "III", "III", "IV"),
    "Жезказган": ("III", "II", "II", "II"),
    "Караганда": ("II", "III", "III", "II"),
    "Павлодар": ("IV", "II", "II", "II"),
    "Петропавловск": ("", "IV", "IV", "IV"),
    "Темиртау": ("II", "III", "III", "II"),
    "Уральск": ("III", "IV", "IV", "II"),
    "Экибастуз": ("V", "I", "I", "I"),
}
TERRAIN = {"0": 0.003, "I": 0.01, "II": 0.05, "III": 0.3, "IV": 1.0}  # z0, m
ZMIN = {"0": 1.0, "I": 1.0, "II": 2.0, "III": 5.0, "IV": 10.0}  # m, table 4.1
ENV = {"Не защищенные от ветра": 0.8, "Обычные": 1.0, "Закрытые": 1.2}  # Ce
ROOF = {
    "Свес крыши с острыми углами": (-1.8, -1.2),
    "Крыша с парапетом (hp/h=0,025)": (-1.6, -1.1),
    "Крыша с парапетом (hp/h=0,05)": (-1.4, -0.9),
    "Крыша с парапетом (hp/h=0,1)": (-1.2, -0.8),
}
G = 9.81


def rnd(x: float, n: int) -> float:
    """Excel's ROUND: half away from zero (as the owner's sheets and app)."""
    if not math.isfinite(x):
        return x
    p = 10**n
    v = math.floor(abs(x) * p * (1 + 1e-15) + 0.5) / p
    return -v if x < 0 else v


def fmt(v: float | str | None, unit: str = "") -> str:
    if isinstance(v, str):
        text = v
    elif v is None or (isinstance(v, float) and math.isnan(v)):
        text = "#Н/Д"
    else:
        text = f"{float(f'{v:.15g}'):g}".replace(".", ",")
    return f"{text} {unit}".rstrip()


def kg(kpa: float) -> float:
    return rnd(kpa / G * 1000, 1)


def mu1(alpha: float) -> float:
    """Shape coefficient μ1 of a pitched roof, table 5.2."""
    return rnd(0.8 if alpha <= 30 else 0.0 if alpha >= 60 else 0.8 * (60 - alpha) / 30, 3)


@dataclass
class Climate:
    city: str
    wind: str
    vb0: float | None
    s4: str
    sk: float | None
    s5: str
    sk_exc: float | None
    s6: str
    sk_drift: float | None


def climate(city: str, cities: dict[str, tuple[str, str, str, str]] | None = None) -> Climate:
    table = cities or CITIES
    if city not in table:
        raise ValueError(
            f"город «{city}» не в справочнике: {', '.join(table)} (или задайте районы)"
        )
    w, s4, s5, s6 = table[city]
    if w and w not in WIND:
        raise ValueError(f"ветровой район «{w}»: один из {', '.join(WIND)}")
    for n, d in (("4", s4), ("5", s5), ("6", s6)):
        if d and d not in SNOW:
            raise ValueError(f"снеговой район по карте {n} «{d}»: один из {', '.join(SNOW)}")
    return Climate(
        city,
        w,
        WIND.get(w),
        s4,
        SNOW.get(s4, (None,) * 3)[0],
        s5,
        SNOW.get(s5, (None,) * 3)[1],
        s6,
        SNOW.get(s6, (None,) * 3)[2],
    )


PLACE = "место строительства"  # the name of a place given by its districts alone


def where(c: Climate) -> str:
    return c.city if c.city == PLACE else f"г. {c.city}"


def need_snow(c: Climate, *maps: str) -> None:
    """Refuse a snow calculation whose district on one of these maps is not given."""
    given = {"4": c.sk, "5": c.sk_exc, "6": c.sk_drift}
    missing = [m for m in maps if given[m] is None]
    if missing:
        raise ValueError(
            f"для «{c.city}» не задан снеговой район по карте {', '.join(missing)} "
            "(districts: snow4, snow5, snow6)"
        )


@dataclass
class Result:
    title: str
    values: dict[str, float] = field(default_factory=dict)
    loads: list[tuple[str, float]] = field(default_factory=list)  # (what, kg/m²) for the table
    lines: list[str] = field(default_factory=list)


def peak_pressure(
    c: Climate, terrain: str, ze: float, cdir: float = 1.0, cseason: float = 1.0, rho: float = 1.25
) -> tuple[dict[str, float], list[str]]:
    """qp(z) by chapter 4 with zmin; kPa and kg/m²."""
    if c.vb0 is None:
        raise ValueError(f"у города «{c.city}» не задан ветровой район")
    if terrain not in TERRAIN:
        raise ValueError(f"тип местности: один из {', '.join(TERRAIN)}")
    vb = c.vb0 * cdir * cseason
    z0, zmin = TERRAIN[terrain], ZMIN[terrain]
    z = max(ze, zmin)
    kr = rnd(0.19 * (z0 / 0.05) ** 0.07, 3)
    cr = rnd(kr * math.log(z / z0), 3)
    vm = rnd(cr * vb, 3)
    iv = rnd(1 / math.log(z / z0), 3)
    qm = rnd(0.5 * rho * vm * vm / 1000, 3)
    qp = rnd((1 + 7 * iv) * 0.5 * rho * vm * vm / 1000, 3)
    q = {
        "vb": vb,
        "z": z,
        "kr": kr,
        "cr": cr,
        "vm": vm,
        "Iv": iv,
        "qm": qm,
        "qp": qp,
        "qp_kg": kg(qp),
    }
    lines = [
        f"Ветровой район {c.wind} (НТП РК прил. Ж): vb,0 = {fmt(c.vb0, 'м/с')}; "
        f"vb = cdir·cseason·vb,0 = {fmt(vb, 'м/с')}",
        f"Тип местности {terrain}: z0 = {fmt(z0)} м, zmin = {fmt(zmin)} м (табл. 4.1); "
        f"z = max(ze; zmin) = {fmt(rnd(z, 3), 'м')}",
        f"kr = 0,19·(z0/0,05)^0,07 = {fmt(kr)}; cr(z) = kr·ln(z/z0) = {fmt(cr)}; co(z) = 1",
        f"vm(z) = cr·co·vb = {fmt(vm, 'м/с')}; Iv(z) = 1/ln(z/z0) = {fmt(iv)}",
        f"qp(z) = [1 + 7·Iv]·0,5·ρ·vm² = {fmt(qp, 'кПа')} = {fmt(q['qp_kg'], 'кг/м²')} "
        f"(среднее qm = {fmt(qm)} кПа)",
    ]
    return q, lines


def snow_duopitch(
    c: Climate, span: float, a1: float, a2: float, env: str = "Обычные", ct: float = 1.0
) -> Result:
    """Duo-pitched roof, 5.3.3: cases I-III; exceptional snow (map 5) for spans of 18 m and more
    (NTP RK 3.4.1)."""
    ce = ENV[env]
    m1, m2 = mu1(a1), mu1(a2)
    s = lambda m, k, part=1.0: rnd(part * m * k * ce * ct, 3)  # noqa: E731
    need_snow(c, "4", *(["5"] if span >= 18 else []))
    sk, ske = c.sk, c.sk_exc if c.sk_exc is not None else 0.0
    norm = [(s(m1, sk), s(m2, sk)), (s(m1, sk, 0.5), s(m2, sk)), (s(m1, sk), s(m2, sk, 0.5))]
    acc = [(s(m1, ske), s(m2, ske)), (s(m1, ske, 0.5), s(m2, ske)), (s(m1, ske), s(m2, ske, 0.5))]
    p4, p5 = kg(max(v[0] for v in norm)), kg(max(v[1] for v in norm))
    p6 = kg(max(v[0] for v in acc)) if span >= 18 else 0.0
    p7 = kg(max(v[1] for v in acc)) if span >= 18 else 0.0
    r = Result("Снеговая нагрузка на двускатное покрытие (СП РК EN 1991-1-3, 5.3.3)")
    r.values = {"snowRoof": max(p4, p5), "snowAcc": max(p6, p7), "sk": sk, "span": span}
    r.loads.append(("Снеговая нагрузка на покрытие (СП РК EN 1991-1-3)", max(p4, p5)))
    if span >= 18:
        r.loads.append(
            ("Чрезвычайная снеговая нагрузка на покрытие (аварийная ситуация)", max(p6, p7))
        )
    r.lines = [
        f"Место строительства: {where(c)}; пролёт {fmt(span)} м; α1 = {fmt(a1)}°, α2 = {fmt(a2)}°",
        f"Снеговой район по карте 4: {c.s4}, sk = {fmt(sk, 'кПа')}; по карте 5: "
        + (f"{c.s5}, sk = {fmt(c.sk_exc, 'кПа')}" if c.sk_exc is not None else "не задан"),
        "Пролёт ≥ 18 м: по НТП РК п. 3.4.1 покрытие проверяется на чрезвычайные снеговые нагрузки."
        if span >= 18
        else "Пролёт < 18 м: чрезвычайные снеговые нагрузки учитывать не требуется.",
        f"Ce = {fmt(ce)} ({env}); Ct = {fmt(ct)}; μ1(α1) = {fmt(m1)}; μ1(α2) = {fmt(m2)}",
        "Постоянная/переходная ситуация, s = μ·Ce·Ct·sk:",
        *[
            f"   случай {n}: s(α1) = {fmt(v[0], 'кПа')}; s(α2) = {fmt(v[1], 'кПа')}"
            for n, v in zip(("I", "II", "III"), norm, strict=True)
        ],
        *(
            [
                "Аварийная ситуация (sk по карте 5):",
                *[
                    f"   случай {n}: s(α1) = {fmt(v[0], 'кПа')}; s(α2) = {fmt(v[1], 'кПа')}"
                    for n, v in zip(("I", "II", "III"), acc, strict=True)
                ],
            ]
            if span >= 18
            else []
        ),
        f"Итого, кг/м²: нормативная — скат 1 {fmt(p4)}, скат 2 {fmt(p5)}"
        + (f"; аварийная — скат 1 {fmt(p6)}, скат 2 {fmt(p7)}" if span >= 18 else ""),
    ]
    return r


def snow_multispan(
    c: Climate,
    span: float,
    a1: float,
    a2: float,
    a3: float,
    a4: float,
    l2: float,
    l3: float,
    h: float,
    env: str = "Обычные",
    ct: float = 1.0,
) -> Result:
    """Multi-span roof, 5.3.4 and annex B.2."""
    ce = ENV[env]
    am = (a2 + a3) / 2
    mu2 = rnd(0.8 + 0.8 * am / 30, 3) if am <= 30 else (1.6 if am < 60 else float("nan"))
    m = [mu1(a1), mu1(a2), mu1(a3), mu1(a4)]
    s = lambda mu, k: rnd(mu * ce * ct * k, 3)  # noqa: E731
    need_snow(c, "4", *(["5", "6"] if span >= 18 else []))
    sk = c.sk
    ske = c.sk_exc if c.sk_exc is not None else 0.0
    skd = c.sk_drift if c.sk_drift is not None else 1.0
    b3 = l2 + l3
    m_b = rnd(min(2 * h / skd, 2 * b3 / (l2 + l3), 5), 3)
    s_b = rnd(m_b * skd, 3)
    case1 = [s(x, sk) for x in m]
    case2 = [s(m[0], sk), s(mu2, sk), s(m[3], sk)]
    acc1 = [s(x, ske) for x in m]
    acc2 = [s(m[0], ske), s(mu2, ske), s(m[3], ske)]
    mx = kg(max(case1 + case2))
    r = Result("Снеговая нагрузка на многопролётное покрытие (СП РК EN 1991-1-3, 5.3.4, прил. B.2)")
    r.values = {
        "snowRoof": mx,
        "snowAcc": kg(max(acc1 + acc2)) if c.sk_exc is not None else None,
        "snowDrift": kg(s_b) if c.sk_drift is not None else None,
        "sk": sk,
        "span": span,
    }
    r.loads.append(("Снеговая нагрузка на покрытие, макс. (СП РК EN 1991-1-3, 5.3.4)", mx))
    if span >= 18:
        r.loads.append(
            ("Чрезвычайная снеговая нагрузка на покрытие (аварийная, карта 5)", r.values["snowAcc"])
        )
        r.loads.append(
            ("Чрезвычайный нанос в разжелобке (аварийная, прил. B.2)", r.values["snowDrift"])
        )
    r.lines = [
        f"Место строительства: {where(c)}; пролёт {fmt(span)} м",
        f"sk (карта 4, район {c.s4}) = {fmt(sk, 'кПа')}; карта 5 "
        + (f"({c.s5}) = {fmt(ske, 'кПа')}" if c.sk_exc is not None else "не задана")
        + "; карта 6 "
        + (f"({c.s6}) = {fmt(skd, 'кПа')}" if c.sk_drift is not None else "не задана")
        + f"; Ce = {fmt(ce)}; Ct = {fmt(ct)}",
        f"μ1(α1…α4) = {'; '.join(fmt(x) for x in m)}",
        f"μ2 в разжелобке при ᾱ = (α2+α3)/2 = {fmt(am)}°: {fmt(mu2)} (табл. 5.2)",
        "Случай I: " + "  ".join(f"s{i + 1} = {fmt(v, 'кПа')}" for i, v in enumerate(case1)),
        f"Случай II: крайние скаты {fmt(case2[0], 'кПа')} и {fmt(case2[2], 'кПа')}; "
        f"разжелобок {fmt(case2[1], 'кПа')}; максимум {fmt(mx, 'кг/м²')}",
    ]
    if span >= 18:
        r.lines += [
            f"Аварийная, карта 5: случай I {'; '.join(fmt(v) for v in acc1)} кПа; случай II "
            f"{fmt(acc2[0])}, {fmt(acc2[1])}, {fmt(acc2[2])} кПа",
            f"Чрезвычайный нанос (прил. B.2): μ1 = min(2h/sk; 2b3/(ls1+ls2); 5) = {fmt(m_b)}; "
            f"s = μ1·sk = {fmt(s_b, 'кПа')} ({fmt(kg(s_b), 'кг/м²')}), γ = 1,0",
        ]
    return r


def snow_step(
    c: Climate,
    a1: float,
    a_up: float,
    h: float,
    b1: float,
    b2: float,
    b_up: float,
    env: str = "Обычные",
    ct: float = 1.0,
    gamma: float = 2.0,
) -> Result:
    """Roof abutting a taller construction: 5.3.6 (persistent) and annex B.3 (exceptional
    drift, sk by map 6)."""
    need_snow(c, "4", "6")
    sk, skd, ce = c.sk, c.sk_drift, ENV[env]
    m1 = mu1(a1)
    ls_raw = 2 * h
    ls = min(max(ls_raw, 5), 15)
    ms = 0.0 if a_up <= 15 else rnd(mu1(a_up) * b_up / ls, 3)
    mw_raw, mw_lim = (b1 + b2) / (2 * h), gamma * h / sk
    mw = rnd(min(max(min(mw_raw, mw_lim), 0.8), 4), 3)
    m2 = rnd(ms + mw, 3)
    s1, s2 = rnd(m1 * ce * ct * sk, 3), rnd(m2 * ce * ct * sk, 3)
    edge = rnd(m2 - (m2 - m1) * b2 / ls, 3) if b2 < ls else None
    ls_b, b = min(5 * h, b1, 15), max(b1, b2)
    m3 = rnd(min(2 * h / skd, 2 * b / ls_b, 8), 3)
    m1_b = m3 if a1 <= 15 else rnd(m3 * (30 - a1) / 15, 3) if a1 <= 30 else 0.0
    m2_b = m3 if a1 <= 30 else rnd(m3 * (60 - a1) / 30, 3) if a1 < 60 else 0.0
    s_b = rnd(max(m1_b, m2_b) * skd, 3)
    r = Result("Снеговой мешок у перепада высот (СП РК EN 1991-1-3, 5.3.6, прил. B.3)")
    r.values = {"snowRoof": kg(s1), "snowBag": kg(s2), "snowAccBag": kg(s_b), "sk": sk}
    r.loads = [
        ("Снеговая нагрузка на покрытие (СП РК EN 1991-1-3, 5.3.6)", kg(s1)),
        ("Снеговой мешок у перепада высот, макс. (СП РК EN 1991-1-3, 5.3.6)", kg(s2)),
        ("Чрезвычайный снеговой нанос у перепада (аварийная, прил. B.3)", kg(s_b)),
    ]
    r.lines = [
        f"Место строительства: {where(c)}; sk (карта 4, район {c.s4}) = {fmt(sk, 'кПа')}; "
        f"sk (карта 6, район {c.s6}) = {fmt(skd, 'кПа')}; Ce = {fmt(ce)}; Ct = {fmt(ct)}; γ = "
        f"{fmt(gamma)} кН/м³",
        f"h = {fmt(h)} м; b1 = {fmt(b1)} м; b2 = {fmt(b2)} м; α1 = {fmt(a1)}°; уклон верхней "
        f"кровли {fmt(a_up)}°",
        "1. Постоянная/переходная ситуация (п. 5.3.6)",
        f"μ1 = {fmt(m1)}; ls = 2h = {fmt(rnd(ls_raw, 3))} м → 5 ≤ ls ≤ 15 м: ls = {fmt(ls, 'м')}",
        f"μs = {fmt(ms)} "
        + (
            "(α ≤ 15°)"
            if a_up <= 15
            else "(50 % снега со ската верхней кровли: μ1·b/ls = "
            f"{fmt(mu1(a_up))}·{fmt(b_up)}/{fmt(ls)})"
        ),
        f"μw = (b1 + b2)/(2h) = {fmt(rnd(mw_raw, 3))}; γh/sk = {fmt(rnd(mw_lim, 3))}; 0,8 ≤ μw "
        f"≤ 4 → μw = {fmt(mw)}",
        f"μ2 = μs + μw = {fmt(m2)} (ф. 5.7)",
        *([f"b2 < ls: на краю нижней кровли μ = {fmt(edge)}"] if edge is not None else []),
        f"s без наноса = {fmt(s1, 'кПа')} ({fmt(kg(s1), 'кг/м²')}); у перепада = {fmt(s2, 'кПа')} "
        f"({fmt(kg(s2), 'кг/м²')}), убывает до μ1 на длине ls",
        "2. Аварийная ситуация — чрезвычайный нанос (прил. B.3)",
        f"ls = min(5h; b1; 15) = {fmt(ls_b, 'м')}; μ3 = min(2h/sk; 2b/ls; 8) = {fmt(m3)}; "
        f"по табл. B.1: μ1 = {fmt(m1_b)}, μ2 = {fmt(m2_b)}",
        f"s = μ·sk (карта 6) = {fmt(s_b, 'кПа')} ({fmt(kg(s_b), 'кг/м²')}), γ = 1,0",
        "Трактовка μs (треугольная эпюра 50 % снега со ската) — принятая; проверьте по условиям "
        "объекта.",
    ]
    return r


def wind_building(
    c: Climate,
    terrain: str,
    h: float,
    l1: float,
    l2: float,
    roof: str = "Свес крыши с острыми углами",
    cdir: float = 1.0,
    cseason: float = 1.0,
    rho: float = 1.25,
) -> Result:
    """Walls (table 7.1, two directions) and a flat roof (table 7.2) of a low building."""
    q, qlines = peak_pressure(c, terrain, h, cdir, cseason, rho)
    qk = q["qp_kg"]
    r = Result("Ветровая нагрузка на здание (СП РК EN 1991-1-4, табл. 7.1, 7.2)")
    r.values = {"qp": qk, "vb": c.vb0, "h": h}
    r.lines = list(qlines)
    we = lambda cpe: rnd(cpe * qk, 2)  # noqa: E731
    for k, (name, b, d) in enumerate(
        (("на длинную сторону", l1, l2), ("вдоль длинной стороны", l2, l1)), start=1
    ):
        e, hd = min(b, 2 * h), rnd(h / d, 3)
        cd = 0.8 if hd >= 1 else 0.7 if hd <= 0.25 else rnd(0.7 + (hd - 0.25) / 0.75 * 0.1, 3)
        ce_ = -0.5 if hd <= 1 else -0.7 if hd >= 5 else rnd(-0.5 + (hd - 1) / 4 * (-0.2), 3)
        if e < d:
            zones = [("A", -1.2, e / 5), ("B", -0.8, 4 * e / 5), ("C", -0.5, d - e)]
        elif e < 5 * d:
            zones = [("A", -1.2, e / 5), ("B", -0.8, d - e / 5)]
        else:
            zones = [("A", -1.2, d)]
        r.values[f"wD{k}"], r.values[f"wE{k}"] = we(cd), abs(we(ce_))
        r.loads.append((f"Ветер {name}: наветренная, we = {fmt(qk)}·{fmt(cd)}", we(cd)))
        r.loads.append(
            (f"Ветер {name}: подветренная, we = {fmt(qk)}·{fmt(abs(ce_))}", abs(we(ce_)))
        )
        r.lines += [
            f"Ветер {name}: b = {fmt(b)} м, d = {fmt(d)} м; e = min(b; 2h) = {fmt(e)} м; h/d = "
            f"{fmt(hd)}",
            *[
                f"   зона {z}: cpe,10 = {fmt(cpe)}; we = {fmt(we(cpe), 'кг/м²')}; длина "
                f"{fmt(rnd(ln, 2))} м"
                for z, cpe, ln in zones
            ],
            f"   зона D (наветренная): cpe,10 = {fmt(cd)}; we = {fmt(we(cd), 'кг/м²')}",
            f"   зона E (подветренная): cpe,10 = {fmt(ce_)}; we = {fmt(we(ce_), 'кг/м²')}",
        ]
    f_, g_ = ROOF[roof]
    e1 = min(l1, 2 * h)
    r.lines += [
        f"Плоская кровля (табл. 7.2), {roof}: F {fmt(f_)} ({fmt(we(f_), 'кг/м²')}); G {fmt(g_)} "
        f"({fmt(we(g_), 'кг/м²')}); H −0,7 ({fmt(we(-0.7), 'кг/м²')}); I ±0,2 "
        f"(±{fmt(we(0.2), 'кг/м²')})",
        f"Зоны при ветре на длинную сторону: e = {fmt(e1)} м; F — {fmt(e1 / 4)} × "
        f"{fmt(e1 / 10)} м; "
        f"H — {fmt(0.4 * e1)} м. Внутреннее давление cpi не учтено.",
    ]
    if h > min(l1, l2):
        r.lines.append("Внимание: h > b — по п. 7.2.2 базовая высота ze принимается по полосам.")
    return r


def vault_cpe(r: float) -> dict[str, float | None]:
    """cpe,10 of a vaulted roof by figure 7.11 (read off the graph, about ±0.02)."""
    lin = lambda x, x0, y0, x1, y1: y0 + (y1 - y0) * (x - x0) / (x1 - x0)  # noqa: E731
    a0 = lin(r, 0, 0, 0.05, 0.1) if r < 0.05 else lin(r, 0.05, 0.1, 0.5, 0.8) if r <= 0.5 else 0.8
    a5n = a5p = None
    if r < 0.05:
        a5n = lin(r, 0, -0.7, 0.05, -0.95)
    elif r < 0.1:
        a5n = lin(r, 0.05, -0.95, 0.1, -1.2)
    elif r <= 0.2:
        a5n = -1.2
    elif r < 0.3:
        a5n = lin(r, 0.2, -1.2, 0.3, -0.3)
    if r >= 0.2:
        a5p = lin(r, 0.2, 0, 0.5, 0.8) if r <= 0.5 else 0.8
    if r >= 0.3:
        a5n = None
    if r == 0.2:
        a5p = None
    b = (
        lin(r, 0, -0.7, 0.05, -0.75)
        if r < 0.05
        else lin(r, 0.05, -0.75, 0.5, -1.2)
        if r <= 0.5
        else -1.2
    )
    cc = (
        lin(r, 0, -0.7, 0.05, -0.55)
        if r < 0.05
        else lin(r, 0.05, -0.55, 0.1, -0.4)
        if r <= 0.1
        else -0.4
    )
    return {
        "A0": rnd(a0, 3),
        "A5neg": None if a5n is None else rnd(a5n, 3),
        "A5pos": None if a5p is None else rnd(a5p, 3),
        "B": rnd(b, 3),
        "C": rnd(cc, 3),
    }


def wind_vault(
    c: Climate,
    terrain: str,
    h: float,
    f: float,
    d: float,
    length: float,
    cdir: float = 1.0,
    cseason: float = 1.0,
    rho: float = 1.25,
) -> Result:
    """Vaulted roof, 7.2.8, figure 7.11; ze = h + f."""
    ze = h + f
    q, qlines = peak_pressure(c, terrain, ze, cdir, cseason, rho)
    qk = q["qp_kg"]
    ratio, k = f / d, min((h / d) / 0.5, 1)
    cp = vault_cpe(ratio)
    mix = lambda a0, a5: None if a0 is None or a5 is None else rnd(a0 + (a5 - a0) * k, 3)  # noqa: E731
    a_vals: list[float] = []
    for a5 in (cp["A5neg"], cp["A5pos"]):
        v = mix(cp["A0"], a5)
        if v is not None and v not in a_vals:
            a_vals.append(v)
    we = lambda cpe: rnd(cpe * qk, 2)  # noqa: E731
    r = Result("Ветровая нагрузка на сводчатое покрытие (СП РК EN 1991-1-4, 7.2.8, рис. 7.11)")
    r.values = {
        "qp": qk,
        "Amax": max(we(a) for a in a_vals),
        "Amin": min(we(a) for a in a_vals),
        "B": we(cp["B"]),
        "C": we(cp["C"]),
    }
    r.loads = [(f"Ветер на свод, зона A (наветренная), cpe = {fmt(a)}", we(a)) for a in a_vals]
    r.loads += [
        (f"Ветер на свод, зона B (средняя), cpe = {fmt(cp['B'])}", we(cp["B"])),
        (f"Ветер на свод, зона C (подветренная), cpe = {fmt(cp['C'])}", we(cp["C"])),
    ]
    r.lines = [
        f"Базовая высота ze = h + f = {fmt(h)} + {fmt(f)} = {fmt(rnd(ze, 3), 'м')}",
        *qlines,
        f"f/d = {fmt(rnd(ratio, 3))}; h/d = {fmt(rnd(h / d, 3))}"
        + (" → зона A интерполируется между h/d = 0 и h/d ≥ 0,5" if 0 < h / d < 0.5 else ""),
        f"Зона A: cpe,10 = {' и '.join(fmt(a) for a in a_vals)}; we = "
        f"{' и '.join(fmt(we(a), 'кг/м²') for a in a_vals)}",
        f"Зона B: cpe,10 = {fmt(cp['B'])}; we = {fmt(we(cp['B']), 'кг/м²')}",
        f"Зона C: cpe,10 = {fmt(cp['C'])}; we = {fmt(we(cp['C']), 'кг/м²')}",
        f"Торцевые стены (ветер вдоль здания, l = {fmt(length)} м) — по табл. 7.1. cpe,10 сняты с "
        "графика рис. 7.11 (точность около ±0,02). Внутреннее давление cpi не учтено.",
    ]
    if ratio < 0.05:
        r.lines.insert(
            1,
            "Внимание: f/d < 0,05 — участок рис. 7.11 показан пунктиром (не для плоских покрытий).",
        )
    return r
